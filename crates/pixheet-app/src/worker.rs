//! The worker: it converts the jobs of the queue, one after the other, and sends a
//! message for each step.
//!
//! A reader thread decodes the next image while the worker fits and writes the current
//! one, as the command does. The decode uses one CPU; the other steps use all of them.

use std::collections::{HashSet, VecDeque};
use std::io;
use std::ops::ControlFlow;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use base64::Engine;
use pixheet_core::{
    ConvertResult, Error, Options, Pixels, Step, check_output, fit_image, io_reason, load, write,
};

/// The marker of an output that appeared after the plan.
const EXISTS: &str = "the output exists";

/// The longest side of a preview, in pixels. A card is at most about 400 CSS pixels wide, and
/// the preview must stay sharp on a display with a scale above 100 %.
const PREVIEW_SIDE: usize = 512;

/// The largest PNG preview, in bytes. A larger preview, for example a photo, goes as a JPEG.
const PNG_BUDGET: usize = 96 * 1024;

/// The quality of a JPEG preview.
const JPEG_QUALITY: u8 = 88;

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

/// Why a conversion failed: a short reason for the card and a full text.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Failure {
    pub reason: String,
    pub detail: String,
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

/// A copy of `image` for the card, as a PNG or JPEG data URL.
///
/// The longest side of the copy is near [`PREVIEW_SIDE`], so the page does not scale it up
/// much. A large image shrinks with the Catmull-Rom filter: thin lines and text stay clear
/// at each ratio. A small image grows by a whole number with the nearest pixel, so each cell stays
/// a sharp square.
pub fn preview_url(image: &Pixels) -> Option<String> {
    let preview = preview_pixels(image)?;
    let (w, h) = (preview.w as u32, preview.h as u32);
    let mut png = Vec::new();
    image::ImageEncoder::write_image(
        image::codecs::png::PngEncoder::new_with_quality(
            &mut png,
            image::codecs::png::CompressionType::Fast,
            image::codecs::png::FilterType::Adaptive,
        ),
        &preview.data,
        w,
        h,
        image::ExtendedColorType::Rgb8,
    )
    .ok()?;
    let (mime, bytes) = if png.len() <= PNG_BUDGET {
        ("png", png)
    } else {
        let mut jpeg = Vec::new();
        image::ImageEncoder::write_image(
            image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY),
            &preview.data,
            w,
            h,
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
        ("jpeg", jpeg)
    };
    Some(format!(
        "data:image/{mime};base64,{}",
        base64::engine::general_purpose::STANDARD.encode(bytes)
    ))
}

/// The pixels of the preview of `image`. Read [`preview_url`].
fn preview_pixels(image: &Pixels) -> Option<Pixels> {
    let longest = image.w.max(image.h).max(1);
    if longest > PREVIEW_SIDE {
        let scale = PREVIEW_SIDE as f64 / longest as f64;
        let w = ((image.w as f64 * scale).round() as u32).max(1);
        let h = ((image.h as f64 * scale).round() as u32).max(1);
        let view = image::ImageBuffer::<image::Rgb<u8>, &[u8]>::from_raw(
            image.w as u32,
            image.h as u32,
            &image.data,
        )?;
        let small = image::imageops::resize(&view, w, h, image::imageops::FilterType::CatmullRom);
        return Some(Pixels {
            w: w as usize,
            h: h as usize,
            data: small.into_raw(),
        });
    }
    // Round, so that the copy is between 2/3 and 3/2 of PREVIEW_SIDE.
    let k = ((PREVIEW_SIDE as f64 / longest as f64).round() as usize).max(1);
    if k == 1 {
        return Some(image.clone());
    }
    let mut out = Pixels::new(image.w * k, image.h * k);
    for (y, row) in out.data.chunks_exact_mut(image.w * k * 3).enumerate() {
        let src = &image.data[(y / k) * image.w * 3..][..image.w * 3];
        for (cell, rgb) in row.chunks_exact_mut(k * 3).zip(src.as_chunks::<3>().0) {
            cell.as_chunks_mut::<3>().0.fill(*rgb);
        }
    }
    Some(out)
}

fn name(path: &Path) -> String {
    path.file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

fn folder(path: &Path) -> String {
    path.parent().unwrap_or(Path::new("")).display().to_string()
}

/// The reason of a failed conversion, for people.
pub fn classify(error: &Error, dst: &Path) -> Failure {
    let plain = |reason: &str, detail: String| Failure {
        reason: reason.into(),
        detail,
    };
    match error {
        Error::NotAnImage { .. } => plain(
            "Not an image",
            "The app cannot read this file as an image.".into(),
        ),
        Error::Decode { message, .. } => plain(
            "Damaged image",
            format!("The image data has an error: {message}"),
        ),
        Error::Invalid(message) => plain("Cannot convert", message.clone()),
        Error::Cancelled => plain("Stopped", "The conversion stopped.".into()),
        Error::Io {
            step: Step::Read,
            source,
            ..
        } => match source.kind() {
            io::ErrorKind::NotFound => {
                plain("File not found", "The image is not there any more.".into())
            }
            _ => plain(
                "Cannot read",
                format!("The app cannot read the image: {}.", io_reason(source)),
            ),
        },
        Error::Io { step, source, .. } => {
            let denied = source.kind() == io::ErrorKind::PermissionDenied
                || source.raw_os_error() == Some(30); // EROFS: a read-only disk
            if cfg!(windows) && *step == Step::Rename && denied && dst.is_file() {
                // The app wrote the temporary file, so it can write in the folder. Windows
                // does not let it replace a file that another app has open.
                return plain(
                    "File is in use",
                    format!(
                        "Another app has {} open, for example Excel. Close the file, then retry.",
                        name(dst)
                    ),
                );
            }
            if denied {
                return plain(
                    "Cannot write here",
                    format!(
                        "The app cannot write in {}. Choose another output folder, then retry.",
                        folder(dst)
                    ),
                );
            }
            if source.kind() == io::ErrorKind::NotFound {
                return plain(
                    "Folder not found",
                    format!(
                        "The folder {} is not there. Choose another output folder, then retry.",
                        folder(dst)
                    ),
                );
            }
            plain(
                "Cannot write",
                format!("The app cannot write {}: {}.", name(dst), io_reason(source)),
            )
        }
    }
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

    fn decode(url: &str) -> image::RgbImage {
        let data = url.split_once(";base64,").unwrap().1;
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(data)
            .unwrap();
        image::load_from_memory(&bytes).unwrap().to_rgb8()
    }

    fn pixels(w: usize, h: usize, mut color: impl FnMut(usize, usize) -> [u8; 3]) -> Pixels {
        let mut image = Pixels::new(w, h);
        for y in 0..h {
            for x in 0..w {
                let i = (y * w + x) * 3;
                image.data[i..i + 3].copy_from_slice(&color(x, y));
            }
        }
        image
    }

    #[test]
    fn a_large_image_shrinks_without_aliasing() {
        // A checkerboard of 1-pixel squares: the nearest pixel gives black or white.
        let image = pixels(2048, 1536, |x, y| {
            [if (x + y) % 2 == 0 { 255 } else { 0 }; 3]
        });
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/png;base64,"));
        let preview = decode(&url);
        assert_eq!(preview.dimensions(), (512, 384));
        assert!(
            preview
                .pixels()
                .all(|p| p.0.iter().all(|&c| (120..=135).contains(&c)))
        );
    }

    #[test]
    fn thin_lines_stay_even_at_a_ratio_that_is_not_whole() {
        // Black and white rows of 1 pixel, shrunk by 1.4. A filter that takes each source
        // row for one target row gives rows of pure black or white.
        let image = pixels(717, 538, |_, y| [if y % 2 == 0 { 255 } else { 0 }; 3]);
        let preview = preview_pixels(&image).unwrap();
        assert_eq!((preview.w, preview.h), (512, 384));
        assert!(preview.data.iter().all(|&c| (64..=192).contains(&c)));
    }

    #[test]
    fn a_small_image_grows_with_sharp_cells() {
        let image = pixels(4, 3, |x, y| [(x * 60) as u8, (y * 80) as u8, 7]);
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/png;base64,"));
        let preview = decode(&url);
        assert_eq!(preview.dimensions(), (512, 384));
        for (x, y, p) in preview.enumerate_pixels() {
            assert_eq!(p.0, image.get(x as usize / 128, y as usize / 128));
        }
    }

    #[test]
    fn a_preview_stays_near_the_preview_side() {
        for (w, h, side) in [
            (300, 200, 600),
            (400, 100, 400),
            (342, 10, 342),
            (1, 1, 512),
        ] {
            let preview = preview_pixels(&pixels(w, h, |_, _| [1, 2, 3])).unwrap();
            assert_eq!(preview.w.max(preview.h), side, "{w} x {h}");
            assert_eq!(preview.w * h, preview.h * w);
        }
    }

    #[test]
    fn a_photo_goes_as_a_jpeg() {
        // Noise does not compress, so the PNG is larger than the budget.
        let mut seed = 1u32;
        let image = pixels(800, 600, |_, _| {
            seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let [a, b, c, _] = seed.to_le_bytes();
            [a, b, c]
        });
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/jpeg;base64,"));
        assert_eq!(decode(&url).dimensions(), (512, 384));
    }
}
