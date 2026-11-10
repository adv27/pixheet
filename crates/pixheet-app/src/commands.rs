//! The commands of the page that change or read the batch. The tests run them on the mock
//! runtime of Tauri, in `app.rs`.

use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::time::Duration;

use tauri::{AppHandle, Manager, Runtime, State};

use crate::app::{App, Startup, save_settings, update};
use crate::batch::{Batch, Updates};
use crate::settings::Settings;

/// The version of the app: `version` in `[workspace.package]` of `Cargo.toml`.
pub(crate) const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The page is ready: send all cards. Add the files of the command line, and end a
/// smoke test.
#[tauri::command]
pub fn ready<R: Runtime>(handle: AppHandle<R>, start: State<'_, Startup>) -> Updates {
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
pub fn add(app: State<'_, App>, paths: Vec<String>) -> Updates {
    let paths: Vec<PathBuf> = paths.into_iter().map(PathBuf::from).collect();
    update(&app, |batch| batch.add(&paths))
}

#[tauri::command]
pub fn set_settings(app: State<'_, App>, settings: Settings) -> Updates {
    let updates = update(&app, |batch| batch.set_settings(settings));
    save_settings(&app, &updates.summary.settings);
    updates
}

#[tauri::command]
pub fn start(app: State<'_, App>) -> Updates {
    update(&app, Batch::start)
}

#[tauri::command]
pub fn stop(app: State<'_, App>) -> Updates {
    update(&app, Batch::stop)
}

#[tauri::command]
pub fn retry_failed(app: State<'_, App>) -> Updates {
    update(&app, Batch::retry_failed)
}

#[tauri::command]
pub fn convert_again(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    update(&app, |batch| batch.convert_again(&ids))
}

#[tauri::command]
pub fn skip(app: State<'_, App>, id: u64) -> Updates {
    update(&app, |batch| batch.skip(id))
}

#[tauri::command]
pub fn remove(app: State<'_, App>, ids: Vec<u64>) -> Updates {
    update(&app, |batch| batch.remove(&ids))
}

#[tauri::command]
pub fn clear(app: State<'_, App>) -> Updates {
    update(&app, Batch::clear)
}

/// The version of the app, for the top bar of the page.
#[tauri::command]
pub fn version() -> &'static str {
    VERSION
}
