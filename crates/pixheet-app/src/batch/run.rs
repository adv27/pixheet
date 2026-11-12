//! The run: start and stop the worker, and put its messages into the cards.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use super::{Batch, State};
use crate::worker::{Job, Message};

impl Batch {
    /// True when the worker can have the job of `id` already (it waits in the reader).
    pub(super) fn queued(&self, id: u64) -> bool {
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
}
