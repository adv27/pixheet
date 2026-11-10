//! The Pixheet desktop app: a Tauri window around [`batch::Batch`].
//!
//! The page in `ui/` shows the state. Each command of the page calls the batch and gives
//! back the updates. The worker thread converts the images and sends its messages to a
//! pump thread. The pump gives them to the batch and sends the updates to the page about
//! 5 times each second.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod batch;
mod commands;
mod files;
mod settings;
mod system;
mod worker;

use tauri::Manager;

/// mimalloc keeps freed memory for the next image. Read docs/rust-port/PROFILE.md.
#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn main() {
    let startup = app::Startup::from_args(std::env::args_os().skip(1));
    let app = app::builder(tauri::Builder::default(), startup)
        .build(tauri::generate_context!())
        .expect("build the app");
    let settings_path = app.path().app_config_dir().ok();
    app::manage(&app, settings_path.map(|d| d.join("settings.json"))).expect("start the pump");
    app.run(|_, _| {});
}
