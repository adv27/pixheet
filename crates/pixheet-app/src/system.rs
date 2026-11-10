//! The commands of the page that open a window of the system: a file dialog, a file, a
//! folder or the browser. A test cannot use them without a person at the screen, so the
//! tests do not call them with a target.

use std::path::PathBuf;

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::app::{App, save_settings, update};
use crate::batch::Updates;
use crate::files;

/// The page of the project on GitHub: `repository` in `[workspace.package]`.
pub(crate) const REPOSITORY: &str = env!("CARGO_PKG_REPOSITORY");

#[tauri::command]
pub async fn browse_files<R: Runtime>(handle: AppHandle<R>) -> Updates {
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
pub async fn browse_folder<R: Runtime>(handle: AppHandle<R>) -> Updates {
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

/// Ask for the output folder. Cancel keeps the old setting.
#[tauri::command]
pub async fn choose_out_dir<R: Runtime>(handle: AppHandle<R>) -> Updates {
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

/// Open the output of a done image in the app of the system, for example Excel.
#[tauri::command]
pub fn open<R: Runtime>(handle: AppHandle<R>, id: u64) -> Result<(), String> {
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
pub fn reveal<R: Runtime>(handle: AppHandle<R>, id: u64) -> Result<(), String> {
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
pub fn open_folder<R: Runtime>(handle: AppHandle<R>) -> Result<(), String> {
    let Some(folder) = handle.state::<App>().lock().done_folder() else {
        return Ok(());
    };
    handle
        .opener()
        .open_path(folder.display().to_string(), None::<&str>)
        .map_err(|e| format!("Cannot open the folder: {e}"))
}

/// Open the page of the project on GitHub in the browser. The page cannot give a URL.
#[tauri::command]
pub fn open_repository<R: Runtime>(handle: AppHandle<R>) -> Result<(), String> {
    handle
        .opener()
        .open_url(REPOSITORY, None::<&str>)
        .map_err(|e| format!("Cannot open the page of the project: {e}"))
}
