//! The state of the app: the images, their outputs, and the run.
//!
//! The batch does not use Tauri. Each action changes the state and marks what the page
//! must show again; [`Batch::take_updates`] gives it. A message of the worker changes
//! only its own card and the counts, so a list of many images stays fast.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use pixheet_core::{ConvertResult, Options};
use serde::Serialize;

use crate::files::{Existing, Plan, find_images, key, output_key, plan_output};
use crate::settings::Settings;
use crate::worker::{Failure, Job, Message, Queue};

pub const TITLE: &str = "Pixheet";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Ready,
    Converting,
    Done,
    Failed,
    Skipped,
    Stopped,
}

impl State {
    fn finished(self) -> bool {
        matches!(
            self,
            State::Done | State::Failed | State::Skipped | State::Stopped
        )
    }
}

/// One image of the list.
#[derive(Clone, Debug)]
struct Item {
    id: u64,
    src: PathBuf,
    dst: PathBuf,
    state: State,
    percent: u8,
    result: Option<ConvertResult>,
    bytes: u64,
    failure: Option<Failure>,
    skip: Arc<AtomicBool>,
    /// True when the job can write over a file at `dst`: the policy Replace at the plan,
    /// or Convert again of the own output.
    replace: bool,
}

/// A card as the page shows it.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Card {
    pub id: u64,
    pub name: String,
    pub src: String,
    pub dst: String,
    pub state: State,
    pub percent: u8,
    /// The short status, for example "Done" or "Not an image".
    pub status: String,
    /// The second line, for example "4032 × 3024 → 297 × 223 · 63,758 colors".
    pub detail: String,
}

#[derive(Clone, Copy, Debug, Default, Serialize, PartialEq)]
pub struct Counts {
    pub total: usize,
    pub ready: usize,
    pub converting: usize,
    pub done: usize,
    pub failed: usize,
    pub skipped: usize,
    pub stopped: usize,
}

impl Counts {
    fn add(&mut self, state: State, n: isize) {
        let slot = match state {
            State::Ready => &mut self.ready,
            State::Converting => &mut self.converting,
            State::Done => &mut self.done,
            State::Failed => &mut self.failed,
            State::Skipped => &mut self.skipped,
            State::Stopped => &mut self.stopped,
        };
        *slot = slot.saturating_add_signed(n);
        self.total = self.total.saturating_add_signed(n);
    }
}

/// The state of the window apart from the cards.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Summary {
    pub title: String,
    pub counts: Counts,
    pub running: bool,
    pub stopping: bool,
    /// The main line of the footer.
    pub text: String,
    pub eta: String,
    /// The progress of the run, 0 to 1.
    pub bar: f64,
    /// The folder of all done outputs, when they are in one folder.
    pub folder: Option<String>,
    pub settings: Settings,
}

/// What the page must show again since the last call.
#[derive(Debug, Serialize)]
pub struct Updates {
    /// All cards, after cards came, went or moved. None when only cards changed.
    pub cards: Option<Vec<Card>>,
    pub changed: Vec<Card>,
    /// New previews: (card id, PNG or JPEG data URL).
    pub previews: Vec<(u64, String)>,
    pub summary: Summary,
    /// A short note for a toast, for example about files that are not images.
    pub toast: Option<String>,
    /// An error for a dialog.
    pub error: Option<String>,
}

/// Starts a worker thread. The app gives a real thread; the tests record the call.
pub type Spawner = Box<dyn FnMut(Arc<Queue>, Arc<Mutex<Options>>, Arc<AtomicBool>, u64) + Send>;

pub struct Batch {
    items: HashMap<u64, Item>,
    order: Vec<u64>,
    last_id: u64,
    counts: Counts,
    settings: Settings,
    options: Arc<Mutex<Options>>,
    queue: Arc<Queue>,
    spawn: Spawner,
    worker: u64,
    running: bool,
    cancel: Arc<AtomicBool>,
    run_ids: HashSet<u64>,
    run_finished: usize,
    run_started: Instant,
    run_stopped: bool,
    previews: HashMap<u64, String>,
    new_previews: Vec<u64>,
    pub closing: bool,
    dirty_list: bool,
    changed: Vec<u64>,
    changed_set: HashSet<u64>,
    toast: Option<String>,
    error: Option<String>,
}

fn plural(count: usize, word: &str) -> String {
    let n = grouped(count);
    if count == 1 {
        format!("{n} {word}")
    } else {
        format!("{n} {word}s")
    }
}

pub fn grouped(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// A file size for people, for example "712 KB" or "1.9 MB".
pub fn size_text(bytes: u64) -> String {
    match bytes {
        b if b < 1024 => format!("{b} bytes"),
        b if b < 1024 * 1024 => format!("{} KB", (b as f64 / 1024.0).round()),
        b => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
    }
}

/// The time left, for example "About 40 s left", after 2 or more files.
pub fn eta_text(seconds: f64, finished: usize, left: usize) -> String {
    if finished < 2 || left == 0 || seconds <= 0.0 {
        return String::new();
    }
    let rest = seconds / finished as f64 * left as f64;
    if rest < 57.5 {
        format!("About {} s left", ((rest / 5.0).round() as u64 * 5).max(5))
    } else if rest < 3570.0 {
        format!("About {} min left", ((rest / 60.0).round() as u64).max(1))
    } else {
        format!("About {} h left", (rest / 3600.0).round() as u64)
    }
}

impl Batch {
    pub fn new(spawn: Spawner, settings: Settings) -> Self {
        let options = settings.options().unwrap_or_default();
        Self {
            items: HashMap::new(),
            order: Vec::new(),
            last_id: 0,
            counts: Counts::default(),
            settings,
            options: Arc::new(Mutex::new(options)),
            queue: Arc::new(Queue::default()),
            spawn,
            worker: 0,
            running: false,
            cancel: Arc::new(AtomicBool::new(false)),
            run_ids: HashSet::new(),
            run_finished: 0,
            run_started: Instant::now(),
            run_stopped: false,
            previews: HashMap::new(),
            new_previews: Vec::new(),
            closing: false,
            dirty_list: true,
            changed: Vec::new(),
            changed_set: HashSet::new(),
            toast: None,
            error: None,
        }
    }

    pub fn running(&self) -> bool {
        self.running
    }

    pub fn settings(&self) -> &Settings {
        &self.settings
    }

    fn mark(&mut self, id: u64) {
        if self.changed_set.insert(id) {
            self.changed.push(id);
        }
    }

    /// Give the item `id` a new state, and count the change.
    fn set_state(&mut self, id: u64, state: State) {
        let Some(item) = self.items.get_mut(&id) else {
            return;
        };
        let old = item.state;
        if old != state {
            item.state = state;
            self.counts.add(old, -1);
            self.counts.add(state, 1);
            if self.run_ids.contains(&id) && state.finished() && !old.finished() {
                self.run_finished += 1;
            }
            // An image that converts again leaves the run, so start() can queue it.
            if old.finished() && !state.finished() && self.run_ids.remove(&id) {
                self.run_finished -= 1;
            }
        }
        self.mark(id);
    }

    /// The keys of the outputs of all items except `except`.
    fn taken(&self, except: &HashSet<u64>) -> HashSet<String> {
        self.items
            .values()
            .filter(|i| !except.contains(&i.id))
            .map(|i| output_key(&i.dst))
            .collect()
    }

    /// Give each item of `ids` a new output for the current settings, in list order.
    fn plan(&mut self, ids: &[u64], existing: Existing) {
        let set: HashSet<u64> = ids.iter().copied().collect();
        let mut taken = self.taken(&set);
        for &id in ids {
            let Some(item) = self.items.get(&id) else {
                continue;
            };
            let plan = plan_output(
                &item.src,
                self.settings.out_dir.as_deref(),
                existing,
                &taken,
            );
            let (dst, state) = match plan {
                Plan::Write(dst) => (dst, State::Ready),
                Plan::Skip(dst) => (dst, State::Skipped),
            };
            taken.insert(output_key(&dst));
            if let Some(item) = self.items.get_mut(&id) {
                item.dst = dst;
                item.replace = existing == Existing::Replace;
                item.percent = 0;
                item.result = None;
                item.failure = None;
                item.skip = Arc::new(AtomicBool::new(false));
            }
            self.set_state(id, state);
        }
    }

    /// Make the finished item `id` ready again, with its own output. The job can write over
    /// that output: the user asked to convert the image again.
    fn keep_output(&mut self, id: u64) {
        if let Some(item) = self.items.get_mut(&id) {
            item.replace = true;
            item.percent = 0;
            item.result = None;
            item.failure = None;
            item.skip = Arc::new(AtomicBool::new(false));
        }
        self.previews.remove(&id);
        self.set_state(id, State::Ready);
    }

    // --- Actions of the user ------------------------------------------------------------

    /// Add the images of the dropped files and folders.
    ///
    /// An image that is in the list and finished converts again with the current
    /// settings. An image that waits or converts already does not come twice.
    pub fn add(&mut self, paths: &[PathBuf]) {
        if self.closing {
            return;
        }
        let found = find_images(paths, self.settings.subfolders);
        let by_key: HashMap<String, u64> =
            self.items.values().map(|i| (key(&i.src), i.id)).collect();
        let mut fresh = Vec::new();
        let mut again = Vec::new();
        let mut duplicates = 0;
        for path in found.images {
            match by_key.get(&key(&path)) {
                None => {
                    self.last_id += 1;
                    let id = self.last_id;
                    let item = Item {
                        id,
                        src: path,
                        dst: PathBuf::new(),
                        state: State::Ready,
                        percent: 0,
                        result: None,
                        bytes: 0,
                        failure: None,
                        skip: Arc::new(AtomicBool::new(false)),
                        replace: false,
                    };
                    self.items.insert(id, item);
                    self.order.push(id);
                    self.counts.add(State::Ready, 1);
                    fresh.push(id);
                }
                Some(&id) if self.items[&id].state.finished() => again.push(id),
                Some(_) => duplicates += 1,
            }
        }
        if !fresh.is_empty() {
            self.dirty_list = true;
        }
        let mut ids = fresh;
        ids.extend(&again);
        self.plan(&ids, self.settings.existing);
        let mut notes = Vec::new();
        if ids.is_empty() && duplicates == 0 {
            notes.push("The drop has no images.".to_string());
        }
        if found.ignored > 0 {
            let what = if found.ignored == 1 {
                "file that is not an image"
            } else {
                "files that are not images"
            };
            notes.push(format!("Ignored {} {what}.", grouped(found.ignored)));
        }
        if duplicates > 0 {
            let verb = if duplicates == 1 { "is" } else { "are" };
            notes.push(format!(
                "{} {verb} in the list already.",
                plural(duplicates, "image")
            ));
        }
        if found.missing > 0 {
            notes.push(format!(
                "The app cannot read {}.",
                plural(found.missing, "dropped item")
            ));
        }
        if !notes.is_empty() {
            self.toast = Some(notes.join(" "));
        }
        if self.settings.auto_start || self.running {
            self.start();
        }
    }

    /// Change the settings. The images that did not start get the new outputs.
    pub fn set_settings(&mut self, settings: Settings) {
        let options = match settings.options() {
            Ok(options) => options,
            Err(e) => {
                self.error = Some(e);
                return;
            }
        };
        let outputs_change = settings.out_dir != self.settings.out_dir
            || settings.existing != self.settings.existing;
        self.settings = settings;
        *self.options.lock().unwrap_or_else(|e| e.into_inner()) = options;
        if outputs_change {
            // The jobs that wait in the queue leave the run, and get new outputs below. The
            // job that the reader has already keeps its output. The worker does not write
            // it over a file that appeared, unless its policy was Replace.
            if self.running {
                for job in self.queue.drain() {
                    self.run_ids.remove(&job.id);
                }
            }
            let ready: Vec<u64> = self
                .order
                .iter()
                .copied()
                .filter(|id| matches!(self.items[id].state, State::Ready | State::Skipped))
                .filter(|id| !self.queued(*id))
                .collect();
            self.plan(&ready, self.settings.existing);
            if self.running {
                self.start();
            }
        }
    }

    /// True when the worker can have the job of `id` already (it waits in the reader).
    fn queued(&self, id: u64) -> bool {
        self.running && self.run_ids.contains(&id)
    }

    /// Convert the images that are ready.
    pub fn start(&mut self) {
        if self.closing {
            return;
        }
        if !self.running {
            self.run_ids.clear();
            self.run_finished = 0;
        }
        // A file can appear after the plan, for example before a click on Convert, or
        // during a stop. Plan these outputs again with the current policy.
        let appeared: Vec<u64> = self
            .order
            .iter()
            .copied()
            .filter(|id| !self.run_ids.contains(id))
            .filter(|id| {
                let item = &self.items[id];
                item.state == State::Ready && !item.replace && item.dst.exists()
            })
            .collect();
        self.plan(&appeared, self.settings.existing);
        let ready: Vec<u64> = self
            .order
            .iter()
            .copied()
            .filter(|id| self.items[id].state == State::Ready && !self.run_ids.contains(id))
            .collect();
        if ready.is_empty() {
            return;
        }
        if !self.running {
            self.run_started = Instant::now();
            self.run_stopped = false;
        }
        let jobs: Vec<Job> = ready
            .iter()
            .map(|id| {
                let item = &self.items[id];
                Job {
                    id: *id,
                    src: item.src.clone(),
                    dst: item.dst.clone(),
                    skip: Arc::clone(&item.skip),
                    replace: item.replace,
                }
            })
            .collect();
        self.run_ids.extend(&ready);
        self.queue.put(jobs);
        if !self.running {
            self.start_worker();
        }
    }

    fn start_worker(&mut self) {
        self.worker += 1;
        self.cancel = Arc::new(AtomicBool::new(false));
        self.running = true;
        (self.spawn)(
            Arc::clone(&self.queue),
            Arc::clone(&self.options),
            Arc::clone(&self.cancel),
            self.worker,
        );
    }

    /// Stop the run. The current image stops; the others stay ready.
    pub fn stop(&mut self) {
        if self.running {
            self.cancel.store(true, Ordering::SeqCst);
        }
    }

    /// Convert the failed and the stopped images again.
    pub fn retry_failed(&mut self) {
        let ids: Vec<u64> = self
            .order
            .iter()
            .copied()
            .filter(|id| matches!(self.items[id].state, State::Failed | State::Stopped))
            .collect();
        self.plan(&ids, self.settings.existing);
        self.start();
    }

    /// Convert the finished images `ids` again with the current settings.
    ///
    /// A done or skipped image writes over its own output: the user asked for it. A failed
    /// or stopped image gets a new output with the current policy, as Retry failed does.
    pub fn convert_again(&mut self, ids: &[u64]) {
        let mut retry = Vec::new();
        for &id in ids {
            match self.items.get(&id).map(|i| i.state) {
                Some(State::Done | State::Skipped) => self.keep_output(id),
                Some(State::Failed | State::Stopped) => retry.push(id),
                _ => {}
            }
        }
        self.plan(&retry, self.settings.existing);
        self.start();
    }

    /// Stop the image `id` if it converts. The run goes on with the next image.
    pub fn skip(&mut self, id: u64) {
        if let Some(item) = self.items.get(&id) {
            item.skip.store(true, Ordering::SeqCst);
        }
    }

    /// Remove the images `ids`. An image that converts stops first.
    pub fn remove(&mut self, ids: &[u64]) {
        let ids: Vec<u64> = ids
            .iter()
            .copied()
            .filter(|id| {
                self.items
                    .get(id)
                    .is_some_and(|i| i.state != State::Converting)
            })
            .collect();
        if ids.is_empty() {
            return;
        }
        let set: HashSet<u64> = ids.iter().copied().collect();
        self.queue.remove(&set);
        for id in &ids {
            if let Some(item) = self.items.remove(id) {
                item.skip.store(true, Ordering::SeqCst); // the reader can have it already
                self.counts.add(item.state, -1);
                if self.run_ids.remove(id) && item.state.finished() {
                    self.run_finished -= 1;
                }
            }
            self.previews.remove(id);
        }
        self.order.retain(|id| !set.contains(id));
        self.dirty_list = true;
    }

    /// Remove the finished images. Without a run, remove all images.
    pub fn clear(&mut self) {
        let ids: Vec<u64> = self
            .order
            .iter()
            .copied()
            .filter(|id| !self.running || self.items[id].state.finished())
            .collect();
        self.remove(&ids);
        if !self.running {
            self.queue.clear();
            self.run_ids.clear();
            self.run_finished = 0;
        }
    }

    // --- Messages of the worker ----------------------------------------------------------

    /// Put a message of the worker `worker` into its card. A message of an older worker
    /// has no effect.
    pub fn handle(&mut self, worker: u64, message: Message) {
        if worker != self.worker {
            return;
        }
        match message {
            Message::Finished { stopped } => self.finish(stopped),
            Message::Started { id } => {
                self.set_state(id, State::Converting);
            }
            Message::Preview { id, url } => {
                if self.items.contains_key(&id) {
                    self.previews.insert(id, url);
                    self.new_previews.push(id);
                }
            }
            Message::Progress { id, percent } => {
                if let Some(item) = self.items.get_mut(&id) {
                    item.percent = percent;
                    self.mark(id);
                }
            }
            Message::Done { id, result, bytes } => {
                if let Some(item) = self.items.get_mut(&id) {
                    item.result = Some(result);
                    item.bytes = bytes;
                    item.percent = 100;
                }
                self.set_state(id, State::Done);
            }
            Message::Failed { id, failure } => {
                if let Some(item) = self.items.get_mut(&id) {
                    item.failure = Some(failure);
                }
                self.set_state(id, State::Failed);
            }
            Message::Stopped { id } => {
                if let Some(item) = self.items.get_mut(&id) {
                    item.percent = 0;
                }
                self.previews.remove(&id);
                self.set_state(id, State::Stopped);
            }
        }
    }

    fn finish(&mut self, stopped: bool) {
        self.running = false;
        if self.closing {
            return;
        }
        // Images that came during the last moments of the run start a new worker.
        if !stopped && !self.queue.is_empty() {
            self.start_worker();
            return;
        }
        self.queue.clear();
        self.run_stopped = stopped;
        // A job that the reader had, but that did not start, waits for the next run.
        let waiting: Vec<u64> = self
            .run_ids
            .iter()
            .copied()
            .filter(|id| self.items.get(id).is_some_and(|i| i.state == State::Ready))
            .collect();
        for id in waiting {
            self.run_ids.remove(&id);
        }
    }

    // --- Targets of the buttons -------------------------------------------------------------

    /// The output of a done image, for Open and Show in folder.
    pub fn output(&self, id: u64) -> Option<PathBuf> {
        self.items
            .get(&id)
            .filter(|i| i.state == State::Done)
            .map(|i| i.dst.clone())
    }

    /// The folder of all done outputs, when there is one such folder.
    pub fn done_folder(&self) -> Option<PathBuf> {
        let mut folders = self
            .items
            .values()
            .filter(|i| i.state == State::Done)
            .map(|i| i.dst.parent().unwrap_or(Path::new("")).to_path_buf());
        let first = folders.next()?;
        let k = key(&first);
        folders.all(|f| key(&f) == k).then_some(first)
    }

    // --- Views ----------------------------------------------------------------------------

    fn card(&self, item: &Item) -> Card {
        let name = item
            .src
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let dst_name = item
            .dst
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let stem = item.src.file_stem().unwrap_or_default().to_string_lossy();
        let renamed = dst_name != format!("{stem}.xlsx");
        let (status, detail) = match item.state {
            State::Ready => (
                "Ready".to_string(),
                if renamed {
                    format!("Writes {dst_name}")
                } else {
                    String::new()
                },
            ),
            State::Converting => (
                if item.percent > 0 {
                    format!("Writing {}%", item.percent)
                } else {
                    "Reading".to_string()
                },
                String::new(),
            ),
            State::Done => {
                let detail = item.result.map_or_else(String::new, |r| {
                    format!(
                        "{} × {} → {} × {}\n{} colors · {}",
                        r.original_size.0,
                        r.original_size.1,
                        r.size.0,
                        r.size.1,
                        grouped(r.colors),
                        size_text(item.bytes)
                    )
                });
                ("Done".to_string(), detail)
            }
            State::Failed => {
                let failure = item.failure.clone().unwrap_or(Failure {
                    reason: "Failed".into(),
                    detail: String::new(),
                });
                (failure.reason, failure.detail)
            }
            State::Skipped => ("Skipped".to_string(), format!("{dst_name} exists")),
            State::Stopped => (
                "Stopped".to_string(),
                "The app did not write the file.".to_string(),
            ),
        };
        Card {
            id: item.id,
            name,
            src: item.src.display().to_string(),
            dst: item.dst.display().to_string(),
            state: item.state,
            percent: item.percent,
            status,
            detail,
        }
    }

    fn summary(&self) -> Summary {
        let c = self.counts;
        let run_total = self.run_ids.len();
        let (text, eta, bar, title) = if self.running {
            let current = self
                .run_ids
                .iter()
                .filter_map(|id| self.items.get(id))
                .find(|i| i.state == State::Converting)
                .map_or(0.0, |i| f64::from(i.percent) / 100.0);
            let index = (self.run_finished + 1).min(run_total.max(1));
            let text = if self.cancel.load(Ordering::SeqCst) {
                "Stopping".to_string()
            } else {
                format!("Converting {} of {}", grouped(index), grouped(run_total))
            };
            let left = run_total.saturating_sub(self.run_finished);
            (
                text,
                eta_text(
                    self.run_started.elapsed().as_secs_f64(),
                    self.run_finished,
                    left,
                ),
                ((self.run_finished as f64 + current) / run_total.max(1) as f64).min(1.0),
                format!("{TITLE} · {} of {}", grouped(index), grouped(run_total)),
            )
        } else {
            let text = if c.total == 0 {
                String::new()
            } else if c.ready == c.total {
                format!("{} ready", plural(c.ready, "image"))
            } else {
                let mut parts = vec![if c.done == c.total {
                    format!("Converted {}", plural(c.total, "image"))
                } else {
                    format!(
                        "Converted {} of {}",
                        grouped(c.done),
                        plural(c.total, "image")
                    )
                }];
                for (n, word) in [
                    (c.failed, "failed"),
                    (c.skipped, "skipped"),
                    (c.stopped, "stopped"),
                    (c.ready, "ready"),
                ] {
                    if n > 0 {
                        parts.push(format!("{} {word}", grouped(n)));
                    }
                }
                let text = parts.join(" · ");
                if self.run_stopped {
                    format!("Stopped · {text}")
                } else {
                    text
                }
            };
            let bar = if c.total > 0 && !self.run_stopped {
                (c.total - c.ready) as f64 / c.total as f64
            } else {
                0.0
            };
            (text, String::new(), bar, TITLE.to_string())
        };
        Summary {
            title,
            counts: c,
            running: self.running,
            stopping: self.running && self.cancel.load(Ordering::SeqCst),
            text,
            eta,
            bar,
            folder: self.done_folder().map(|f| f.display().to_string()),
            settings: self.settings.clone(),
        }
    }

    /// Send all cards and previews with the next updates, for example after a reload.
    pub fn mark_all(&mut self) {
        self.dirty_list = true;
        self.new_previews = self
            .order
            .iter()
            .copied()
            .filter(|id| self.previews.contains_key(id))
            .collect();
    }

    /// What the page must show again since the last call.
    pub fn take_updates(&mut self) -> Updates {
        let cards = std::mem::take(&mut self.dirty_list).then(|| {
            self.order
                .iter()
                .map(|id| self.card(&self.items[id]))
                .collect()
        });
        let changed = if cards.is_some() {
            Vec::new()
        } else {
            self.changed
                .iter()
                .filter_map(|id| self.items.get(id))
                .map(|i| self.card(i))
                .collect()
        };
        self.changed.clear();
        self.changed_set.clear();
        let previews = std::mem::take(&mut self.new_previews)
            .into_iter()
            .filter_map(|id| self.previews.get(&id).map(|url| (id, url.clone())))
            .collect();
        Updates {
            cards,
            changed,
            previews,
            summary: self.summary(),
            toast: self.toast.take(),
            error: self.error.take(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    type RunArgs = (Arc<Queue>, Arc<Mutex<Options>>, Arc<AtomicBool>, u64);

    /// A spawner that keeps the arguments of each run. run_all() then runs the worker on
    /// the thread of the test.
    fn deferred() -> (Spawner, Arc<Mutex<Vec<RunArgs>>>) {
        let runs = Arc::new(Mutex::new(Vec::new()));
        let keep = Arc::clone(&runs);
        let spawn: Spawner = Box::new(move |q, o, c, w| keep.lock().unwrap().push((q, o, c, w)));
        (spawn, runs)
    }

    fn run_all(batch: &mut Batch, runs: &Arc<Mutex<Vec<RunArgs>>>) {
        loop {
            let next = runs.lock().unwrap().pop();
            let Some((queue, options, cancel, worker)) = next else {
                return;
            };
            let mut messages = Vec::new();
            crate::worker::run(queue, options, cancel, &mut |m| messages.push(m));
            for message in messages {
                batch.handle(worker, message);
            }
        }
    }

    fn png(path: &Path, w: u32, h: u32) {
        image::RgbImage::from_fn(w, h, |x, y| image::Rgb([x as u8 * 40, y as u8 * 50, 7]))
            .save(path)
            .unwrap();
    }

    fn images(dir: &Path, count: usize) -> Vec<PathBuf> {
        (0..count)
            .map(|i| {
                let path = dir.join(format!("img{i}.png"));
                png(&path, 4, 3);
                path
            })
            .collect()
    }

    fn manual() -> Settings {
        Settings {
            auto_start: false,
            ..Settings::default()
        }
    }

    #[test]
    fn a_drop_adds_cards_and_starts_one_run() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(&images(dir.path(), 3));
        let updates = batch.take_updates();
        let cards = updates.cards.expect("all cards");
        assert_eq!(cards.len(), 3);
        assert!(
            cards
                .iter()
                .all(|c| c.state == State::Ready && c.status == "Ready")
        );
        assert_eq!(runs.lock().unwrap().len(), 1);
        assert!(updates.summary.running);
        assert_eq!(updates.summary.text, "Converting 1 of 3");
    }

    #[test]
    fn without_auto_start_the_cards_wait_for_start() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(&images(dir.path(), 2));
        assert!(runs.lock().unwrap().is_empty());
        assert_eq!(batch.take_updates().summary.text, "2 images ready");
        batch.start();
        assert_eq!(runs.lock().unwrap().len(), 1);
    }

    #[test]
    fn a_run_converts_and_gives_previews() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        let paths = images(dir.path(), 3);
        batch.add(&paths);
        batch.take_updates();
        run_all(&mut batch, &runs);
        let updates = batch.take_updates();
        assert_eq!(batch.counts.done, 3);
        assert_eq!(updates.previews.len(), 3);
        assert!(
            updates
                .previews
                .iter()
                .all(|(_, url)| url.starts_with("data:image/png;base64,"))
        );
        assert_eq!(updates.summary.text, "Converted 3 images");
        assert_eq!(
            updates.summary.folder.as_deref(),
            Some(dir.path().to_str().unwrap())
        );
        assert!(paths.iter().all(|p| p.with_extension("xlsx").is_file()));
        let done = &updates.changed[0];
        assert_eq!(done.status, "Done");
        assert!(
            done.detail.starts_with("4 × 3 → 4 × 3\n12 colors · "),
            "{}",
            done.detail
        );
    }

    #[test]
    fn a_tick_of_500_cards_changes_one_card() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(&images(dir.path(), 500));
        batch.take_updates();
        let worker = batch.worker;
        let id = batch.order[250];
        batch.handle(worker, Message::Started { id });
        batch.handle(worker, Message::Progress { id, percent: 40 });
        let updates = batch.take_updates();
        assert!(updates.cards.is_none());
        assert_eq!(updates.changed.len(), 1);
        assert_eq!(updates.changed[0].status, "Writing 40%");
        assert_eq!(batch.counts.converting, 1);
        assert_eq!(batch.counts.ready, 499);
    }

    #[test]
    fn two_images_with_one_name_get_two_outputs() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("photo.png"), dir.path().join("photo.bmp"));
        png(&a, 2, 2);
        png(&b, 2, 2);
        let (spawn, _) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(&[a, b]);
        let cards = batch.take_updates().cards.unwrap();
        assert!(cards[0].dst.ends_with("photo.xlsx"));
        assert!(cards[1].dst.ends_with("photo (2).xlsx"));
        assert_eq!(cards[1].detail, "Writes photo (2).xlsx");
    }

    #[test]
    fn the_policy_skip_keeps_an_existing_output() {
        let dir = tempfile::tempdir().unwrap();
        let paths = images(dir.path(), 2);
        std::fs::write(paths[0].with_extension("xlsx"), b"old").unwrap();
        let (spawn, runs) = deferred();
        let settings = Settings {
            existing: Existing::Skip,
            ..Settings::default()
        };
        let mut batch = Batch::new(spawn, settings);
        batch.add(&paths);
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.skipped, 1);
        assert_eq!(batch.counts.done, 1);
        assert_eq!(
            std::fs::read(paths[0].with_extension("xlsx")).unwrap(),
            b"old"
        );
        let text = batch.take_updates().summary.text;
        assert_eq!(text, "Converted 1 of 2 images · 1 skipped");
    }

    #[test]
    fn a_failed_image_tells_why_and_can_retry() {
        let dir = tempfile::tempdir().unwrap();
        let bad = dir.path().join("bad.png");
        std::fs::write(&bad, b"not an image").unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(std::slice::from_ref(&bad));
        batch.take_updates();
        run_all(&mut batch, &runs);
        let updates = batch.take_updates();
        let card = &updates.changed[0];
        assert_eq!(card.state, State::Failed);
        assert_eq!(card.status, "Not an image");
        assert_eq!(updates.summary.text, "Converted 0 of 1 image · 1 failed");
        png(&bad, 2, 2);
        batch.retry_failed();
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 1);
    }

    #[test]
    fn stop_leaves_the_other_images_ready() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(&images(dir.path(), 3));
        let worker = batch.worker;
        let first = batch.order[0];
        batch.handle(worker, Message::Started { id: first });
        batch.stop();
        assert!(batch.take_updates().summary.stopping);
        batch.handle(worker, Message::Stopped { id: first });
        batch.handle(worker, Message::Finished { stopped: true });
        let updates = batch.take_updates();
        assert!(!updates.summary.running);
        assert_eq!(batch.counts.ready, 2);
        assert_eq!(
            updates.summary.text,
            "Stopped · Converted 0 of 3 images · 1 stopped · 2 ready"
        );
        // The next start queues the images again, also the ones of the old run.
        runs.lock().unwrap().clear();
        batch.retry_failed();
        assert_eq!(batch.counts.ready, 3);
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 3);
    }

    #[test]
    fn a_message_of_an_old_worker_has_no_effect() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(&images(dir.path(), 1));
        let id = batch.order[0];
        batch.handle(batch.worker + 1, Message::Started { id });
        assert_eq!(batch.counts.converting, 0);
    }

    #[test]
    fn a_drop_tells_about_other_files_and_duplicates() {
        let dir = tempfile::tempdir().unwrap();
        let paths = images(dir.path(), 1);
        std::fs::write(dir.path().join("notes.txt"), b"x").unwrap();
        let (spawn, _) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(&[dir.path().to_path_buf()]);
        assert_eq!(
            batch.take_updates().toast.as_deref(),
            Some("Ignored 1 file that is not an image.")
        );
        batch.add(&paths);
        assert_eq!(
            batch.take_updates().toast.as_deref(),
            Some("1 image is in the list already.")
        );
        batch.add(&[dir.path().join("notes.txt")]);
        let toast = batch.take_updates().toast.unwrap();
        assert_eq!(
            toast,
            "The drop has no images. Ignored 1 file that is not an image."
        );
        std::fs::write(dir.path().join("more.txt"), b"x").unwrap();
        let missing = dir.path().join("gone.png");
        batch.add(&[
            dir.path().join("notes.txt"),
            dir.path().join("more.txt"),
            missing,
        ]);
        let toast = batch.take_updates().toast.unwrap();
        assert_eq!(
            toast,
            "The drop has no images. Ignored 2 files that are not images. \
             The app cannot read 1 dropped item."
        );
    }

    #[test]
    fn the_numbers_of_the_summary_are_for_people() {
        assert_eq!(grouped(999), "999");
        assert_eq!(grouped(1_234_567), "1,234,567");
        assert_eq!(size_text(1000), "1000 bytes");
        assert_eq!(size_text(712 * 1024), "712 KB");
        assert_eq!(size_text(2_000_000), "1.9 MB");
        // The time left comes only after 2 images, from the mean time of an image.
        assert_eq!(eta_text(10.0, 1, 5), "");
        assert_eq!(eta_text(10.0, 2, 0), "");
        assert_eq!(eta_text(1.0, 2, 1), "About 5 s left");
        assert_eq!(eta_text(10.0, 2, 4), "About 20 s left");
        assert_eq!(eta_text(60.0, 2, 4), "About 2 min left");
        assert_eq!(eta_text(3600.0, 2, 4), "About 2 h left");
    }

    #[test]
    fn a_new_output_folder_moves_the_ready_outputs() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("out");
        let (spawn, _) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(&images(dir.path(), 2));
        batch.take_updates();
        batch.set_settings(Settings {
            out_dir: Some(out.clone()),
            ..manual()
        });
        let updates = batch.take_updates();
        assert_eq!(updates.changed.len(), 2);
        assert!(
            updates
                .changed
                .iter()
                .all(|c| Path::new(&c.dst).parent() == Some(out.as_path()))
        );
        batch.set_settings(Settings {
            background: "nope".into(),
            ..manual()
        });
        assert!(
            batch
                .take_updates()
                .error
                .unwrap()
                .starts_with("invalid background color")
        );
    }

    #[test]
    fn convert_again_writes_over_its_own_output() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        let paths = images(dir.path(), 1);
        batch.add(&paths);
        run_all(&mut batch, &runs);
        let id = batch.order[0];
        batch.convert_again(&[id]);
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 1);
        assert!(!dir.path().join("img0 (2).xlsx").exists());
    }

    #[test]
    fn convert_again_keeps_the_assigned_output() {
        // Keep both gave photo (2).xlsx, because an unrelated photo.xlsx exists.
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("photo.png");
        png(&src, 3, 2);
        let other = dir.path().join("photo.xlsx");
        std::fs::write(&other, b"other").unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(std::slice::from_ref(&src));
        run_all(&mut batch, &runs);
        let id = batch.order[0];
        batch.convert_again(&[id]);
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 1);
        assert_eq!(std::fs::read(&other).unwrap(), b"other");
        assert_eq!(batch.items[&id].dst, dir.path().join("photo (2).xlsx"));
    }

    #[test]
    fn start_plans_again_an_output_that_appeared() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("photo.png");
        png(&src, 3, 2);
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(std::slice::from_ref(&src));
        let id = batch.order[0];
        assert_eq!(batch.items[&id].dst, dir.path().join("photo.xlsx"));
        // The file appears before the click on Convert.
        std::fs::write(dir.path().join("photo.xlsx"), b"other").unwrap();
        batch.start();
        assert_eq!(batch.items[&id].dst, dir.path().join("photo (2).xlsx"));
        run_all(&mut batch, &runs);
        assert_eq!(
            std::fs::read(dir.path().join("photo.xlsx")).unwrap(),
            b"other"
        );
        // With Skip, the image does not convert.
        let mut batch = Batch::new(
            deferred().0,
            Settings {
                existing: Existing::Skip,
                ..manual()
            },
        );
        batch.add(std::slice::from_ref(&src));
        std::fs::remove_file(dir.path().join("photo (2).xlsx")).unwrap();
        batch.start();
        assert_eq!(batch.counts.skipped, 1);
    }

    #[test]
    fn the_worker_does_not_write_over_a_file_that_appeared() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("photo.png");
        png(&src, 3, 2);
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        batch.add(std::slice::from_ref(&src));
        batch.take_updates();
        // The job waits in the queue, and then the file appears.
        std::fs::write(dir.path().join("photo.xlsx"), b"other").unwrap();
        run_all(&mut batch, &runs);
        let card = batch.take_updates().changed.pop().unwrap();
        assert_eq!(card.status, "File exists");
        assert_eq!(
            std::fs::read(dir.path().join("photo.xlsx")).unwrap(),
            b"other"
        );
        batch.retry_failed();
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 1);
        assert!(dir.path().join("photo (2).xlsx").is_file());
    }

    #[test]
    fn a_new_output_folder_during_a_run_moves_the_waiting_jobs() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("out");
        std::fs::create_dir(&out).unwrap();
        let (spawn, runs) = deferred();
        let mut batch = Batch::new(spawn, Settings::default());
        let paths = images(dir.path(), 3);
        batch.add(&paths);
        assert!(batch.running());
        batch.set_settings(Settings {
            out_dir: Some(out.clone()),
            ..Settings::default()
        });
        run_all(&mut batch, &runs);
        assert_eq!(batch.counts.done, 3);
        for path in &paths {
            assert!(
                out.join(path.with_extension("xlsx").file_name().unwrap())
                    .is_file()
            );
            assert!(!path.with_extension("xlsx").exists());
        }
    }

    #[test]
    fn clear_removes_all_cards_without_a_run() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _runs) = deferred();
        let mut batch = Batch::new(spawn, manual());
        batch.add(&images(dir.path(), 2));
        assert_eq!(batch.counts.ready, 2);
        batch.clear();
        let updates = batch.take_updates();
        assert_eq!(updates.cards.map(|c| c.len()), Some(0));
        assert_eq!(updates.summary.counts.total, 0);
    }
}
