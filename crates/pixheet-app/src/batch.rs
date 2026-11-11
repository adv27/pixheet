//! The state of the app: the images, their outputs, and the run.
//!
//! The batch does not use Tauri. Each action changes the state and marks what the page
//! must show again; [`Batch::take_updates`] gives it. A message of the worker changes
//! only its own card and the counts, so a list of many images stays fast.
//!
//! This file holds the types and the state. The child modules add the parts of
//! `impl Batch`:
//!
//! - `actions`: the actions of the user on the list.
//! - `outputs`: the plan of the output of each image.
//! - `run`: the start and the stop of the worker, and its messages.
//! - `view`: the cards, the summary and the updates for the page.

mod actions;
mod outputs;
mod run;
mod text;
mod view;

pub use view::Updates;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use pixheet_core::{ConvertResult, Options};
use serde::Serialize;

use crate::files::key;
use crate::settings::Settings;
use crate::worker::{Failure, Queue};

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
}

#[cfg(test)]
mod tests;
