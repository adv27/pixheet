//! The logic of the window, without Tauri: a port of the `App` class of `gui.py`.
//!
//! The controller keeps the rows, the tallies, the queue of jobs and the state of the
//! run. Each action changes the state and marks what the window must show again. The
//! Tauri layer calls [`Controller::take_updates`] after each action and sends the
//! result to the web page. Thus the tests can drive the controller without a window.
//!
//! A message of the worker changes only its row and the counts in the `Tally` objects.
//! The controller does not read all rows for each message.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use pixheet_core::Options;
use pixheet_plan::{
    DropResult, FailureKind, Job, JobQueue, Message, ReplaceQuestion, Row, State, Tally,
    absolute, apply_message, assign_outputs, drop_note, eta_text, expand_drop, failure_detail,
    folder_labels, middle_ellipsis, output_key, parse_options, path_key, path_problem,
    progress_text, result_text, status_text, subfolder_note, summary, summary_line,
};
use serde::Serialize;

pub const TITLE: &str = "Pixheet";
pub const DIALOG_DROP_TEXT: &str = "Close the dialog first. The app did not add the dropped files.";
pub const CANCELLED_TEXT: &str = "The app did not add the dropped images.";
pub const NOT_STARTED_TEXT: &str = "The app did not start the run.";
pub const NEXT_TO_IMAGE: &str = "Next to each image";
pub const CHOOSE_FOLDER: &str = "Choose a folder...";
pub const NO_DND_TEXT: &str = "Drag and drop is not available. Use Add files or Add folder.";
pub const SEP: &str = " \u{b7} ";
pub const FOLDER_IN: &str = "Folder (in ";
/// The path limit for the error text of a failed row in the details.
const DETAIL_PATH_LIMIT: usize = 40;

fn plural(count: usize, word: &str) -> String {
    let n = grouped(count);
    if count == 1 { format!("{n} {word}") } else { format!("{n} {word}s") }
}

/// `1234` gives "1,234".
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

/// The name of a state for the web page, as in _plan.py.
pub fn state_name(state: State) -> &'static str {
    match state {
        State::Waiting => "waiting",
        State::Converting => "converting",
        State::Done => "done",
        State::Failed => "failed",
        State::Skipped => "skipped",
        State::Stopped => "stopped",
    }
}

fn is_finished(state: State) -> bool {
    matches!(state, State::Done | State::Failed | State::Skipped | State::Stopped)
}

/// The answer of the replace dialog.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Answer {
    Replace,
    Skip,
    Cancel,
}

/// The action of the button of the note bar.
#[derive(Clone, Debug)]
enum NoteAction {
    IncludeSubfolders(Vec<PathBuf>),
}

/// A row as the web page shows it.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct RowView {
    pub id: u64,
    pub name: String,
    /// `path_key` of the folder of the image: the key of the folder label.
    pub folder: String,
    pub status: String,
    /// "waiting", "converting", "done", "failed", "skipped" or "stopped".
    pub state: &'static str,
    pub percent: u8,
    pub src: String,
    pub dst: String,
    /// The full reason of a failed row, for "Copy error". Empty for other rows.
    pub error: String,
}

/// The labels of the Folder column.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct FoldersView {
    pub heading: String,
    pub labels: HashMap<String, String>,
    /// False when all images are in one folder: then the list hides the column.
    pub show: bool,
}

/// A button with a label, shown or hidden.
#[derive(Clone, Debug, Serialize, PartialEq, Default)]
pub struct ButtonView {
    pub label: String,
    pub accent: bool,
    pub enabled: bool,
}

/// The question of the replace dialog.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct QuestionView {
    pub heading: String,
    pub text: String,
    pub folders: Vec<(String, Vec<String>)>,
    pub replace: String,
    pub skip: Option<String>,
}

/// The two lines of the details below the list, or the status in the drop zone.
#[derive(Clone, Debug, Serialize, PartialEq, Default)]
pub struct DetailsView {
    /// Line 1: the image, its output and an extra text. The page shortens the paths.
    pub src: String,
    pub dst: Option<String>,
    pub extra: String,
    /// Line 1 without paths, for example "3 files selected."
    pub path_text: String,
    /// Line 2: the result, or the reason of a failure.
    pub text: String,
    pub failed: bool,
}

/// All the state of the window except the rows. It is small, so the controller sends
/// it whole after each change.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct StatusView {
    pub title: String,
    pub list_mode: bool,
    pub count_line: String,
    pub running: bool,
    pub stopping: bool,
    pub progress: String,
    pub eta: String,
    pub bar: f64,
    pub current: Option<u64>,
    pub left: Option<ButtonView>,
    pub retry: Option<ButtonView>,
    pub clear: bool,
    pub folders_menu: Option<ButtonView>,
    pub folder_open: bool,
    pub show: Option<ButtonView>,
    pub open: Option<ButtonView>,
    pub failed_only: Option<ButtonView>,
    pub failed_only_on: bool,
    pub note: String,
    pub note_action: Option<String>,
    pub details: DetailsView,
    pub save_values: Vec<String>,
    pub save_index: usize,
    pub question: Option<QuestionView>,
    pub tally: TallyView,
    pub dnd: bool,
    pub single: Option<RowView>,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Default)]
pub struct TallyView {
    pub done: usize,
    pub failed: usize,
    pub stopped: usize,
    pub skipped: usize,
    pub waiting: usize,
}

/// What the window must show again after an action.
#[derive(Debug, Serialize)]
pub struct Updates {
    /// The full list, after rows came or went. None when the list did not change.
    pub rows: Option<(Vec<RowView>, FoldersView)>,
    /// The rows that changed, when the list did not change.
    pub changed: Vec<RowView>,
    pub status: StatusView,
    /// An error for a dialog, for example bad options.
    pub error: Option<String>,
}

/// An action that waits for the answer of the replace dialog.
enum Pending {
    Add { earlier: Vec<Row>, new_rows: Vec<Row>, again: Vec<Row>, options: Options, wait: bool, notes: Vec<String>, result: DropResult, during_run: bool, stopping: bool, include: bool },
    ConvertWaiting { rows: Vec<Row>, options: Options },
    Retry { earlier: Vec<Row>, rows: Vec<Row>, options: Options, wait: bool },
}

struct Question {
    pending: Pending,
    problems: HashMap<u64, String>,
    existing: HashSet<u64>,
    cancel_note: &'static str,
    view: QuestionView,
}

/// The new state of each row after the checks and the answer of the replace dialog.
struct Decision {
    problems: HashMap<u64, String>,
    skip: HashSet<u64>,
}

impl Decision {
    fn apply(&self, row: &Row) -> Row {
        let mut fresh = row.clone();
        fresh.percent = 0;
        fresh.size = 0;
        fresh.error.clear();
        fresh.kind = None;
        fresh.result = None;
        fresh.failed_dst = None;
        if let Some(problem) = self.problems.get(&row.id) {
            fresh.state = State::Failed;
            fresh.error = problem.clone();
            fresh.kind = Some(FailureKind::Long);
        } else if self.skip.contains(&row.id) {
            fresh.state = State::Skipped;
        } else {
            fresh.state = State::Waiting;
        }
        fresh
    }
}

/// Starts a worker thread for a run. The Tauri layer gives a real thread; tests can give
/// a function that only records the call.
pub type Spawner = Box<dyn FnMut(Arc<JobQueue>, Options, Arc<AtomicBool>, u64) + Send>;

/// The state of the window and of the run.
pub struct Controller {
    rows: HashMap<u64, Row>,
    order: Vec<u64>,
    last_id: u64,
    tally: Tally,
    run_tally: Tally,
    run_ids: HashSet<u64>,
    jobs: Arc<JobQueue>,
    jobs_by_id: HashMap<u64, Job>,
    done_ids: Vec<u64>,
    /// The output folders of the done rows, with the number of files, in order.
    done_folders: Vec<(String, PathBuf, usize)>,
    out_dir: Option<PathBuf>,
    fields: (String, String, bool),
    options: Option<Options>,
    running: bool,
    worker: u64,
    cancel: Arc<AtomicBool>,
    spawn: Spawner,
    current: Option<u64>,
    current_text: String,
    run_started: Instant,
    run_seconds: f64,
    run_processed: usize,
    stopping_text: String,
    /// After a run: (stopped, number of rows of the run), for the summary line.
    run_end_text: Option<(bool, usize)>,
    note: String,
    note_action: Option<NoteAction>,
    failed_only: bool,
    failed_toggle: bool,
    selection: Vec<u64>,
    question: Option<Question>,
    labels: FoldersView,
    dnd: bool,
    pub closing: bool,
    // What changed since the last take_updates().
    list_dirty: bool,
    changed: Vec<u64>,
    changed_set: HashSet<u64>,
    error: Option<String>,
}

impl Controller {
    pub fn new(spawn: Spawner, dnd: bool) -> Self {
        Self {
            rows: HashMap::new(),
            order: Vec::new(),
            last_id: 0,
            tally: Tally::default(),
            run_tally: Tally::default(),
            run_ids: HashSet::new(),
            jobs: Arc::new(JobQueue::new()),
            jobs_by_id: HashMap::new(),
            done_ids: Vec::new(),
            done_folders: Vec::new(),
            out_dir: None,
            fields: ("1000".into(), "white".into(), false),
            options: None,
            running: false,
            worker: 0,
            cancel: Arc::new(AtomicBool::new(false)),
            spawn,
            current: None,
            current_text: String::new(),
            run_started: Instant::now(),
            run_seconds: 0.0,
            run_processed: 0,
            stopping_text: String::new(),
            run_end_text: None,
            note: String::new(),
            note_action: None,
            failed_only: false,
            failed_toggle: false,
            selection: Vec::new(),
            question: None,
            labels: FoldersView { heading: "Folder".into(), labels: HashMap::new(), show: false },
            dnd,
            closing: false,
            list_dirty: true,
            changed: Vec::new(),
            changed_set: HashSet::new(),
            error: None,
        }
    }

    // --- State ---------------------------------------------------------------------

    pub fn running(&self) -> bool {
        self.running
    }

    /// True during a run and while the replace dialog is open.
    pub fn busy(&self) -> bool {
        self.running || self.question.is_some()
    }

    fn list_mode(&self) -> bool {
        self.rows.len() >= 2
    }

    fn stopping(&self) -> bool {
        self.running && self.cancel.load(Ordering::SeqCst)
    }

    pub fn tally(&self) -> Tally {
        self.tally
    }

    pub fn row(&self, id: u64) -> Option<&Row> {
        self.rows.get(&id)
    }

    fn next_id(&mut self) -> u64 {
        self.last_id += 1;
        self.last_id
    }

    fn mark(&mut self, id: u64) {
        if self.changed_set.insert(id) {
            self.changed.push(id);
        }
    }

    // --- Rows ------------------------------------------------------------------------

    /// Count a change of the state of `row` in the tallies and the done folders.
    fn count(&mut self, row: &Row, old: Option<State>, new: Option<State>) {
        if old == new {
            return;
        }
        self.tally.add(old, new).expect("a valid change of state");
        if self.run_ids.contains(&row.id) {
            self.run_tally.add(old, new).expect("a valid change of state");
        }
        if old == Some(State::Done) {
            self.done_ids.retain(|&id| id != row.id);
            let key = path_key(row.dst.parent().unwrap_or(Path::new("")));
            if let Some(i) = self.done_folders.iter().position(|(k, _, _)| *k == key) {
                self.done_folders[i].2 -= 1;
                if self.done_folders[i].2 == 0 {
                    self.done_folders.remove(i);
                }
            }
        }
        if new == Some(State::Done) {
            self.done_ids.push(row.id);
            let folder = row.dst.parent().unwrap_or(Path::new("")).to_path_buf();
            let key = path_key(&folder);
            match self.done_folders.iter().position(|(k, _, _)| *k == key) {
                Some(i) => self.done_folders[i].2 += 1,
                None => self.done_folders.push((key, folder, 1)),
            }
        }
    }

    fn insert_row(&mut self, row: Row) {
        let state = row.state;
        self.count(&row, None, Some(state));
        self.order.push(row.id);
        self.rows.insert(row.id, row);
        self.list_dirty = true;
    }

    /// Put `new` in the place of the row with its id. With `to_end`, move it to the end.
    fn update_row(&mut self, new: Row, to_end: bool) {
        let Some(old) = self.rows.get(&new.id).cloned() else { return };
        // Count the old state with the old output folder, and the new with the new.
        self.count(&old, Some(old.state), None);
        self.count(&new, None, Some(new.state));
        if to_end {
            self.order.retain(|&id| id != new.id);
            self.order.push(new.id);
            self.list_dirty = true;
        }
        let id = new.id;
        self.rows.insert(id, new);
        self.mark(id);
    }

    fn remove_row(&mut self, id: u64) {
        if let Some(row) = self.rows.remove(&id) {
            self.count(&row, Some(row.state), None);
            self.run_ids.remove(&id);
            self.jobs_by_id.remove(&id);
            self.selection.retain(|&s| s != id);
            self.list_dirty = true;
        }
    }

    fn ordered_rows(&self) -> impl Iterator<Item = &Row> {
        self.order.iter().filter_map(|id| self.rows.get(id))
    }

    pub fn row_view(&self, row: &Row) -> RowView {
        let parent = row.src.parent().unwrap_or(Path::new(""));
        let error = if row.state == State::Failed {
            failure_detail(row.kind.unwrap_or(FailureKind::Other), &row.error, row.failed_output())
        } else {
            String::new()
        };
        RowView {
            id: row.id,
            name: row.src.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
            folder: path_key(parent),
            status: status_text(row),
            state: state_name(row.state),
            percent: row.percent,
            src: row.src.display().to_string(),
            dst: row.dst.display().to_string(),
            error,
        }
    }

    // --- Options and "Save to" ---------------------------------------------------------

    /// The text of the option fields of the window.
    pub fn set_fields(&mut self, max_side: String, background: String, compact: bool) {
        self.fields = (max_side, background, compact);
    }

    /// The options for new rows, or an error for a dialog. A run keeps its options.
    fn run_options(&mut self) -> Option<Options> {
        if self.running
            && let Some(options) = self.options
        {
            return Some(options);
        }
        match parse_options(&self.fields.0, &self.fields.1, self.fields.2) {
            Ok(options) => Some(options),
            Err(e) => {
                self.error = Some(e);
                None
            }
        }
    }

    fn save_values(&self) -> Vec<String> {
        let mut values = vec![NEXT_TO_IMAGE.to_string()];
        if let Some(dir) = &self.out_dir {
            values.push(format!("In {}", middle_ellipsis(&dir.display().to_string(), 40)));
        }
        values.push(CHOOSE_FOLDER.to_string());
        values
    }

    /// Save the outputs in `folder`, or next to each image for None.
    ///
    /// The rows that wait, failed or stopped get new outputs. The done rows and the
    /// skipped rows keep their outputs.
    pub fn set_out_dir(&mut self, folder: Option<PathBuf>) {
        self.out_dir = folder.map(|f| absolute(&f));
        let targets: Vec<Row> = self
            .ordered_rows()
            .filter(|r| matches!(r.state, State::Waiting | State::Failed | State::Stopped))
            .cloned()
            .collect();
        let ids: HashSet<u64> = targets.iter().map(|r| r.id).collect();
        let taken: HashSet<String> = self
            .rows
            .values()
            .filter(|r| !ids.contains(&r.id))
            .map(|r| output_key(&r.dst))
            .collect();
        let sources: Vec<PathBuf> = targets.iter().map(|r| r.src.clone()).collect();
        let outputs = assign_outputs(&sources, self.out_dir.as_deref(), &taken);
        for (row, dst) in targets.into_iter().zip(outputs) {
            if dst != row.dst {
                let mut new = row;
                new.dst = dst;
                self.update_row(new, false);
            }
        }
    }

    pub fn out_dir(&self) -> Option<&Path> {
        self.out_dir.as_deref()
    }

    // --- Notes -------------------------------------------------------------------------

    pub fn set_note(&mut self, text: &str) {
        self.note = text.to_string();
        self.note_action = None;
    }

    /// The note has expired, or the user closed it.
    pub fn close_note(&mut self) {
        self.set_note("");
    }

    /// Run the action of the note bar (Include subfolders).
    pub fn run_note_action(&mut self) {
        let action = self.note_action.take();
        self.set_note("");
        if let Some(NoteAction::IncludeSubfolders(folders)) = action {
            self.add_paths(&folders, true, true);
        }
    }

    fn show_drop_notes(&mut self, notes: &[String], result: &DropResult) {
        let subfolders = subfolder_note(result);
        let text = notes
            .iter()
            .chain(std::iter::once(&subfolders))
            .filter(|n| !n.is_empty())
            .cloned()
            .collect::<Vec<_>>()
            .join(" ");
        if !subfolders.is_empty() {
            self.note = text;
            let folders = result.skipped_subfolders.iter().map(|(f, _)| f.clone()).collect();
            self.note_action = Some(NoteAction::IncludeSubfolders(folders));
        } else if !text.is_empty() {
            self.set_note(&text);
        }
    }

    // --- Drops -------------------------------------------------------------------------

    /// Add the images in the dropped `paths` to the list, and convert them.
    ///
    /// `paths` holds files and folders. With `recursive`, the app adds the images of
    /// the subfolders too. `include` is for the Include subfolders action: the images
    /// that are in the list already do not count as duplicates, and they do not convert
    /// again.
    pub fn add_paths(&mut self, paths: &[PathBuf], recursive: bool, include: bool) {
        if self.closing {
            return;
        }
        if self.question.is_some() {
            self.set_note(DIALOG_DROP_TEXT);
            return;
        }
        // A path from the command line can be relative. The rows keep the full path.
        let paths: Vec<PathBuf> = paths.iter().map(|p| absolute(p)).collect();
        let known: HashSet<String> = self
            .rows
            .values()
            .filter(|r| include || !is_finished(r.state))
            .map(|r| path_key(&r.src))
            .collect();
        let mut result = expand_drop(&paths, &known, recursive);
        if include {
            // The note of the first drop named the files in the folders already.
            let folders: HashSet<String> = paths.iter().map(|p| path_key(p)).collect();
            result.duplicates = 0;
            result.ignored.retain(|p| !folders.contains(&path_key(p.parent().unwrap_or(Path::new("")))));
        }
        let notes = vec![drop_note(&result)];
        if result.images.is_empty() {
            self.show_drop_notes(&notes, &result);
            return;
        }
        let during_run = self.running;
        let stopping = self.stopping();
        let images = result.images.clone();
        self.add_images(&images, notes, result, during_run, stopping, include);
    }

    /// Add rows for `images`, ask about outputs that exist, and queue the rows.
    fn add_images(
        &mut self,
        images: &[PathBuf],
        notes: Vec<String>,
        result: DropResult,
        during_run: bool,
        stopping: bool,
        include: bool,
    ) {
        let by_key: HashMap<String, u64> =
            self.rows.values().map(|r| (path_key(&r.src), r.id)).collect();
        let mut fresh = Vec::new();
        let mut again = Vec::new();
        for path in images {
            match by_key.get(&path_key(path)) {
                None => fresh.push(path.clone()),
                Some(id) => {
                    let row = &self.rows[id];
                    if is_finished(row.state) {
                        again.push(row.clone());
                    }
                }
            }
        }
        let again = self.current_outputs(&again);
        let again_ids: HashSet<u64> = again.iter().map(|r| r.id).collect();
        let mut taken: HashSet<String> = self
            .rows
            .values()
            .filter(|r| !again_ids.contains(&r.id))
            .map(|r| output_key(&r.dst))
            .collect();
        taken.extend(again.iter().map(|r| output_key(&r.dst)));
        let outputs = assign_outputs(&fresh, self.out_dir.as_deref(), &taken);
        let new_rows: Vec<Row> = fresh
            .into_iter()
            .zip(outputs)
            .map(|(src, dst)| Row::new(self.next_id(), src, dst))
            .collect();
        // Check the options before the question. Then an error does not discard the answer.
        let Some(options) = self.run_options() else { return };
        // The worker can end while the question is open. The rows wait all the same.
        let wait = self.stopping();
        let earlier = self.waiting_rows();
        let all: Vec<Row> = earlier.iter().chain(&new_rows).chain(&again).cloned().collect();
        let pending =
            Pending::Add { earlier, new_rows, again, options, wait, notes, result, during_run, stopping, include };
        self.decide(&all, &HashSet::new(), CANCELLED_TEXT, pending);
    }

    /// Copies of `rows` with outputs for the current "Save to" folder.
    fn current_outputs(&self, rows: &[Row]) -> Vec<Row> {
        let ids: HashSet<u64> = rows.iter().map(|r| r.id).collect();
        let mut taken: HashSet<String> = self
            .rows
            .values()
            .filter(|r| !ids.contains(&r.id))
            .map(|r| output_key(&r.dst))
            .collect();
        let mut result = rows.to_vec();
        let mut moved = Vec::new();
        for (index, row) in rows.iter().enumerate() {
            let folder = self
                .out_dir
                .clone()
                .unwrap_or_else(|| row.src.parent().unwrap_or(Path::new("")).to_path_buf());
            let key = output_key(&row.dst);
            let parent = row.dst.parent().unwrap_or(Path::new(""));
            if path_key(parent) == path_key(&folder) && !taken.contains(&key) {
                taken.insert(key);
            } else {
                moved.push(index);
            }
        }
        let sources: Vec<PathBuf> = moved.iter().map(|&i| rows[i].src.clone()).collect();
        let outputs = assign_outputs(&sources, self.out_dir.as_deref(), &taken);
        for (index, dst) in moved.into_iter().zip(outputs) {
            result[index].dst = dst;
        }
        result
    }

    /// The rows that wait, when no run is active. A new run takes them too.
    fn waiting_rows(&self) -> Vec<Row> {
        if self.running {
            return Vec::new();
        }
        self.ordered_rows().filter(|r| r.state == State::Waiting).cloned().collect()
    }

    /// Find the rows that cannot run, and ask about the outputs that exist.
    ///
    /// Without outputs that exist, the action continues at once. Else the replace dialog
    /// opens, and [`Controller::answer`] continues the action.
    fn decide(
        &mut self,
        rows: &[Row],
        replace_ids: &HashSet<u64>,
        cancel_note: &'static str,
        pending: Pending,
    ) {
        let mut problems = HashMap::new();
        for row in rows {
            let mut problem = path_problem(&row.dst);
            if problem.is_none() && same_file::is_same_file(&row.src, &row.dst).unwrap_or(false) {
                problem = Some("The output file is the input file.".to_string());
            }
            if let Some(problem) = problem {
                problems.insert(row.id, problem);
            }
        }
        let existing: Vec<&Row> = rows
            .iter()
            .filter(|r| !problems.contains_key(&r.id) && !replace_ids.contains(&r.id) && r.dst.exists())
            .collect();
        if existing.is_empty() {
            let decision = Decision { problems, skip: HashSet::new() };
            self.resume(pending, &decision);
            return;
        }
        // The rows with a problem fail at once. They are not images that convert.
        let others = rows.len() - existing.len() - problems.len();
        let question = ReplaceQuestion {
            existing: existing.iter().map(|r| r.dst.clone()).collect(),
            other_count: others,
        };
        let view = QuestionView {
            heading: question.heading(),
            text: question.text(),
            folders: question
                .folders()
                .into_iter()
                .map(|(folder, files)| {
                    let names = files
                        .iter()
                        .map(|f| f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default())
                        .collect();
                    (folder.display().to_string(), names)
                })
                .collect(),
            replace: question.replace_label().to_string(),
            skip: question.skip_label(),
        };
        let existing = existing.iter().map(|r| r.id).collect();
        self.question = Some(Question { pending, problems, existing, cancel_note, view });
    }

    /// The answer of the replace dialog.
    pub fn answer(&mut self, answer: Answer) {
        let Some(question) = self.question.take() else { return };
        if self.closing {
            return;
        }
        match answer {
            Answer::Cancel => self.set_note(question.cancel_note),
            Answer::Replace | Answer::Skip => {
                let skip = if answer == Answer::Skip { question.existing } else { HashSet::new() };
                let decision = Decision { problems: question.problems, skip };
                self.resume(question.pending, &decision);
            }
        }
    }

    /// Continue an action after its decision.
    fn resume(&mut self, pending: Pending, decision: &Decision) {
        match pending {
            Pending::Add { earlier, new_rows, again, options, wait, mut notes, result, during_run, stopping, include } => {
                if self.failed_only {
                    self.failed_only = false;
                }
                let mut queued = self.apply(&earlier, decision, false);
                for row in &new_rows {
                    let new = decision.apply(row);
                    self.insert_row(new.clone());
                    queued.push(new);
                }
                queued.extend(self.apply(&again, decision, true));
                let added = queued.len();
                self.enqueue(&queued, options, wait);
                if added > 0 && stopping {
                    let verb = if added == 1 { "It waits" } else { "They wait" };
                    notes.insert(0, format!("Added {} to the list. {verb}, because the run stops.", plural(added, "image")));
                } else if added > 0 && during_run && !include {
                    notes.insert(0, format!("Added {} to the end of the list.", plural(added, "image")));
                }
                self.show_drop_notes(&notes, &result);
            }
            Pending::ConvertWaiting { rows, options } => {
                let rows = self.apply(&rows, decision, false);
                self.enqueue(&rows, options, false);
            }
            Pending::Retry { earlier, rows, options, wait } => {
                if self.failed_only {
                    self.failed_only = false;
                }
                let mut all = self.apply(&earlier, decision, false);
                all.extend(self.apply(&rows, decision, false));
                self.enqueue(&all, options, wait);
            }
        }
    }

    /// Give each row of `rows` its new state from `decision`. A removed row is ignored.
    fn apply(&mut self, rows: &[Row], decision: &Decision, to_end: bool) -> Vec<Row> {
        let mut result = Vec::new();
        for row in rows {
            let Some(current) = self.rows.get(&row.id) else { continue };
            let mut base = current.clone();
            base.dst = row.dst.clone();
            let new = decision.apply(&base);
            self.update_row(new.clone(), to_end);
            result.push(new);
        }
        result
    }

    // --- The run -----------------------------------------------------------------------

    /// Put the waiting rows of `rows` into the queue. Start a run if none is active.
    fn enqueue(&mut self, rows: &[Row], options: Options, wait: bool) {
        if wait || self.stopping() {
            return;
        }
        if !self.running {
            self.options = Some(options);
            self.run_ids.clear();
            self.run_tally = Tally::default();
            self.run_started = Instant::now();
            self.run_seconds = 0.0;
            self.run_processed = 0;
            self.run_end_text = None;
        }
        for row in rows {
            if self.run_ids.insert(row.id) {
                self.run_tally.add(None, Some(row.state)).expect("a new row");
            }
        }
        let jobs: Vec<Job> = rows
            .iter()
            .filter(|r| r.state == State::Waiting)
            .map(|r| Job::new(r.id, r.src.clone(), r.dst.clone()))
            .collect();
        for job in &jobs {
            self.jobs_by_id.insert(job.id, job.clone());
        }
        let any = !jobs.is_empty();
        self.jobs.put(jobs);
        if self.running {
            return;
        }
        if any {
            self.start_worker();
        } else {
            self.end_run(false);
        }
    }

    fn start_worker(&mut self) {
        self.worker += 1;
        self.cancel = Arc::new(AtomicBool::new(false));
        self.stopping_text.clear();
        self.failed_only = false;
        self.running = true;
        let options = self.options.unwrap_or_default();
        (self.spawn)(Arc::clone(&self.jobs), options, Arc::clone(&self.cancel), self.worker);
    }

    /// The number of the current worker. A message of an older worker has no effect.
    pub fn worker(&self) -> u64 {
        self.worker
    }

    /// Stop the run. The current file stops at its next row. The other rows wait.
    pub fn cancel(&mut self, text: &str) {
        if !self.running {
            return;
        }
        self.cancel.store(true, Ordering::SeqCst);
        self.stopping_text = text.to_string();
    }

    /// The left button: Stop during a run, else Convert N.
    pub fn left_action(&mut self) {
        if self.running {
            self.cancel("Stopping.");
        } else {
            self.convert_waiting();
        }
    }

    /// Start a run with the rows that wait, for example after Stop.
    pub fn convert_waiting(&mut self) {
        if self.running || self.closing || self.question.is_some() {
            return;
        }
        let rows = self.waiting_rows();
        if rows.is_empty() {
            return;
        }
        let Some(options) = self.run_options() else { return };
        let pending = Pending::ConvertWaiting { rows: rows.clone(), options };
        self.decide(&rows, &HashSet::new(), NOT_STARTED_TEXT, pending);
    }

    /// Stop the file that converts now, if it is `row_id`. The queue continues.
    pub fn skip_current(&mut self, row_id: u64) {
        if self.current != Some(row_id) {
            return;
        }
        if let Some(job) = self.jobs_by_id.get(&row_id) {
            job.skip.store(true, Ordering::SeqCst);
        }
    }

    /// Convert the failed rows and the stopped rows again.
    pub fn retry_failed(&mut self) {
        let ids: Vec<u64> = self
            .ordered_rows()
            .filter(|r| matches!(r.state, State::Failed | State::Stopped))
            .map(|r| r.id)
            .collect();
        self.retry(&ids, true);
    }

    /// Convert the rows `ids` again with the current "Save to" folder.
    ///
    /// With `ask`, the rows get new outputs, and the app asks before it replaces an output
    /// that exists. Without `ask`, the rows keep their outputs, and the app replaces them.
    pub fn retry(&mut self, ids: &[u64], ask: bool) {
        if self.closing || self.question.is_some() {
            return;
        }
        let rows: Vec<Row> = ids
            .iter()
            .filter_map(|id| self.rows.get(id))
            .filter(|r| is_finished(r.state))
            .cloned()
            .collect();
        if rows.is_empty() {
            return;
        }
        let Some(options) = self.run_options() else { return };
        let rows = if ask { self.current_outputs(&rows) } else { rows };
        let wait = self.stopping();
        let earlier = self.waiting_rows();
        let replace_ids: HashSet<u64> =
            if ask { HashSet::new() } else { rows.iter().map(|r| r.id).collect() };
        let all: Vec<Row> = earlier.iter().chain(&rows).cloned().collect();
        let pending = Pending::Retry { earlier, rows, options, wait };
        self.decide(&all, &replace_ids, NOT_STARTED_TEXT, pending);
    }

    /// Remove the rows `ids` from the list. The row that converts now stays.
    pub fn remove_rows(&mut self, ids: &[u64]) {
        let removed: Vec<u64> = ids
            .iter()
            .copied()
            .filter(|id| self.rows.contains_key(id) && Some(*id) != self.current)
            .collect();
        if removed.is_empty() {
            return;
        }
        self.jobs.remove(&removed.iter().copied().collect());
        for id in &removed {
            if let Some(job) = self.jobs_by_id.get(id) {
                job.skip.store(true, Ordering::SeqCst); // the worker can have taken it already
            }
            self.remove_row(*id);
        }
        self.order.retain(|id| self.rows.contains_key(id));
        if !self.running && (self.failed_only || self.failed_toggle) {
            // Without failed rows, the filter ends, and the list shows all rows again.
            if self.tally.failed > 0 {
                self.failed_toggle = true;
            } else {
                self.failed_only = false;
                self.failed_toggle = false;
            }
        }
    }

    /// Remove the done rows from the list.
    pub fn remove_done(&mut self) {
        let ids = self.done_ids.clone();
        self.remove_rows(&ids);
    }

    /// Remove all rows when no run is active.
    pub fn clear_list(&mut self) {
        if self.running {
            return;
        }
        self.rows.clear();
        self.order.clear();
        self.tally = Tally::default();
        self.run_ids.clear();
        self.run_tally = Tally::default();
        self.jobs.clear();
        self.jobs_by_id.clear();
        self.done_ids.clear();
        self.done_folders.clear();
        self.failed_only = false;
        self.failed_toggle = false;
        self.selection.clear();
        self.run_end_text = None;
        self.set_note("");
        self.list_dirty = true;
    }

    /// Put one message of the worker `worker` into its row, the tallies and the current
    /// row. A message of an older worker has no effect.
    pub fn handle(&mut self, worker: u64, message: &Message) {
        if worker != self.worker {
            return;
        }
        if let Message::Finished { stopped } = message {
            self.finish(*stopped);
            return;
        }
        let id = match message {
            Message::Started { job_id }
            | Message::Progress { job_id, .. }
            | Message::Converted { job_id, .. }
            | Message::Failed { job_id, .. }
            | Message::Stopped { job_id } => *job_id,
            Message::Finished { .. } => unreachable!(),
        };
        let Some(row) = self.rows.get(&id) else { return }; // the user removed the row
        let new = apply_message(row, message);
        match message {
            Message::Started { .. } => {
                self.current = Some(id);
                self.current_text = "Reading the image.".into();
            }
            Message::Progress { done, total, .. } => {
                self.current_text = format!("Writing row {} of {}.", grouped(*done), grouped(*total));
            }
            _ => {
                if self.current == Some(id) {
                    self.current = None;
                }
                self.jobs_by_id.remove(&id);
                self.run_processed += 1;
                self.run_seconds = self.run_started.elapsed().as_secs_f64();
            }
        }
        if new.state != row.state || new.percent != row.percent || new.dst != row.dst {
            self.update_row(new, false);
        }
    }

    fn finish(&mut self, stopped: bool) {
        // The user can click Stop after the worker sent Finished(false), and a drop can
        // add jobs in that time. Then the run stops too, as the user asked.
        let stopped = stopped || (self.cancel.load(Ordering::SeqCst) && !self.jobs.is_empty());
        self.running = false;
        if self.closing {
            return;
        }
        if stopped {
            self.jobs.clear(); // the rows that wait stay in the list
        } else if !self.jobs.is_empty() {
            // The window added jobs while the worker ended. The run continues.
            self.start_worker();
            return;
        }
        self.jobs_by_id.clear();
        self.current = None;
        self.end_run(stopped);
    }

    fn end_run(&mut self, stopped: bool) {
        self.stopping_text.clear();
        self.running = false;
        let total = self.run_tally.total();
        if self.list_mode() && self.tally.failed > 0 {
            self.failed_toggle = true;
            if let Some(first) = self.ordered_rows().find(|r| r.state == State::Failed) {
                self.selection = vec![first.id];
            }
        }
        self.run_end_text = Some((stopped, total));
    }

    // --- Selection and targets -----------------------------------------------------------

    pub fn select(&mut self, ids: Vec<u64>) {
        self.selection = ids.into_iter().filter(|id| self.rows.contains_key(id)).collect();
    }

    pub fn set_failed_only(&mut self, on: bool) {
        self.failed_only = on && self.tally.failed > 0;
    }

    /// The done row that Open and Show in folder use.
    pub fn open_target(&self) -> Option<&Row> {
        if self.list_mode() && !self.selection.is_empty() {
            if self.selection.len() == 1 {
                return self.rows.get(&self.selection[0]).filter(|r| r.state == State::Done);
            }
            return None;
        }
        if self.done_ids.len() == 1 {
            return self.rows.get(&self.done_ids[0]);
        }
        None
    }

    /// The only output folder, for Show folder.
    pub fn only_folder(&self) -> Option<&Path> {
        match self.done_folders.as_slice() {
            [(_, folder, _)] => Some(folder),
            _ => None,
        }
    }

    /// The output folders and their number of files, for the Show folders menu.
    pub fn folders(&self) -> Vec<(String, String)> {
        self.done_folders
            .iter()
            .map(|(_, folder, count)| {
                let label = format!("{} ({})", middle_ellipsis(&folder.display().to_string(), 60), grouped(*count));
                (folder.display().to_string(), label)
            })
            .collect()
    }

    /// The selected rows that failed, as text for "Copy error".
    pub fn errors_text(&self, ids: &[u64]) -> String {
        ids.iter()
            .filter_map(|id| self.rows.get(id))
            .filter(|r| r.state == State::Failed)
            .map(|r| {
                let detail = failure_detail(r.kind.unwrap_or(FailureKind::Other), &r.error, r.failed_output());
                format!("{}: {detail}", r.src.display())
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    // --- Views -------------------------------------------------------------------------------

    fn folders_view(&self) -> FoldersView {
        let folders: Vec<PathBuf> = self
            .ordered_rows()
            .map(|r| r.src.parent().unwrap_or(Path::new("")).to_path_buf())
            .collect();
        let (heading, labels) = folder_labels(&folders);
        let show = labels.len() > 1;
        FoldersView { heading, labels, show }
    }

    fn count_line(&self) -> String {
        let count = self.rows.len();
        if self.labels.labels.len() <= 1 {
            let folder = self
                .ordered_rows()
                .next()
                .and_then(|r| r.src.parent())
                .map(|p| p.display().to_string())
                .unwrap_or_default();
            format!("{} in {folder}", plural(count, "image"))
        } else {
            format!("{} from {} folders", plural(count, "image"), grouped(self.labels.labels.len()))
        }
    }

    fn details(&self) -> DetailsView {
        let mut view = DetailsView::default();
        if !self.stopping_text.is_empty() {
            view.text = self.stopping_text.clone();
            return view;
        }
        if !self.list_mode() {
            let Some(row) = self.ordered_rows().next() else {
                if !self.dnd {
                    view.text = NO_DND_TEXT.to_string();
                }
                return view;
            };
            match row.state {
                State::Converting => view.text = self.current_text.clone(),
                State::Done => {
                    view.text = match &row.result {
                        Some(result) => summary(&row.dst, result),
                        None => row.dst.display().to_string(),
                    }
                }
                State::Failed => {
                    let detail = pixheet_plan::as_sentence(&failure_detail(
                        row.kind.unwrap_or(FailureKind::Other),
                        &row.error,
                        row.failed_output(),
                    ));
                    let name = row.src.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    view.text = format!("The app did not convert {name}.\n{detail}");
                    view.failed = true;
                }
                State::Stopped => view.text = "The app did not write the current file.".into(),
                State::Skipped => {
                    let name = row.dst.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    view.text = format!("{name} exists. The app did not replace it.");
                }
                State::Waiting => {}
            }
            return view;
        }
        let current = self.current.and_then(|id| self.rows.get(&id));
        if self.selection.len() == 1
            && let Some(row) = self.rows.get(&self.selection[0])
        {
            view.src = row.src.display().to_string();
            let same = path_key(row.dst.parent().unwrap_or(Path::new("")))
                == path_key(row.src.parent().unwrap_or(Path::new("")));
            view.dst = Some(if same {
                row.dst.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
            } else {
                row.dst.display().to_string()
            });
            match row.state {
                State::Done => {
                    if let Some(result) = &row.result {
                        view.text = result_text(result);
                    }
                }
                State::Failed => {
                    view.text = failed_text(row, DETAIL_PATH_LIMIT);
                    view.failed = true;
                }
                State::Converting => view.text = self.current_text.clone(),
                _ => {}
            }
        } else if !self.selection.is_empty() {
            view.path_text = format!("{} files selected.", grouped(self.selection.len()));
        } else if let Some(row) = current {
            view.src = row.src.display().to_string();
            view.extra = format!("{SEP}{}", self.current_text.trim_end_matches('.'));
        }
        view
    }

    fn status(&mut self) -> StatusView {
        let running = self.running;
        let tally = self.tally;
        let list_mode = self.list_mode();
        let (progress, eta, bar, title) = if running {
            let run = self.run_tally;
            let total = run.total().max(1);
            let current = self.current.and_then(|id| self.rows.get(&id));
            let fraction = current.map_or(0.0, |r| f64::from(r.percent) / 100.0);
            let index = (run.finished() + 1).min(total);
            let name = current
                .and_then(|r| r.src.file_name())
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let left = run.waiting as f64 + if current.is_some() { 1.0 - fraction } else { 0.0 };
            (
                progress_text(index, total, &name, &run),
                eta_text(self.run_seconds, self.run_processed, left),
                ((run.finished() as f64 + fraction) / total as f64).min(1.0),
                format!("{TITLE} - {} of {}", grouped(index), grouped(total)),
            )
        } else {
            let (text, bar) = match self.run_end_text {
                Some((stopped, total)) if total > 0 => (
                    summary_line(&self.run_tally, total, stopped),
                    if stopped { 0.0 } else { self.run_tally.finished() as f64 / total as f64 },
                ),
                _ => (String::new(), 0.0),
            };
            (text, String::new(), bar, TITLE.to_string())
        };
        let left = if running {
            Some(ButtonView { label: "Stop".into(), accent: false, enabled: !self.stopping() })
        } else if tally.waiting > 0 {
            Some(ButtonView { label: format!("Convert {}", grouped(tally.waiting)), accent: true, enabled: true })
        } else {
            None
        };
        let retry_count = tally.failed + tally.stopped;
        let retry = (retry_count > 0 && !running).then(|| ButtonView {
            label: format!("Retry failed ({})", grouped(retry_count)),
            accent: false,
            enabled: true,
        });
        let folders = self.done_folders.len();
        let folders_menu = (list_mode && folders >= 2).then(|| ButtonView {
            label: format!("Show folders ({})", grouped(folders)),
            accent: false,
            enabled: true,
        });
        let target = self.open_target().is_some();
        let (show, open) = if list_mode {
            (target && (!running || !self.selection.is_empty()), true)
        } else {
            let both = !running && tally.done > 0;
            (both, both)
        };
        let accent = target && !running;
        let failed_only = (self.failed_toggle && !running && tally.failed > 0).then(|| ButtonView {
            label: format!("Failed only ({})", grouped(tally.failed)),
            accent: false,
            enabled: true,
        });
        let single = if list_mode { None } else { self.ordered_rows().next().map(|r| self.row_view(r)) };
        StatusView {
            title,
            list_mode,
            count_line: if list_mode { self.count_line() } else { String::new() },
            running,
            stopping: self.stopping(),
            progress,
            eta,
            bar,
            current: self.current,
            left,
            retry,
            clear: list_mode && !running,
            folders_menu,
            folder_open: list_mode && folders == 1,
            show: show.then(|| ButtonView { label: "Show in folder".into(), accent: false, enabled: target }),
            open: open.then(|| ButtonView { label: "Open".into(), accent, enabled: target }),
            failed_only,
            failed_only_on: self.failed_only,
            note: self.note.clone(),
            note_action: self.note_action.as_ref().map(|_| "Include subfolders".to_string()),
            details: self.details(),
            save_values: self.save_values(),
            save_index: usize::from(self.out_dir.is_some()),
            question: self.question.as_ref().map(|q| q.view.clone()),
            tally: TallyView {
                done: tally.done,
                failed: tally.failed,
                stopped: tally.stopped,
                skipped: tally.skipped,
                waiting: tally.waiting,
            },
            dnd: self.dnd,
            single,
        }
    }

    /// Send the full list with the next updates, for example after the page loaded.
    pub fn mark_all(&mut self) {
        self.list_dirty = true;
    }

    /// What the window must show again since the last call.
    pub fn take_updates(&mut self) -> Updates {
        let rows = if self.list_dirty {
            self.list_dirty = false;
            self.labels = self.folders_view();
            let rows = self.ordered_rows().map(|r| self.row_view(r)).collect();
            Some((rows, self.labels.clone()))
        } else {
            None
        };
        let changed = if rows.is_some() {
            Vec::new()
        } else {
            self.changed.iter().filter_map(|id| self.rows.get(id)).map(|r| self.row_view(r)).collect()
        };
        self.changed.clear();
        self.changed_set.clear();
        let status = self.status();
        Updates { rows, changed, status, error: self.error.take() }
    }
}

/// Line 2 of a failed row, with its paths shortened to `limit` characters.
fn failed_text(row: &Row, limit: usize) -> String {
    let short = |path: &Path| -> PathBuf {
        let parent = path.parent().unwrap_or(Path::new("")).display().to_string();
        PathBuf::from(middle_ellipsis(&parent, limit)).join(path.file_name().unwrap_or_default())
    };
    let failed = row.failed_output();
    let mut error = row.error.clone();
    for path in [row.src.as_path(), failed] {
        error = error.replace(&path.display().to_string(), &short(path).display().to_string());
    }
    format!(
        "Failed: {}",
        failure_detail(row.kind.unwrap_or(FailureKind::Other), &error, &short(failed))
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    /// A spawner that only counts the runs. The tests send the messages themselves.
    fn recording() -> (Spawner, Arc<Mutex<Vec<u64>>>) {
        let runs = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::clone(&runs);
        let spawn: Spawner = Box::new(move |_, _, _, worker| seen.lock().unwrap().push(worker));
        (spawn, runs)
    }

    use std::sync::Mutex;

    fn png(path: &Path, w: u32, h: u32) {
        image::RgbImage::from_fn(w, h, |x, y| image::Rgb([x as u8 * 20, y as u8 * 30, 9]))
            .save(path)
            .unwrap();
    }

    fn images(dir: &Path, count: usize) -> Vec<PathBuf> {
        (0..count)
            .map(|i| {
                let path = dir.join(format!("img{i}.png"));
                png(&path, 3, 2);
                path
            })
            .collect()
    }

    /// Run the jobs of the controller on this thread with the real converter.
    fn run_all(controller: &mut Controller, jobs: &Arc<Mutex<Option<RunArgs>>>) {
        while let Some((queue, options, cancel, worker)) = jobs.lock().unwrap().take() {
            let (tx, rx) = mpsc::channel();
            let mut post = |m: Message| tx.send(m).unwrap();
            pixheet_plan::run_queue(&queue, &options, &mut post, &cancel, std::time::Duration::ZERO);
            drop(post);
            for message in rx {
                controller.handle(worker, &message);
            }
        }
    }

    type RunArgs = (Arc<JobQueue>, Options, Arc<AtomicBool>, u64);

    /// A spawner that keeps the arguments of the run, for run_all().
    fn deferred() -> (Spawner, Arc<Mutex<Option<RunArgs>>>) {
        let slot = Arc::new(Mutex::new(None));
        let keep = Arc::clone(&slot);
        let spawn: Spawner = Box::new(move |queue, options, cancel, worker| {
            *keep.lock().unwrap() = Some((queue, options, cancel, worker));
        });
        (spawn, slot)
    }

    #[test]
    fn a_drop_adds_rows_and_starts_one_run() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 2), false, false);
        let updates = c.take_updates();
        let (list, _) = updates.rows.expect("the full list");
        assert_eq!(list.len(), 2);
        assert!(list.iter().all(|r| r.state == "waiting"));
        assert_eq!(runs.lock().unwrap().len(), 1);
        assert!(updates.status.running);
        assert!(updates.status.list_mode);
        assert_eq!(updates.status.left.unwrap().label, "Stop");
    }

    #[test]
    fn a_tick_of_500_rows_does_not_count_the_rows() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 500), false, false);
        c.take_updates();
        let worker = c.worker();
        let id = c.order[250];
        c.handle(worker, &Message::Started { job_id: id });
        c.handle(worker, &Message::Progress { job_id: id, done: 1, total: 2 });
        let updates = c.take_updates();
        // A message changes only its row: no full list, one changed row.
        assert!(updates.rows.is_none());
        assert_eq!(updates.changed.len(), 1);
        assert_eq!(updates.changed[0].id, id);
        assert_eq!(updates.changed[0].status, "Converting 50%");
        assert_eq!(updates.status.current, Some(id));
        assert_eq!(c.tally().converting, 1);
        assert_eq!(c.tally().waiting, 499);
    }

    #[test]
    fn a_run_converts_the_rows() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, jobs) = deferred();
        let mut c = Controller::new(spawn, true);
        let paths = images(dir.path(), 3);
        c.add_paths(&paths, false, false);
        run_all(&mut c, &jobs);
        let updates = c.take_updates();
        assert!(!updates.status.running);
        assert_eq!(c.tally().done, 3);
        assert_eq!(updates.status.progress, "Converted 3 files.");
        assert!(paths.iter().all(|p| p.with_extension("xlsx").is_file()));
        assert_eq!(updates.status.folders_menu, None);
        assert!(updates.status.folder_open);
        assert!(updates.status.clear);
    }

    #[test]
    fn an_existing_output_asks_and_skip_keeps_it() {
        let dir = tempfile::tempdir().unwrap();
        let paths = images(dir.path(), 2);
        std::fs::write(paths[0].with_extension("xlsx"), b"old").unwrap();
        let (spawn, jobs) = deferred();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&paths, false, false);
        let question = c.take_updates().status.question.expect("the replace dialog");
        assert_eq!(question.heading, "img0.xlsx exists");
        assert_eq!(question.skip.as_deref(), Some("Skip this file"));
        // A drop while the dialog is open adds nothing.
        c.add_paths(&paths, false, false);
        assert_eq!(c.take_updates().status.note, DIALOG_DROP_TEXT);
        c.answer(Answer::Skip);
        run_all(&mut c, &jobs);
        assert_eq!(c.tally().skipped, 1);
        assert_eq!(c.tally().done, 1);
        assert_eq!(std::fs::read(paths[0].with_extension("xlsx")).unwrap(), b"old");
    }

    #[test]
    fn cancel_in_the_dialog_adds_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let paths = images(dir.path(), 1);
        std::fs::write(paths[0].with_extension("xlsx"), b"old").unwrap();
        let (spawn, runs) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&paths, false, false);
        let question = c.take_updates().status.question.unwrap();
        assert_eq!(question.skip, None); // no other images: no Skip button
        c.answer(Answer::Cancel);
        let updates = c.take_updates();
        assert_eq!(updates.status.note, CANCELLED_TEXT);
        assert_eq!(c.tally().total(), 0);
        assert!(runs.lock().unwrap().is_empty());
    }

    #[test]
    fn stop_leaves_the_other_rows_waiting() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 3), false, false);
        let worker = c.worker();
        let first = c.order[0];
        c.handle(worker, &Message::Started { job_id: first });
        c.left_action(); // Stop
        assert!(c.take_updates().status.stopping);
        c.handle(worker, &Message::Stopped { job_id: first });
        c.handle(worker, &Message::Finished { stopped: true });
        let updates = c.take_updates();
        assert!(!updates.status.running);
        assert_eq!(c.tally().waiting, 2);
        assert_eq!(c.tally().stopped, 1);
        assert_eq!(updates.status.left.unwrap().label, "Convert 2");
        assert_eq!(updates.status.retry.unwrap().label, "Retry failed (1)");
        assert_eq!(updates.status.progress, "Stopped. Converted 0 of 3 files. 1 stopped.");
    }

    #[test]
    fn a_failed_row_can_retry() {
        let dir = tempfile::tempdir().unwrap();
        let bad = dir.path().join("bad.png");
        std::fs::write(&bad, b"not an image").unwrap();
        let mut paths = images(dir.path(), 1);
        paths.push(bad.clone());
        let (spawn, jobs) = deferred();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&paths, false, false);
        run_all(&mut c, &jobs);
        let updates = c.take_updates();
        assert_eq!(c.tally().failed, 1);
        let failed = updates.status.failed_only.expect("the Failed only toggle");
        assert_eq!(failed.label, "Failed only (1)");
        assert!(updates.status.details.failed);
        assert!(updates.status.details.text.starts_with("Failed: "));
        png(&bad, 2, 2);
        c.retry_failed();
        run_all(&mut c, &jobs);
        assert_eq!(c.tally().done, 2);
    }

    #[test]
    fn bad_options_give_an_error_and_add_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, runs) = recording();
        let mut c = Controller::new(spawn, true);
        c.set_fields("big".into(), "white".into(), false);
        c.add_paths(&images(dir.path(), 1), false, false);
        let updates = c.take_updates();
        assert!(updates.error.unwrap().starts_with("The maximum side must be a whole number"));
        assert_eq!(c.tally().total(), 0);
        assert!(runs.lock().unwrap().is_empty());
    }

    #[test]
    fn save_to_moves_the_waiting_outputs() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("out");
        std::fs::create_dir(&out).unwrap();
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 2), false, false);
        let worker = c.worker();
        c.left_action();
        c.handle(worker, &Message::Finished { stopped: true });
        c.set_out_dir(Some(out.clone()));
        let updates = c.take_updates();
        assert!(updates.changed.iter().all(|r| Path::new(&r.dst).parent() == Some(out.as_path())));
        assert_eq!(updates.status.save_values.len(), 3);
        assert_eq!(updates.status.save_index, 1);
    }

    #[test]
    fn remove_keeps_the_current_row_and_clear_needs_no_run() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 3), false, false);
        let worker = c.worker();
        let ids = c.order.clone();
        c.handle(worker, &Message::Started { job_id: ids[0] });
        c.remove_rows(&ids);
        assert_eq!(c.tally().total(), 1);
        c.clear_list();
        assert_eq!(c.tally().total(), 1); // a run is active
        c.handle(worker, &Message::Stopped { job_id: ids[0] });
        c.handle(worker, &Message::Finished { stopped: false });
        c.clear_list();
        assert_eq!(c.tally().total(), 0);
        assert!(!c.take_updates().status.list_mode);
    }

    #[test]
    fn a_message_of_an_old_worker_has_no_effect() {
        let dir = tempfile::tempdir().unwrap();
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&images(dir.path(), 1), false, false);
        let id = c.order[0];
        c.handle(c.worker() + 1, &Message::Started { job_id: id });
        assert_eq!(c.tally().converting, 0);
    }

    #[test]
    fn a_folder_with_subfolders_offers_to_include_them() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        images(dir.path(), 1);
        images(&sub, 2);
        let (spawn, _) = recording();
        let mut c = Controller::new(spawn, true);
        c.add_paths(&[dir.path().to_path_buf()], false, false);
        let status = c.take_updates().status;
        assert_eq!(status.note_action.as_deref(), Some("Include subfolders"));
        assert_eq!(c.tally().total(), 1);
        c.run_note_action();
        assert_eq!(c.tally().total(), 3);
    }
}
