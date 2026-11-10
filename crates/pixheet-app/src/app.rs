//! The app: its state, the pump of the messages of the worker, the events of the window,
//! and the builder. `main()` and the tests make the app with `builder()` and `manage()`.

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use tauri::{AppHandle, DragDropEvent, Emitter, Manager, Runtime, Window, WindowEvent};

use crate::batch::{self, Batch, Updates};
use crate::settings::Settings;
use crate::worker::{self, Message};
use crate::{commands, system};

/// The longest time between two updates of the page during a run.
const PUMP_INTERVAL: Duration = Duration::from_millis(200);

pub(crate) struct App {
    pub(crate) batch: Mutex<Batch>,
    pub(crate) settings_path: Option<PathBuf>,
}

impl App {
    pub(crate) fn lock(&self) -> MutexGuard<'_, Batch> {
        // A panic in one command must not stop the next commands.
        self.batch.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// Send updates to the page, and show the progress in the window title.
pub(crate) fn emit<R: Runtime>(app: &AppHandle<R>, updates: &Updates) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_title(&updates.summary.title);
    }
    let _ = app.emit("updates", updates);
}

/// Give the messages of the workers to the batch. Send the updates to the page at most
/// every PUMP_INTERVAL, and at once at the end of a run.
fn pump<R: Runtime>(app: AppHandle<R>, messages: Receiver<(u64, Message)>) {
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

pub(crate) fn update(app: &App, action: impl FnOnce(&mut Batch)) -> Updates {
    let mut batch = app.lock();
    action(&mut batch);
    batch.take_updates()
}

pub(crate) fn save_settings(app: &App, settings: &Settings) {
    if let Some(path) = &app.settings_path {
        settings.save(path);
    }
}

/// The options of the command line.
pub(crate) struct Startup {
    pub(crate) smoke: bool,
    pub(crate) passed: AtomicBool,
    pub(crate) files: Mutex<Vec<PathBuf>>,
}

impl Startup {
    /// Read the arguments after the name of the program: `--smoke` and the files to add.
    pub(crate) fn from_args(args: impl IntoIterator<Item = OsString>) -> Self {
        let mut smoke = false;
        let mut files = Vec::new();
        for arg in args {
            if arg == "--smoke" {
                smoke = true;
            } else {
                files.push(PathBuf::from(arg));
            }
        }
        Self {
            smoke,
            passed: AtomicBool::new(false),
            files: Mutex::new(files),
        }
    }
}

/// True when the window must open off the screen, for the tests.
fn hidden_window() -> bool {
    std::env::var("PIXHEET_HIDDEN_WINDOW").is_ok_and(|v| v == "1")
}

/// Add the dropped files, show the drop zone, and stop a run before the window closes.
fn window_event<R: Runtime>(window: &Window<R>, event: &WindowEvent) {
    match event {
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
    }
}

/// The app without its context and without its state. The tests use this function with
/// the mock runtime of Tauri.
pub(crate) fn builder<R: Runtime>(
    builder: tauri::Builder<R>,
    startup: Startup,
) -> tauri::Builder<R> {
    builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(startup)
        .setup(|app| {
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
        .on_window_event(window_event)
        .invoke_handler(tauri::generate_handler![
            commands::ready,
            commands::add,
            system::browse_files,
            system::browse_folder,
            commands::set_settings,
            system::choose_out_dir,
            commands::start,
            commands::stop,
            commands::retry_failed,
            commands::convert_again,
            commands::skip,
            commands::remove,
            commands::clear,
            system::open,
            system::reveal,
            system::open_folder,
            commands::version,
            system::open_repository,
        ])
}

/// Give the batch to the app, and start the pump thread. The settings come from
/// `settings_path`. Without a path, the settings stay in memory only.
pub(crate) fn manage<R: Runtime>(
    app: &tauri::App<R>,
    settings_path: Option<PathBuf>,
) -> std::io::Result<()> {
    let (post, messages) = mpsc::channel();
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
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use serde_json::{Value, json};
    use tauri::test::{INVOKE_KEY, MockRuntime, get_ipc_response, mock_builder, mock_context};
    use tauri::webview::InvokeRequest;
    use tauri::{Listener, PhysicalPosition, WebviewWindow, WebviewWindowBuilder};

    use super::*;
    use crate::commands::VERSION;
    use crate::system::REPOSITORY;

    type MockApp = tauri::App<MockRuntime>;
    type MockWindow = WebviewWindow<MockRuntime>;

    /// The app on the mock runtime of Tauri, and its main window. No window shows.
    fn mock_app(args: &[&Path], settings: Option<PathBuf>) -> (MockApp, MockWindow) {
        let startup = Startup::from_args(args.iter().map(OsString::from));
        let app = builder(mock_builder(), startup)
            .build(mock_context(tauri::test::noop_assets()))
            .expect("build the mock app");
        manage(&app, settings).unwrap();
        let window = WebviewWindowBuilder::new(&app, "main", Default::default())
            .build()
            .expect("make the mock window");
        (app, window)
    }

    /// Call a command as the page does, and give the result as JSON.
    fn invoke(window: &MockWindow, cmd: &str, args: Value) -> Result<Value, Value> {
        let url = if cfg!(windows) {
            "http://tauri.localhost"
        } else {
            "tauri://localhost"
        };
        let request = InvokeRequest {
            cmd: cmd.into(),
            callback: tauri::ipc::CallbackFn(0),
            error: tauri::ipc::CallbackFn(1),
            url: url.parse().unwrap(),
            body: tauri::ipc::InvokeBody::Json(args),
            headers: Default::default(),
            invoke_key: INVOKE_KEY.to_string(),
        };
        get_ipc_response(window, request).map(|body| body.deserialize::<Value>().unwrap())
    }

    /// A count of the summary in the updates, for example "done".
    fn count(updates: &Value, name: &str) -> u64 {
        updates["summary"]["counts"][name].as_u64().unwrap()
    }

    /// The ids of all cards: the page asks for them with "ready".
    fn ids(window: &MockWindow) -> Vec<u64> {
        let updates = invoke(window, "ready", json!({})).unwrap();
        let cards = updates["cards"].as_array().unwrap();
        cards.iter().map(|c| c["id"].as_u64().unwrap()).collect()
    }

    fn png(path: &Path) {
        image::RgbImage::from_fn(6, 4, |x, y| image::Rgb([x as u8 * 40, y as u8 * 60, 9]))
            .save(path)
            .unwrap();
    }

    /// A settings file in `dir` without auto start: the images wait for "start".
    fn manual(dir: &Path) -> PathBuf {
        let path = dir.join("settings.json");
        let settings = Settings {
            auto_start: false,
            ..Settings::default()
        };
        settings.save(&path);
        path
    }

    /// Keep the payloads of the event `name`.
    fn record(app: &MockApp, name: &str) -> Arc<Mutex<Vec<String>>> {
        let events = Arc::new(Mutex::new(Vec::new()));
        let keep = Arc::clone(&events);
        app.listen(name, move |event| {
            keep.lock().unwrap().push(event.payload().to_string());
        });
        events
    }

    /// Wait for the update that the pump sends at the end of a run, and give it. Then
    /// forget the recorded updates, for the next run.
    fn end_of_run(updates: &Mutex<Vec<String>>) -> Value {
        let end = Instant::now() + Duration::from_secs(60);
        loop {
            let last = updates.lock().unwrap().iter().rev().find_map(|text| {
                let update: Value = serde_json::from_str(text).unwrap();
                (update["summary"]["running"] == false).then_some(update)
            });
            if let Some(update) = last {
                updates.lock().unwrap().clear();
                return update;
            }
            assert!(Instant::now() < end, "the run did not end in 60 s");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn the_arguments_give_the_smoke_test_and_the_files() {
        let args = ["a.png", "--smoke", "b.jpg"].map(OsString::from);
        let startup = Startup::from_args(args);
        assert!(startup.smoke);
        let files = startup.files.into_inner().unwrap();
        assert_eq!(files, [PathBuf::from("a.png"), PathBuf::from("b.jpg")]);

        let startup = Startup::from_args(Vec::<OsString>::new());
        assert!(!startup.smoke);
        assert!(startup.files.into_inner().unwrap().is_empty());
    }

    #[test]
    fn ready_adds_the_files_of_the_command_line_one_time() {
        let dir = tempfile::tempdir().unwrap();
        let image = dir.path().join("a.png");
        png(&image);
        let (_app, window) = mock_app(&[&image], Some(manual(dir.path())));

        let updates = invoke(&window, "ready", json!({})).unwrap();
        assert_eq!(updates["cards"].as_array().unwrap().len(), 1);
        assert_eq!(count(&updates, "ready"), 1);
        assert_eq!(updates["summary"]["running"], false);

        let updates = invoke(&window, "ready", json!({})).unwrap();
        assert_eq!(count(&updates, "total"), 1);
    }

    #[test]
    fn the_commands_convert_and_remove_the_images() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("a.png"), dir.path().join("b.png"));
        png(&a);
        png(&b);
        let (app, window) = mock_app(&[], Some(manual(dir.path())));
        let sent = record(&app, "updates");

        // Without a done image, the buttons Open, Show in folder and Open folder do nothing.
        assert_eq!(invoke(&window, "open", json!({ "id": 1 })), Ok(Value::Null));
        assert_eq!(
            invoke(&window, "reveal", json!({ "id": 1 })),
            Ok(Value::Null)
        );
        assert_eq!(invoke(&window, "open_folder", json!({})), Ok(Value::Null));

        let updates = invoke(&window, "add", json!({ "paths": [a, b] })).unwrap();
        assert_eq!(count(&updates, "ready"), 2);

        // The worker converts on its thread. The pump sends the updates to the page.
        let updates = invoke(&window, "start", json!({})).unwrap();
        assert_eq!(updates["summary"]["running"], true);
        assert_eq!(count(&end_of_run(&sent), "done"), 2);
        assert!(dir.path().join("a.xlsx").is_file());
        assert!(dir.path().join("b.xlsx").is_file());

        let ids = ids(&window);
        let updates = invoke(&window, "convert_again", json!({ "ids": [ids[0]] })).unwrap();
        assert_eq!(updates["summary"]["running"], true);
        assert_eq!(count(&end_of_run(&sent), "done"), 2);
        assert!(
            !dir.path().join("a-1.xlsx").exists(),
            "convert again writes over a.xlsx"
        );

        // Skip has an effect only on an image that converts.
        let updates = invoke(&window, "skip", json!({ "id": ids[0] })).unwrap();
        assert_eq!(count(&updates, "done"), 2);
        let updates = invoke(&window, "remove", json!({ "ids": [ids[1]] })).unwrap();
        assert_eq!(count(&updates, "total"), 1);
        let updates = invoke(&window, "clear", json!({})).unwrap();
        assert_eq!(count(&updates, "total"), 0);
    }

    #[test]
    fn retry_failed_converts_a_failed_image_again() {
        let dir = tempfile::tempdir().unwrap();
        let bad = dir.path().join("bad.png");
        std::fs::write(&bad, b"not an image").unwrap();
        let (app, window) = mock_app(&[], Some(manual(dir.path())));
        let sent = record(&app, "updates");
        invoke(&window, "add", json!({ "paths": [bad] })).unwrap();

        invoke(&window, "start", json!({})).unwrap();
        assert_eq!(count(&end_of_run(&sent), "failed"), 1);

        let updates = invoke(&window, "retry_failed", json!({})).unwrap();
        assert_eq!(updates["summary"]["running"], true);
        assert_eq!(count(&end_of_run(&sent), "failed"), 1);

        // Stop without a run changes nothing.
        let updates = invoke(&window, "stop", json!({})).unwrap();
        assert_eq!(updates["summary"]["running"], false);
        assert_eq!(count(&updates, "failed"), 1);
    }

    #[test]
    fn set_settings_saves_the_settings() {
        let dir = tempfile::tempdir().unwrap();
        let path = manual(dir.path());
        let (_app, window) = mock_app(&[], Some(path.clone()));
        let settings = Settings {
            max_side: 50,
            auto_start: false,
            ..Settings::default()
        };

        let updates = invoke(&window, "set_settings", json!({ "settings": settings })).unwrap();
        assert_eq!(updates["summary"]["settings"]["maxSide"], 50);
        assert_eq!(Settings::load(&path), settings);
    }

    #[test]
    fn a_drop_adds_the_images_and_shows_the_drop_zone() {
        let dir = tempfile::tempdir().unwrap();
        let image = dir.path().join("a.png");
        png(&image);
        // Without a settings file the app keeps the settings in memory.
        let (app, window) = mock_app(&[], None);
        let settings = Settings {
            auto_start: false,
            ..Settings::default()
        };
        invoke(&window, "set_settings", json!({ "settings": settings })).unwrap();
        let drag = record(&app, "drag");
        let window = window.as_ref().window();
        let position = PhysicalPosition::new(10.0, 10.0);

        let events = [
            DragDropEvent::Enter {
                paths: vec![image.clone()],
                position,
            },
            DragDropEvent::Over { position },
            DragDropEvent::Leave,
            DragDropEvent::Drop {
                paths: vec![image],
                position,
            },
        ];
        for event in events {
            window_event(&window, &WindowEvent::DragDrop(event));
        }
        // Another event of the window does not change the drop zone.
        window_event(&window, &WindowEvent::Focused(true));

        assert_eq!(*drag.lock().unwrap(), ["true", "true", "false", "false"]);
        assert_eq!(ids(&window.get_webview_window("main").unwrap()).len(), 1);
    }

    #[test]
    fn the_version_command_gives_the_version() {
        let (_app, window) = mock_app(&[], None);
        assert_eq!(invoke(&window, "version", json!({})), Ok(json!(VERSION)));
    }

    #[test]
    fn the_repository_is_on_github() {
        assert!(
            REPOSITORY.starts_with("https://github.com/"),
            "{REPOSITORY}"
        );
        assert!(!REPOSITORY.ends_with('/'), "{REPOSITORY}");
    }
}
