//! Tests of the `pixheet` binary. The Python version had the same tests.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// The report line. Its format is the same as in the Python version.
const LINE: &str = r"^(?P<name>.+)  (?P<ow>\d+)x(?P<oh>\d+) -> (?P<w>\d+)x(?P<h>\d+)  (?P<colors>[\d,]+) colors  (?P<cells>[\d,]+) cells  \d+\.\d\ds$";

fn pixheet(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pixheet"))
        .args(args)
        .output()
        .expect("run pixheet")
}

fn s(path: &Path) -> &str {
    path.to_str().unwrap()
}

fn stdout(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).into_owned()
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

/// An image with distinct colors in each pixel.
fn noise(path: &Path, w: u32, h: u32, seed: u32) {
    let im = image::RgbImage::from_fn(w, h, |x, y| {
        let v = (x * 31 + y * 17 + seed * 101).wrapping_mul(2_654_435_761);
        image::Rgb([(v >> 8) as u8, (v >> 16) as u8, (v >> 24) as u8])
    });
    im.save(path).unwrap();
}

fn images(dir: &Path) -> Vec<PathBuf> {
    [(40, 30), (25, 50)]
        .iter()
        .enumerate()
        .map(|(i, &(w, h))| {
            let path = dir.join(format!("img{i}.png"));
            noise(&path, w, h, i as u32);
            path
        })
        .collect()
}

fn sheet(path: &Path) -> String {
    let file = std::fs::File::open(path).unwrap();
    let mut zip = zip::ZipArchive::new(file).unwrap();
    let mut text = String::new();
    zip.by_name("xl/worksheets/sheet1.xml")
        .unwrap()
        .read_to_string(&mut text)
        .unwrap();
    text
}

#[test]
fn single_input_writes_next_to_it() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let out = pixheet(&[s(&imgs[0])]);
    assert!(out.status.success(), "{}", stderr(&out));
    assert!(imgs[0].with_extension("xlsx").is_file());
    let line = stdout(&out);
    let re = regex::Regex::new(LINE).unwrap();
    let caps = re.captures(line.trim_end()).expect(&line);
    assert_eq!(&caps["name"], s(&imgs[0]));
    assert_eq!((&caps["w"], &caps["h"]), ("40", "30"));
    assert_eq!(&caps["cells"], "1,200");
}

#[test]
fn output_option() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let dst = dir.path().join("out.xlsx");
    assert!(pixheet(&[s(&imgs[0]), "-o", s(&dst)]).status.success());
    assert!(dst.is_file());
}

#[test]
fn multiple_inputs() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let out = pixheet(&[s(&imgs[0]), s(&imgs[1])]);
    assert!(out.status.success());
    assert_eq!(stdout(&out).lines().count(), 2);
}

#[test]
fn output_with_multiple_inputs_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let out = pixheet(&[
        s(&imgs[0]),
        s(&imgs[1]),
        "-o",
        s(&dir.path().join("o.xlsx")),
    ]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("-o/--output can only be used with a single INPUT"));
}

#[test]
fn failure_keeps_going() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let bad = dir.path().join("bad.png");
    std::fs::write(&bad, b"not an image").unwrap();
    let missing = dir.path().join("missing.png");
    let out = pixheet(&[s(&imgs[0]), s(&bad), s(&missing), s(&imgs[1])]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stdout(&out).lines().count(), 2);
    let err = stderr(&out);
    let lines: Vec<&str> = err.lines().collect();
    assert_eq!(lines.len(), 2, "{err}");
    assert!(lines[0].starts_with(&format!("pixheet: {}: cannot identify image file", s(&bad))));
    assert!(lines[1].starts_with(&format!("pixheet: {}: ", s(&missing))));
}

#[test]
fn quiet() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let out = pixheet(&[s(&imgs[0]), "-q"]);
    assert!(out.status.success());
    assert!(out.stdout.is_empty());
}

#[test]
fn options_are_passed() {
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("t.png");
    image::RgbaImage::from_pixel(60, 40, image::Rgba([0, 0, 0, 0]))
        .save(&src)
        .unwrap();
    let out = pixheet(&[
        s(&src),
        "--max-side",
        "30",
        "--background",
        "#FF0000",
        "--compact",
    ]);
    assert!(out.status.success(), "{}", stderr(&out));
    assert!(stdout(&out).contains("60x40 -> 30x20"));
    let text = sheet(&src.with_extension("xlsx"));
    assert!(text.contains("<c s=\"1\"/>"));
    assert!(!text.contains("<c r="));
}

#[test]
fn max_side_zero_keeps_a_wide_image() {
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("wide.png");
    image::RgbImage::from_pixel(1200, 10, image::Rgb([1, 2, 3]))
        .save(&src)
        .unwrap();
    let out = pixheet(&[s(&src), "--max-side", "0"]);
    assert!(stdout(&out).contains("1200x10 -> 1200x10"));
}

#[test]
fn bad_arguments() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let cases: &[(&[&str], &str)] = &[
        (&["--max-side", "-1"], "must be >= 0, got -1"),
        (&["--max-side", "big"], "not an integer: 'big'"),
        (&["--max-side", "1.5"], "not an integer: '1.5'"),
        (&["--background", "nope"], "invalid background color 'nope'"),
    ];
    for (args, message) in cases {
        let mut argv = vec![s(&imgs[0])];
        argv.extend_from_slice(args);
        let out = pixheet(&argv);
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        let err = stderr(&out);
        assert!(
            err.contains("--max-side") || err.contains("--background"),
            "{err}"
        );
        assert!(err.contains(message), "{err}");
    }
    let out = pixheet(&[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(stderr(&out).contains("INPUT"));
}

#[test]
fn wildcards_are_expanded() {
    let dir = tempfile::tempdir().unwrap();
    images(dir.path());
    let out = pixheet(&[s(&dir.path().join("img*.png"))]);
    assert!(out.status.success());
    assert_eq!(stdout(&out).lines().count(), 2);
}

#[test]
fn unmatched_wildcard_is_reported() {
    let dir = tempfile::tempdir().unwrap();
    let out = pixheet(&[s(&dir.path().join("nothing*.png"))]);
    assert_eq!(out.status.code(), Some(1));
    assert!(stderr(&out).contains("nothing*.png"));
}

#[test]
fn version() {
    let out = pixheet(&["--version"]);
    assert!(out.status.success());
    let re = regex::Regex::new(r"^pixheet \d+\.\d+\.\d+").unwrap();
    assert!(re.is_match(&stdout(&out)));
}

#[test]
fn two_inputs_with_the_same_output() {
    let dir = tempfile::tempdir().unwrap();
    let (png, jpg) = (dir.path().join("a.png"), dir.path().join("a.jpg"));
    noise(&png, 4, 3, 5);
    noise(&jpg, 6, 5, 5);
    let out = pixheet(&[s(&png), s(&jpg)]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stdout(&out).lines().count(), 1);
    assert!(stderr(&out).contains("already written from"));
    assert!(sheet(&dir.path().join("a.xlsx")).contains("<dimension ref=\"A1:D3\"/>"));
}

#[test]
fn failed_input_does_not_claim_its_output() {
    let dir = tempfile::tempdir().unwrap();
    let (bad, good) = (dir.path().join("a.txt"), dir.path().join("a.png"));
    std::fs::write(&bad, "not an image").unwrap();
    noise(&good, 4, 3, 5);
    assert_eq!(pixheet(&[s(&bad), s(&good)]).status.code(), Some(1));
    assert!(dir.path().join("a.xlsx").exists());
}

#[test]
fn input_that_is_not_a_file_keeps_going() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let folder = dir.path().join("folder");
    std::fs::create_dir(&folder).unwrap();
    let out = pixheet(&[s(&folder), s(&imgs[0])]);
    assert_eq!(out.status.code(), Some(1));
    assert!(imgs[0].with_extension("xlsx").exists());
    assert!(stderr(&out).starts_with(&format!("pixheet: {}: ", s(&folder))));
}

#[test]
fn output_equal_to_input() {
    let dir = tempfile::tempdir().unwrap();
    let imgs = images(dir.path());
    let before = std::fs::read(&imgs[0]).unwrap();
    let out = pixheet(&[s(&imgs[0]), "-o", s(&imgs[0])]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(std::fs::read(&imgs[0]).unwrap(), before);
    assert!(stderr(&out).contains("is the input image"));
}

#[test]
fn background_none() {
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("t.png");
    image::RgbaImage::from_pixel(3, 2, image::Rgba([10, 20, 30, 0]))
        .save(&src)
        .unwrap();
    assert!(pixheet(&[s(&src), "--background", "none"]).status.success());
    let file = std::fs::File::open(src.with_extension("xlsx")).unwrap();
    let mut zip = zip::ZipArchive::new(file).unwrap();
    let mut styles = String::new();
    zip.by_name("xl/styles.xml")
        .unwrap()
        .read_to_string(&mut styles)
        .unwrap();
    assert!(styles.contains("rgb=\"FF0A141E\""));
}
