//! The outputs of the images: the plan of a new output, and the own output of an image
//! that converts again.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

use super::{Batch, State};
use crate::files::{Existing, Plan, output_key, plan_output};

impl Batch {
    /// The keys of the outputs of all items except `except`.
    fn taken(&self, except: &HashSet<u64>) -> HashSet<String> {
        self.items
            .values()
            .filter(|i| !except.contains(&i.id))
            .map(|i| output_key(&i.dst))
            .collect()
    }

    /// Give each item of `ids` a new output for the current settings, in list order.
    pub(super) fn plan(&mut self, ids: &[u64], existing: Existing) {
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
    pub(super) fn keep_output(&mut self, id: u64) {
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
}
