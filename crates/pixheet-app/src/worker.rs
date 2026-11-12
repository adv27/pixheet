//! The worker: it converts the jobs of the queue, one after the other, and sends a
//! message for each step.
//!
//! A reader thread decodes the next image while the worker fits and writes the current
//! one, as the command does. The decode uses one CPU; the other steps use all of them.
//! The child module `preview` makes the preview of a card, and `failure` gives the
//! reason of a failed conversion.

mod failure;
mod preview;

use std::collections::{HashSet, VecDeque};
use std::ops::ControlFlow;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use pixheet_core::{ConvertResult, Error, Options, Pixels, check_output, fit_image, load, write};

pub use failure::Failure;
use failure::{classify, name};
use preview::preview_url;

/// The marker of an output that appeared after the plan.
const EXISTS: &str = "the output exists";

/// One image to convert.
#[derive(Clone, Debug)]
pub struct Job {
    pub id: u64,
    pub src: PathBuf,
    pub dst: PathBuf,
    /// Set to stop this job, but not the queue.
    pub skip: Arc<AtomicBool>,
    /// True when the job can write over a file at `dst`: the policy Replace, or Convert
    /// again of the own output. Else the worker refuses to write over a file that
    /// appeared after the plan.
    pub replace: bool,
}

/// The jobs that wait. The window and the worker share it.
#[derive(Default)]
pub struct Queue(Mutex<VecDeque<Job>>);

impl Queue {
    fn jobs(&self) -> std::sync::MutexGuard<'_, VecDeque<Job>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn put(&self, jobs: impl IntoIterator<Item = Job>) {
        self.jobs().extend(jobs);
    }

    pub fn take(&self) -> Option<Job> {
        self.jobs().pop_front()
    }

    pub fn remove(&self, ids: &HashSet<u64>) {
        self.jobs().retain(|job| !ids.contains(&job.id));
    }

    /// Take all jobs that wait out of the queue. The job that the reader has keeps going.
    pub fn drain(&self) -> Vec<Job> {
        self.jobs().drain(..).collect()
    }

    pub fn clear(&self) {
        self.jobs().clear();
    }

    pub fn is_empty(&self) -> bool {
        self.jobs().is_empty()
    }
}

/// A message of the worker.
#[derive(Clone, Debug)]
pub enum Message {
    Started {
        id: u64,
    },
    /// The fitted image, as a small PNG in a data URL.
    Preview {
        id: u64,
        url: String,
    },
    Progress {
        id: u64,
        percent: u8,
    },
    Done {
        id: u64,
        result: ConvertResult,
        bytes: u64,
    },
    Failed {
        id: u64,
        failure: Failure,
    },
    Stopped {
        id: u64,
    },
    /// The last message of a run.
    Finished {
        stopped: bool,
    },
}

/// An image that the reader decoded, or the error of the decode.
struct Loaded {
    job: Job,
    options: Options,
    decode: Duration,
    image: Result<Pixels, Error>,
}

/// Convert the jobs of `queue` until it is empty or `cancel` is set.
///
/// `options` gives the options of each job when its decode starts. Thus a change of the
/// settings has an effect on the jobs that did not start. The last message is always
/// [`Message::Finished`].
pub fn run(
    queue: Arc<Queue>,
    options: Arc<Mutex<Options>>,
    cancel: Arc<AtomicBool>,
    post: &mut dyn FnMut(Message),
) {
    let (tx, rx) = mpsc::sync_channel::<Loaded>(0);
    let reader_cancel = Arc::clone(&cancel);
    let reader = std::thread::Builder::new()
        .name("pixheet-reader".into())
        .spawn(move || {
            while !reader_cancel.load(Ordering::SeqCst) {
                let Some(job) = queue.take() else { return };
                let options = *options.lock().unwrap_or_else(|e| e.into_inner());
                let start = Instant::now();
                let image = if job.skip.load(Ordering::SeqCst) {
                    Err(Error::Cancelled)
                } else {
                    check_output(&job.src, &job.dst)
                        .and_then(|()| load(&job.src, options.background))
                };
                let loaded = Loaded {
                    job,
                    options,
                    decode: start.elapsed(),
                    image,
                };
                if tx.send(loaded).is_err() {
                    return;
                }
            }
        });
    let mut stopped = false;
    for loaded in rx.iter() {
        if cancel.load(Ordering::SeqCst) {
            stopped = true;
            break; // the job of `loaded` did not start: it stays ready
        }
        convert_one(loaded, &cancel, post);
    }
    drop(rx);
    if let Ok(reader) = reader {
        let _ = reader.join();
    }
    post(Message::Finished {
        stopped: stopped || cancel.load(Ordering::SeqCst),
    });
}

fn convert_one(loaded: Loaded, cancel: &AtomicBool, post: &mut dyn FnMut(Message)) {
    let Loaded {
        job,
        options,
        decode,
        image,
    } = loaded;
    let id = job.id;
    post(Message::Started { id });
    let start = Instant::now();
    let fitted = image.and_then(|upright| {
        let original_size = (upright.w, upright.h);
        fit_image(upright, options.max_side).map(|fitted| (original_size, fitted))
    });
    let result = fitted.and_then(|(original_size, fitted)| {
        // The card shows the sheet while the writer works.
        if let Some(url) = preview_url(&fitted) {
            post(Message::Preview { id, url });
        }
        let mut last = 0u8;
        let mut progress = |done: usize, total: usize| {
            if cancel.load(Ordering::SeqCst) || job.skip.load(Ordering::SeqCst) {
                return ControlFlow::Break(());
            }
            let percent = (done * 100)
                .checked_div(total)
                .map_or(0, |p| p.min(100) as u8);
            if percent != last {
                last = percent;
                post(Message::Progress { id, percent });
            }
            ControlFlow::Continue(())
        };
        // A file that appeared after the plan must not go away without permission.
        if !job.replace && job.dst.exists() {
            return Err(Error::Invalid(EXISTS.to_string()));
        }
        let colors = write(&fitted, &job.dst, options.compact, &mut progress)?;
        Ok(ConvertResult {
            original_size,
            size: (fitted.w, fitted.h),
            colors,
            cells: fitted.w * fitted.h,
            seconds: (decode + start.elapsed()).as_secs_f64(),
        })
    });
    post(match result {
        Ok(result) => {
            let bytes = std::fs::metadata(&job.dst).map(|m| m.len()).unwrap_or(0);
            Message::Done { id, result, bytes }
        }
        Err(Error::Cancelled) => Message::Stopped { id },
        Err(Error::Invalid(text)) if text == EXISTS => Message::Failed {
            id,
            failure: Failure {
                reason: "File exists".into(),
                detail: format!(
                    "{} appeared after the image was added. Retry to apply the setting \"If the file exists\".",
                    name(&job.dst)
                ),
            },
        },
        Err(error) => Message::Failed {
            id,
            failure: classify(&error, &job.dst),
        },
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_skipped_job_stops_and_the_next_job_converts() {
        let dir = tempfile::tempdir().unwrap();
        let job = |id: u64, name: &str, skip: bool| {
            let src = dir.path().join(format!("{name}.png"));
            image::RgbImage::from_pixel(3, 2, image::Rgb([id as u8, 2, 3]))
                .save(&src)
                .unwrap();
            Job {
                id,
                dst: src.with_extension("xlsx"),
                src,
                skip: Arc::new(AtomicBool::new(skip)),
                replace: false,
            }
        };
        let queue = Queue::default();
        queue.put([job(1, "a", true), job(2, "b", false)]);
        let mut messages = Vec::new();
        let options = Arc::new(Mutex::new(Options::default()));
        let cancel = Arc::new(AtomicBool::new(false));
        run(Arc::new(queue), options, cancel, &mut |m| messages.push(m));

        assert!(matches!(messages[0], Message::Started { id: 1 }));
        assert!(matches!(messages[1], Message::Stopped { id: 1 }));
        assert!(!dir.path().join("a.xlsx").exists());
        assert!(
            messages
                .iter()
                .any(|m| matches!(m, Message::Done { id: 2, .. }))
        );
        assert!(dir.path().join("b.xlsx").is_file());
        assert!(matches!(
            messages.last(),
            Some(Message::Finished { stopped: false })
        ));
    }
}
