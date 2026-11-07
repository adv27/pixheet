//! Files: find the images of a drop, sort them, and give each image an output path.

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

/// The suffixes of the images that the app adds from a drop, lower case, without the dot.
pub const IMAGE_SUFFIXES: &[&str] = &[
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "ico", "ppm", "pgm", "pbm", "pnm",
    "tga",
];

/// Files in a folder that the note about other files does not count.
const QUIET_NAMES: &[&str] = &["desktop.ini", "thumbs.db", ".ds_store"];

/// What to do when the output file exists already.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Existing {
    /// Write a new file with a number, for example "photo (2).xlsx".
    #[default]
    KeepBoth,
    /// Write over the old file.
    Replace,
    /// Do not convert the image.
    Skip,
}

pub fn is_image(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| IMAGE_SUFFIXES.contains(&e.to_ascii_lowercase().as_str()))
}

/// The absolute path without "." and "..". It does not read the disk.
pub fn absolute(path: &Path) -> PathBuf {
    let full = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    let mut out = PathBuf::new();
    for part in full.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

/// One key for two spellings of one file. The case of the letters has no effect, also on
/// Linux: a disk such as exFAT or a share can ignore the case there too.
pub fn key(path: &Path) -> String {
    let text = absolute(path).display().to_string().to_lowercase();
    if cfg!(windows) {
        text.replace('/', "\\")
    } else {
        text
    }
}

/// A sort key that puts names in the order of File Explorer: "IMG_2" before "IMG_10".
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct NaturalKey(Vec<Part>, String);

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Part {
    Text(String),
    // The digits without the zeros in front: a longer number is larger.
    Number(usize, String),
}

pub fn natural_key(name: &str) -> NaturalKey {
    let lower = name.to_lowercase();
    let mut parts = Vec::new();
    let mut current = String::new();
    let mut digits = false;
    for c in lower.chars() {
        if c.is_ascii_digit() != digits && !current.is_empty() {
            parts.push(part(std::mem::take(&mut current), digits));
        }
        digits = c.is_ascii_digit();
        current.push(c);
    }
    if !current.is_empty() {
        parts.push(part(current, digits));
    }
    NaturalKey(parts, name.to_string())
}

fn part(text: String, digits: bool) -> Part {
    if digits {
        let trimmed = text.trim_start_matches('0').to_string();
        Part::Number(trimmed.len(), trimmed)
    } else {
        Part::Text(text)
    }
}

fn sort_natural(paths: &mut [PathBuf]) {
    paths.sort_by_cached_key(|p| natural_key(&p.file_name().unwrap_or_default().to_string_lossy()));
}

/// The images of a drop, and what the app did not add.
#[derive(Debug, Default)]
pub struct Found {
    pub images: Vec<PathBuf>,
    /// Files that are not images. Hidden files, .xlsx files and system files do not count.
    pub ignored: usize,
    /// Dropped paths that do not exist, and folders that the app cannot read.
    pub missing: usize,
}

/// Find the images of the dropped `paths`, files and folders.
///
/// A folder gives its images in the order of File Explorer. With `subfolders`, the images
/// of its subfolders follow. An image comes once, also when the drop holds it twice.
pub fn find_images(paths: &[PathBuf], subfolders: bool) -> Found {
    let mut found = Found::default();
    let mut seen = HashSet::new();
    let mut visited = HashSet::new();
    for path in paths {
        let path = absolute(path);
        if path.is_dir() {
            scan(&path, subfolders, &mut found, &mut seen, &mut visited);
        } else if !path.is_file() {
            found.missing += 1;
        } else if is_image(&path) {
            if seen.insert(key(&path)) {
                found.images.push(path);
            }
        } else {
            found.ignored += 1;
        }
    }
    found
}

fn scan(
    folder: &Path,
    subfolders: bool,
    found: &mut Found,
    seen: &mut HashSet<String>,
    visited: &mut HashSet<PathBuf>,
) {
    // A link can point back to a folder above it: open each real folder once.
    let real = folder
        .canonicalize()
        .unwrap_or_else(|_| folder.to_path_buf());
    if !visited.insert(real) {
        return;
    }
    let Ok(entries) = std::fs::read_dir(folder) else {
        found.missing += 1;
        return;
    };
    let mut files = Vec::new();
    let mut folders = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            folders.push(path)
        } else {
            files.push(path)
        }
    }
    sort_natural(&mut files);
    for path in files {
        // A hidden file in a folder is not an image of the user, for example the "._"
        // files that macOS writes on a USB drive. A dropped hidden file still counts.
        let hidden = path
            .file_name()
            .is_some_and(|n| n.to_string_lossy().starts_with('.'));
        if is_image(&path) && !hidden {
            if seen.insert(key(&path)) {
                found.images.push(path);
            }
        } else if !quiet(&path) {
            found.ignored += 1;
        }
    }
    if subfolders {
        sort_natural(&mut folders);
        for sub in folders {
            scan(&sub, true, found, seen, visited);
        }
    }
}

fn quiet(path: &Path) -> bool {
    let name = path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    QUIET_NAMES.contains(&name.as_str()) || name.starts_with('.') || name.ends_with(".xlsx")
}

/// What the app does with an image, after the check of its output.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Plan {
    /// Write this file.
    Write(PathBuf),
    /// The output exists and the policy is Skip.
    Skip(PathBuf),
}

/// Give `src` an output in `out_dir` (or next to the image) that no other row uses.
///
/// `taken` holds the keys of the outputs of the other rows. Two images that want one
/// output get numbers: "photo.xlsx", then "photo (2).xlsx". A file on the disk counts as
/// taken unless the policy is Replace. The caller adds the key of the result to `taken`.
pub fn plan_output(
    src: &Path,
    out_dir: Option<&Path>,
    existing: Existing,
    taken: &HashSet<String>,
) -> Plan {
    let folder = out_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| src.parent().unwrap_or(Path::new("")).to_path_buf());
    let stem = src
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let plain = folder.join(format!("{stem}.xlsx"));
    let free = |path: &Path| !taken.contains(&key(path));
    if free(&plain) {
        match (plain.exists(), existing) {
            (false, _) | (true, Existing::Replace) => return Plan::Write(plain),
            (true, Existing::Skip) => return Plan::Skip(plain),
            (true, Existing::KeepBoth) => {}
        }
    }
    let mut number = 2;
    loop {
        let candidate = folder.join(format!("{stem} ({number}).xlsx"));
        if free(&candidate) && (existing == Existing::Replace || !candidate.exists()) {
            return Plan::Write(candidate);
        }
        number += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn touch(path: &Path) {
        std::fs::write(path, b"").unwrap();
    }

    #[test]
    fn natural_order() {
        let mut names = vec![
            "IMG_10.png",
            "img_2.png",
            "IMG_1.png",
            "a.png",
            "IMG_002.png",
        ];
        names.sort_by_key(|n| natural_key(n));
        assert_eq!(
            names,
            [
                "a.png",
                "IMG_1.png",
                "IMG_002.png",
                "img_2.png",
                "IMG_10.png"
            ]
        );
    }

    #[test]
    fn a_folder_gives_its_images_in_order() {
        let dir = tempfile::tempdir().unwrap();
        for name in [
            "b10.jpg",
            "b2.PNG",
            "notes.txt",
            ".hidden.png",
            "old.xlsx",
            "Thumbs.db",
        ] {
            touch(&dir.path().join(name));
        }
        let sub = dir.path().join("sub");
        std::fs::create_dir(&sub).unwrap();
        touch(&sub.join("c.gif"));
        let found = find_images(&[dir.path().to_path_buf()], false);
        let names: Vec<_> = found
            .images
            .iter()
            .map(|p| p.file_name().unwrap().to_str().unwrap())
            .collect();
        assert_eq!(names, ["b2.PNG", "b10.jpg"]);
        assert_eq!(found.ignored, 1); // notes.txt; the hidden, .xlsx and system files are quiet
        let found = find_images(&[dir.path().to_path_buf()], true);
        assert_eq!(found.images.len(), 3);
    }

    #[test]
    fn dropped_files_keep_their_order_and_come_once() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("z.png"), dir.path().join("a.png"));
        touch(&a);
        touch(&b);
        let found = find_images(
            &[a.clone(), b.clone(), a.clone(), dir.path().join("x.png")],
            false,
        );
        assert_eq!(found.images, [a, b]);
        assert_eq!(found.missing, 1);
    }

    #[cfg(unix)]
    #[test]
    fn a_loop_of_links_ends() {
        let dir = tempfile::tempdir().unwrap();
        touch(&dir.path().join("a.png"));
        std::os::unix::fs::symlink(dir.path(), dir.path().join("loop")).unwrap();
        let found = find_images(&[dir.path().to_path_buf()], true);
        assert_eq!(found.images.len(), 1);
    }

    #[test]
    fn outputs_get_numbers_when_they_collide() {
        let dir = tempfile::tempdir().unwrap();
        let mut taken = HashSet::new();
        let mut plan = |name: &str| {
            let plan = plan_output(&dir.path().join(name), None, Existing::KeepBoth, &taken);
            let Plan::Write(path) = plan else {
                panic!("{plan:?}")
            };
            taken.insert(key(&path));
            path.file_name().unwrap().to_string_lossy().into_owned()
        };
        assert_eq!(plan("photo.png"), "photo.xlsx");
        assert_eq!(plan("photo.jpg"), "photo (2).xlsx");
        // The case of the letters has no effect.
        assert_eq!(plan("PHOTO.gif"), "PHOTO (3).xlsx");
    }

    #[test]
    fn the_policy_decides_for_a_file_on_the_disk() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("a.png");
        touch(&dir.path().join("a.xlsx"));
        touch(&dir.path().join("a (2).xlsx"));
        let none = HashSet::new();
        let a = dir.path().join("a.xlsx");
        assert_eq!(
            plan_output(&src, None, Existing::Replace, &none),
            Plan::Write(a.clone())
        );
        assert_eq!(
            plan_output(&src, None, Existing::Skip, &none),
            Plan::Skip(a)
        );
        assert_eq!(
            plan_output(&src, None, Existing::KeepBoth, &none),
            Plan::Write(dir.path().join("a (3).xlsx"))
        );
    }

    #[test]
    fn an_output_folder_takes_the_outputs() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("out");
        let plan = plan_output(
            &dir.path().join("a.png"),
            Some(&out),
            Existing::KeepBoth,
            &HashSet::new(),
        );
        assert_eq!(plan, Plan::Write(out.join("a.xlsx")));
    }
}
