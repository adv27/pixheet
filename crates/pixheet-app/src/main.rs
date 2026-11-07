//! The Pixheet desktop app: a Tauri window around [`batch::Batch`].
//!
//! The page in `ui/` shows the state. Each command of the page calls the batch and gives
//! back the updates. The worker thread converts the images and sends its messages to a
//! pump thread. The pump gives them to the batch and sends the updates to the page about
//! 5 times each second.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod batch;
mod files;
mod settings;
mod worker;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use batch::{Batch, Updates};
use settings::Settings;
use tauri::{AppHandle, DragDropEvent, Emitter, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use worker::Message;

/// mimalloc keeps freed memory for the next image. Read docs/rust-port/PROFILE.md.
#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The longest time between two updates of the page during a run.
const PUMP_INTERVAL: Duration = Duration::from_millis(200);

struct App {
    batch: Mutex<Batch>,
    settings_path: Option<PathBuf>,
}

impl App {
    fn lock(&self) -> MutexGuard<'_, Batch> {
        // A panic in one command must not stop the next commands.
        self.batch.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// Send updates to the page, and show the progress in the window title.
fn emit(app: &AppHandle, updates: &Updates) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_title(&updates.summary.title);
    }
    let _ = app.emit("updates", updates);
}

/// Give the messages of the workers to the batch. Send the updates to the page at most
/// every PUMP_INTERVAL, and at once at the end of a run.
fn pump(app: AppHandle, messages: Receiver<(u64, Message)>) {
    let mut last = Instant::now();
    let mut pending = false;
    loop {
        let wait = if pending {
            PUMP_INTERVAL.saturating_sub(last.elapsed())
        } else {
            Duration::from_secs(3600)
        };
        match messages.recv_timeout(wait) {
            Ok((worker, message)) => {
                let finished = matches!(message, Message::Finished { .. });
                let state = app.state::<App>();
                let mut batch = state.lock();
                batch.handle(worker, message);
                pending = true;
                if finished && batch.closing && !batch.running() {
                    drop(batch);
                    app.exit(0);
                    return;
                }
                if finished || last.elapsed() >= PUMP_INTERVAL {
                    let updates = batch.take_updates();
                    drop(batch);
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

/// A spawner that runs the worker on a new thread for each run.
fn spawner(post: Sender<(u64, Message)>) -> batch::Spawner {
    Box::new(move |queue, options, cancel, worker| {
        let post = post.clone();
        std::thread::Builder::new()
            .name("pixheet-worker".into())
            .spawn(move || {
                let mut send = |message: Message| {
                    let _ = post.send((worker, message));
                };
                worker::run(queue, options, cancel, &mut send);
            })
            .expect("start the worker thread");
    })
}

fn update(app: &App, action: impl FnOnce(&mut Batch)) -> Updates {
    let mut batch = app.lock();
    action(&mut batch);
    batch.take_updates()
}

// --- Commands of the page ------------------------------------------------------------

/// The page is ready: send all cards. Add the files of the command line, and end a
/// smoke test.
#[tauri::command]
fn ready(handle: AppHandle, start: State<'_, Startup>) -> Updates {
    let files = std::mem::take(&mut *start.files.lock().unwrap_or_else(|e| e.into_inner()));
    let updates = update(&handle.state::<App>(), |batch| {
        if !files.is_empty() {
            batch.add(&files);
        }
        batch.mark_all();
    });
    if start.smoke {
        start.passed.store(true, Ordering::SeqCst);
        let app = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            app.exit(0);
        });
    }
    updates
}

#[tauri::command]
fn add(app: State<'_, App>, paths: Vec<String>) -> Updates {
    let paths: Vec<PathBuf> = paths.into_iter().map(PathBuf::from).collect();
    update(&app, |batch| batch.add(&paths))
}

#[tauri::command]
async fn browse_files(handle: AppHandle) -> Updates {
    let extensions = files::IMAGE_SUFFIXES;
    let picked = handle
        .dialog()
        .file()
        .set_title("Add images")
        .add_filter("Images", extensions)
        .blocking_pick_files();
    let paths: Vec<PathBuf> = picked
        .unwrap_or_default()
        .into_iter()
        .filter_map(|p| p.into_path().ok())
        .collect();
    update(&handle.state::<App>(), |batch| {
        if !paths.is_empty() {
            batch.add(&paths);
        }
    })
}

#[tauri::command]
async fn browse_folder(handle: AppHandle) -> Updates {
    let picked = handle
        .dialog()
        .file()
        .set_title("Add a folder of images")
        .blocking_pick_folder();
    update(&handle.state::<App>(), |batch| {
        if let Some(folder) = picked.and_then(|p| p.into_path().ok()) {
            batch.add(&[folder]);
        }
    })
}

fn save_settings(app: &App, settings: &Settings) {
    if let Some(path) = &app.settings_path {
        settings.save(path);
    }
}

#[tauri::command]
fn set_settings(app: State<'_, App>, settings: Settings) -> Updates {
    let updates = update(&app, |batch| batch.set_settings(settings));
    save_settings(&app, &updates.summary.settings);
    updates
}

/// Ask for the output folder. Cancel keeps the old setting.
#[tauri::command]
async fn choose_out_dir(handle: AppHandle) -> Updates {
    let picked = handle
        .dialog()
        .file()
        .set_title("Save the .xlsx files in")
        .blocking_pick_folder();
    let app = handle.state::<App>();
    let updates = update(&app, |batch| {
        if let Some(folder) = picked.and_then(|p| p.into_path().ok()) {
            let mut settings = batch.settings().clone();
            settings.out_dir = Some(folder);
            batch.set_settings(settings);
        }
    });
    save_settings(&app, &updates.summary.settings);
    updates
}

#[tauri::command]
fn start(app: State<'_, App>) -> Updates {
    update(&app, Batch::start)
}

#[tauri::command]
fn stop(app: State<'_, App>) -> Updates {
    update(&app, Batch::stop)
}

#[tauri::command]
fn retry_failed(app: State<'_, App>) -> Updates {
    update(&app, Batch::retry_failed)
}

#[tauri::command]
fn convert_again(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    update(&app, |batch| batch.convert_again(&ids))
}

#[tauri::command]
fn skip(app: State<'_, App>, id: u64) -> Updates {
    update(&app, |batch| batch.skip(id))
}

#[tauri::command]
fn remove(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    update(&app, |batch| batch.remove(&ids))
}

#[tauri::command]
fn clear(app: State<'_, App>) -> Updates {
    update(&app, Batch::clear)
}

/// Open the output of a done image in the app of the system, for example Excel.
#[tauri::command]
fn open(handle: AppHandle, id: u64) -> Result<(), String> {
    let Some(path) = handle.state::<App>().lock().output(id) else {
        return Ok(());
    };
    handle
        .opener()
        .open_path(path.display().to_string(), None::<&str>)
        .map_err(|e| format!("Cannot open the file: {e}"))
}

/// Show the output of a done image in its folder.
#[tauri::command]
fn reveal(handle: AppHandle, id: u64) -> Result<(), String> {
    let Some(path) = handle.state::<App>().lock().output(id) else {
        return Ok(());
    };
    handle
        .opener()
        .reveal_item_in_dir(path)
        .map_err(|e| format!("Cannot show the file: {e}"))
}

/// Open the folder of the outputs.
#[tauri::command]
fn open_folder(handle: AppHandle) -> Result<(), String> {
    let Some(folder) = handle.state::<App>().lock().done_folder() else {
        return Ok(());
    };
    handle
        .opener()
        .open_path(folder.display().to_string(), None::<&str>)
        .map_err(|e| format!("Cannot open the folder: {e}"))
}

/// The options of the command line.
struct Startup {
    smoke: bool,
    passed: AtomicBool,
    files: Mutex<Vec<PathBuf>>,
}

/// True when the window must open off the screen, for the tests.
fn hidden_window() -> bool {
    std::env::var("PIXHEET_HIDDEN_WINDOW").is_ok_and(|v| v == "1")
}

fn main() {
    let mut smoke = false;
    let mut files = Vec::new();
    for arg in std::env::args_os().skip(1) {
        if arg == "--smoke" {
            smoke = true;
        } else {
            files.push(PathBuf::from(arg));
        }
    }
    let (post, messages) = mpsc::channel();
    let startup = Startup {
        smoke,
        passed: AtomicBool::new(false),
        files: Mutex::new(files),
    };
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(startup)
        .setup(move |app| {
            let settings_path = app
                .path()
                .app_config_dir()
                .ok()
                .map(|d| d.join("settings.json"));
            let settings = settings_path
                .as_deref()
                .map(Settings::load)
                .unwrap_or_default();
            let batch = Batch::new(spawner(post), settings);
            app.manage(App {
                batch: Mutex::new(batch),
                settings_path,
            });
            let handle = app.handle().clone();
            std::thread::Builder::new()
                .name("pixheet-pump".into())
                .spawn(move || pump(handle, messages))?;
            if let Some(window) = app.get_webview_window("main")
                && hidden_window()
            {
                let _ = window.set_position(tauri::PhysicalPosition::new(-20_000, -20_000));
            }
            if app.state::<Startup>().smoke {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(30));
                    if !handle.state::<Startup>().passed.load(Ordering::SeqCst) {
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
                let updates = update(&app.state::<App>(), |batch| batch.add(paths));
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
                let mut batch = state.lock();
                if batch.running() {
                    // The worker must stop before the process ends. Then no temporary file
                    // stays next to an output.
                    api.prevent_close();
                    batch.closing = true;
                    batch.stop();
                    let updates = batch.take_updates();
                    drop(batch);
                    emit(app, &updates);
                }
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            ready,
            add,
            browse_files,
            browse_folder,
            set_settings,
            choose_out_dir,
            start,
            stop,
            retry_failed,
            convert_again,
            skip,
            remove,
            clear,
            open,
            reveal,
            open_folder,
        ])
        .build(tauri::generate_context!())
        .expect("build the app");
    app.run(|_, _| {});
}
