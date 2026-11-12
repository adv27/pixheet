//! What the page shows: the cards, the summary, and the updates since the last call.

use std::sync::atomic::Ordering;

use serde::Serialize;

use super::text::{eta_text, grouped, plural, size_text};
use super::{Batch, Counts, Item, State, TITLE};
use crate::settings::Settings;
use crate::worker::Failure;

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

impl Batch {
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
