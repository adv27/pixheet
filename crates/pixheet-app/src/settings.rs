//! The settings of the sidebar. The app saves them in `settings.json` in its config folder.

use std::path::{Path, PathBuf};

use pixheet_core::{DEFAULT_MAX_SIDE, Options, parse_background};
use serde::{Deserialize, Serialize};

use crate::files::Existing;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Settings {
    /// The cap on the longest side in pixels. 0 means only the sheet limits apply.
    pub max_side: usize,
    /// "white", "black", "none", or a color such as "#336699".
    pub background: String,
    pub compact: bool,
    /// None writes each output next to its image.
    pub out_dir: Option<PathBuf>,
    pub existing: Existing,
    /// A dropped folder also gives the images of its subfolders.
    pub subfolders: bool,
    /// Start the conversion when images come. Else the Convert button starts it.
    pub auto_start: bool,
    /// "system", "light" or "dark".
    pub theme: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            max_side: DEFAULT_MAX_SIDE,
            background: "white".into(),
            compact: false,
            out_dir: None,
            existing: Existing::KeepBoth,
            subfolders: false,
            auto_start: true,
            theme: "system".into(),
        }
    }
}

impl Settings {
    /// The options of a conversion, or the reason why the background is not valid.
    pub fn options(&self) -> Result<Options, String> {
        let background = parse_background(&self.background).map_err(|e| e.to_string())?;
        Ok(Options {
            max_side: self.max_side,
            background,
            compact: self.compact,
        })
    }

    /// The settings in `path`. A missing or damaged file gives the defaults.
    pub fn load(path: &Path) -> Self {
        std::fs::read(path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Settings>(&bytes).ok())
            .filter(|s| s.options().is_ok())
            .unwrap_or_default()
    }

    /// Write the settings to `path`. A failure only loses the settings, so it is silent.
    pub fn save(&self, path: &Path) {
        if let Some(folder) = path.parent() {
            let _ = std::fs::create_dir_all(folder);
        }
        if let Ok(text) = serde_json::to_vec_pretty(self) {
            let tmp = path.with_extension("json.tmp");
            if std::fs::write(&tmp, text).is_ok() {
                let _ = std::fs::rename(&tmp, path);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_survive_a_save() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sub").join("settings.json");
        let settings = Settings {
            max_side: 500,
            background: "#336699".into(),
            existing: Existing::Skip,
            theme: "dark".into(),
            ..Settings::default()
        };
        settings.save(&path);
        assert_eq!(Settings::load(&path), settings);
    }

    #[test]
    fn a_damaged_file_gives_the_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        std::fs::write(&path, b"{ not json").unwrap();
        assert_eq!(Settings::load(&path), Settings::default());
        std::fs::write(&path, br##"{"background": "nope"}"##).unwrap();
        assert_eq!(Settings::load(&path), Settings::default());
        std::fs::write(&path, br##"{"maxSide": 250}"##).unwrap();
        assert_eq!(Settings::load(&path).max_side, 250);
    }
}
