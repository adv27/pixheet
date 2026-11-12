//! The actions of the user on the list: add, settings, retry, convert again, skip,
//! remove and clear.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use super::text::{grouped, plural};
use super::{Batch, Item, State};
use crate::files::{find_images, key};
use crate::settings::Settings;

impl Batch {
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
}
