//! The Pixheet desktop app: a Tauri window around [`controller::Controller`].
//!
//! The web page in `ui/` shows the state. Each command of the page calls the controller
//! and returns the updates. The worker thread converts the images and sends its messages
//! to a pump thread. The pump gives them to the controller and sends the updates to the
//! page about 5 times each second.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod controller;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use controller::{Answer, Controller, Updates};
use pixheet_plan::{IMAGE_SUFFIX_ORDER, Message, PROGRESS_INTERVAL, run_queue};
use tauri::{AppHandle, DragDropEvent, Emitter, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

/// The longest time between two updates of the page during a run.
const PUMP_INTERVAL: Duration = Duration::from_millis(200);
const CLOSE_TEXT: &str = "Stopping. The window closes when the current file stops.";

struct App {
    controller: Mutex<Controller>,
}

impl App {
    fn lock(&self) -> MutexGuard<'_, Controller> {
        // A panic in a command must not stop the next commands.
        self.controller.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// Send the updates of the controller to the page, and set the window title.
fn emit(app: &AppHandle, updates: &Updates) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_title(&updates.status.title);
    }
    let _ = app.emit("updates", updates);
}

/// Give the messages of the workers to the controller. Send the updates to the page at
/// most every PUMP_INTERVAL, and at once at the end of a run.
fn pump(app: AppHandle, messages: Receiver<(u64, Message)>) {
    let mut last = Instant::now();
    let mut pending = false;
    loop {
        let timeout = if pending { PUMP_INTERVAL.saturating_sub(last.elapsed()) } else { Duration::from_secs(3600) };
        match messages.recv_timeout(timeout) {
            Ok((worker, message)) => {
                let finished = matches!(message, Message::Finished { .. });
                let state = app.state::<App>();
                let mut controller = state.lock();
                controller.handle(worker, &message);
                pending = true;
                if finished && controller.closing && !controller.running() {
                    drop(controller);
                    app.exit(0);
                    return;
                }
                if finished || last.elapsed() >= PUMP_INTERVAL {
                    let updates = controller.take_updates();
                    drop(controller);
                    emit(&app, &updates);
                    last = Instant::now();
                    pending = false;
                }
            }
            Err(RecvTimeoutError::Timeout) => {
                if pending {
                    let updates = app.state::<App>().lock().take_updates();
                    emit(&app, &updates);
                    last = Instant::now();
                    pending = false;
                }
            }
            Err(RecvTimeoutError::Disconnected) => return,
        }
    }
}

/// A spawner that runs `run_queue` on a new thread for each run.
fn spawner(post: Sender<(u64, Message)>) -> controller::Spawner {
    Box::new(move |jobs, options, cancel, worker| {
        let post = post.clone();
        std::thread::Builder::new()
            .name("pixheet-convert".into())
            .spawn(move || {
                let mut send = |message: Message| {
                    let _ = post.send((worker, message));
                };
                run_queue(&jobs, &options, &mut send, &cancel, PROGRESS_INTERVAL);
            })
            .expect("start the worker thread");
    })
}

fn paths(items: Vec<String>) -> Vec<PathBuf> {
    items.into_iter().map(PathBuf::from).collect()
}

// --- Commands of the page ---------------------------------------------------------------

#[tauri::command]
fn snapshot(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.mark_all();
    controller.take_updates()
}

#[tauri::command]
fn add_paths(app: State<'_, App>, items: Vec<String>, recursive: bool) -> Updates {
    let mut controller = app.lock();
    controller.add_paths(&paths(items), recursive, false);
    controller.take_updates()
}

#[tauri::command]
async fn browse_files(handle: AppHandle) -> Updates {
    let extensions: Vec<&str> = IMAGE_SUFFIX_ORDER.iter().map(|s| s.trim_start_matches('.')).collect();
    let picked = handle
        .dialog()
        .file()
        .set_title("Add images")
        .add_filter("Images", &extensions)
        .blocking_pick_files();
    let items: Vec<PathBuf> =
        picked.unwrap_or_default().into_iter().filter_map(|p| p.into_path().ok()).collect();
    let state = handle.state::<App>();
    let mut controller = state.lock();
    if !items.is_empty() {
        controller.add_paths(&items, false, false);
    }
    controller.take_updates()
}

#[tauri::command]
async fn browse_folder(handle: AppHandle) -> Updates {
    let picked = handle.dialog().file().set_title("Add the images of a folder").blocking_pick_folder();
    let state = handle.state::<App>();
    let mut controller = state.lock();
    if let Some(folder) = picked.and_then(|p| p.into_path().ok()) {
        controller.add_paths(&[folder], false, false);
    }
    controller.take_updates()
}

#[tauri::command]
fn answer(app: State<'_, App>, choice: Answer) -> Updates {
    let mut controller = app.lock();
    controller.answer(choice);
    controller.take_updates()
}

#[tauri::command]
fn set_fields(app: State<'_, App>, max_side: String, background: String, compact: bool) {
    app.lock().set_fields(max_side, background, compact);
}

/// The "Save to" box: 0 is "Next to each image", the last item opens a folder dialog,
/// and an item between them keeps the folder.
#[tauri::command]
async fn choose_save(handle: AppHandle, index: usize, count: usize) -> Updates {
    let state = handle.state::<App>();
    if index == 0 {
        let mut controller = state.lock();
        controller.set_out_dir(None);
        return controller.take_updates();
    }
    if index + 1 == count {
        let picked = handle.dialog().file().set_title("Save the .xlsx files in").blocking_pick_folder();
        let mut controller = state.lock();
        if let Some(folder) = picked.and_then(|p| p.into_path().ok()) {
            controller.set_out_dir(Some(folder));
        }
        return controller.take_updates();
    }
    state.lock().take_updates()
}

#[tauri::command]
fn left_action(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.left_action();
    controller.take_updates()
}

#[tauri::command]
fn retry_failed(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.retry_failed();
    controller.take_updates()
}

#[tauri::command]
fn retry(app: State<'_, App>, ids: Vec<u64>, ask: bool) -> Updates {
    let mut controller = app.lock();
    controller.retry(&ids, ask);
    controller.take_updates()
}

#[tauri::command]
fn skip_current(app: State<'_, App>, id: u64) -> Updates {
    let mut controller = app.lock();
    controller.skip_current(id);
    controller.take_updates()
}

#[tauri::command]
fn remove_rows(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    let mut controller = app.lock();
    controller.remove_rows(&ids);
    controller.take_updates()
}

#[tauri::command]
fn remove_done(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.remove_done();
    controller.take_updates()
}

#[tauri::command]
fn clear_list(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.clear_list();
    controller.take_updates()
}

#[tauri::command]
fn select(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    let mut controller = app.lock();
    controller.select(ids);
    controller.take_updates()
}

#[tauri::command]
fn set_failed_only(app: State<'_, App>, on: bool) -> Updates {
    let mut controller = app.lock();
    controller.set_failed_only(on);
    controller.take_updates()
}

#[tauri::command]
fn note_action(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.run_note_action();
    controller.take_updates()
}

#[tauri::command]
fn close_note(app: State<'_, App>) -> Updates {
    let mut controller = app.lock();
    controller.close_note();
    controller.take_updates()
}

#[tauri::command]
fn errors_text(app: State<'_, App>, ids: Vec<u64>) -> String {
    app.lock().errors_text(&ids)
}

#[tauri::command]
fn folders(app: State<'_, App>) -> Vec<(String, String)> {
    app.lock().folders()
}

/// Open the output of the selected done row, or of the only done row.
#[tauri::command]
fn open_output(handle: AppHandle) -> Result<(), String> {
    let target = handle.state::<App>().lock().open_target().map(|r| r.dst.clone());
    let Some(path) = target else { return Ok(()) };
    handle
        .opener()
        .open_path(path.display().to_string(), None::<&str>)
        .map_err(|e| format!("Cannot open the file: {e}"))
}

/// Show the output of the selected done row in its folder.
#[tauri::command]
fn show_output(handle: AppHandle) -> Result<(), String> {
    let target = handle.state::<App>().lock().open_target().map(|r| r.dst.clone());
    let Some(path) = target else { return Ok(()) };
    handle.opener().reveal_item_in_dir(path).map_err(|e| format!("Cannot open the folder: {e}"))
}

/// Open an output folder. Without `folder`, open the only output folder.
#[tauri::command]
fn show_folder(handle: AppHandle, folder: Option<String>) -> Result<(), String> {
    let folder = match folder {
        Some(f) => Some(PathBuf::from(f)),
        None => handle.state::<App>().lock().only_folder().map(PathBuf::from),
    };
    let Some(folder) = folder else { return Ok(()) };
    handle
        .opener()
        .open_path(folder.display().to_string(), None::<&str>)
        .map_err(|e| format!("Cannot open the folder: {e}"))
}

/// The page is ready. Add the files of the command line, and end a smoke test.
#[tauri::command]
fn ready(handle: AppHandle, smoke: State<'_, Smoke>) -> Updates {
    let state = handle.state::<App>();
    let mut controller = state.lock();
    let files = std::mem::take(&mut *smoke.files.lock().unwrap_or_else(|e| e.into_inner()));
    if !files.is_empty() {
        controller.add_paths(&files, false, false);
    }
    controller.mark_all();
    let updates = controller.take_updates();
    if smoke.enabled {
        smoke.passed.store(true, Ordering::SeqCst);
        let app = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            app.exit(0);
        });
    }
    updates
}

/// The options of the command line.
struct Smoke {
    enabled: bool,
    passed: AtomicBool,
    files: Mutex<Vec<PathBuf>>,
}

/// True when the window must open off the screen, for the tests.
fn hidden_window() -> bool {
    std::env::var("PIXHEET_HIDDEN_WINDOW").is_ok_and(|v| v == "1")
}

fn main() {
    let mut smoke_enabled = false;
    let mut files = Vec::new();
    for arg in std::env::args_os().skip(1) {
        if arg == "--smoke" {
            smoke_enabled = true;
        } else {
            files.push(PathBuf::from(arg));
        }
    }
    let (post, messages) = mpsc::channel();
    let controller = Controller::new(spawner(post), true);
    let smoke = Smoke { enabled: smoke_enabled, passed: AtomicBool::new(false), files: Mutex::new(files) };
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(App { controller: Mutex::new(controller) })
        .manage(smoke)
        .setup(move |app| {
            let handle = app.handle().clone();
            std::thread::Builder::new().name("pixheet-pump".into()).spawn(move || pump(handle, messages))?;
            if let Some(window) = app.get_webview_window("main")
                && hidden_window()
            {
                let _ = window.set_position(tauri::PhysicalPosition::new(-20_000, -20_000));
            }
            if app.state::<Smoke>().enabled {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(30));
                    if !handle.state::<Smoke>().passed.load(Ordering::SeqCst) {
                        eprintln!("pixheet-app: smoke test: the page did not start");
                        handle.exit(1);
                    }
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                let app = window.app_handle();
                let state = app.state::<App>();
                let mut controller = state.lock();
                controller.add_paths(paths, false, false);
                let updates = controller.take_updates();
                drop(controller);
                emit(app, &updates);
                let _ = app.emit("drag", false);
            }
            WindowEvent::DragDrop(DragDropEvent::Enter { .. } | DragDropEvent::Over { .. }) => {
                let _ = window.app_handle().emit("drag", true);
            }
            WindowEvent::DragDrop(DragDropEvent::Leave) => {
                let _ = window.app_handle().emit("drag", false);
            }
            WindowEvent::CloseRequested { api, .. } => {
                let app = window.app_handle();
                let state = app.state::<App>();
                let mut controller = state.lock();
                if controller.running() {
                    // The worker must end before the process does, so that no temporary
                    // file stays next to an output.
                    api.prevent_close();
                    controller.closing = true;
                    controller.cancel(CLOSE_TEXT);
                    let updates = controller.take_updates();
                    drop(controller);
                    emit(app, &updates);
                }
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            snapshot,
            ready,
            add_paths,
            browse_files,
            browse_folder,
            answer,
            set_fields,
            choose_save,
            left_action,
            retry_failed,
            retry,
            skip_current,
            remove_rows,
            remove_done,
            clear_list,
            select,
            set_failed_only,
            note_action,
            close_note,
            errors_text,
            folders,
            open_output,
            show_output,
            show_folder,
        ])
        .build(tauri::generate_context!())
        .expect("build the app");
    app.run(|_, _| {});
}
