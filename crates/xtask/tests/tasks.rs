//! Tests of the `xtask` binary: the arguments, stdin, stdout and the exit code. The
//! workflows read the output of the release tasks, so this format is a contract. The unit
//! tests of release.rs and images.rs test the logic.

use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};

fn xtask(args: &[&str], stdin: &str) -> Output {
    run(Command::new(env!("CARGO_BIN_EXE_xtask")).args(args), stdin)
}

fn run(command: &mut Command, stdin: &str) -> Output {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start xtask");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    child.wait_with_output().expect("run xtask")
}

fn stdout(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).replace("\r\n", "\n")
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

fn s(path: &Path) -> &str {
    path.to_str().unwrap()
}

/// A manifest with the version `version` in `[workspace.package]`.
fn manifest(dir: &Path, name: &str, version: &str) -> String {
    let path = dir.join(name);
    let text = format!("[workspace.package]\nversion = \"{version}\"\n");
    std::fs::write(&path, text).unwrap();
    s(&path).to_string()
}

#[test]
fn version_prints_the_version_of_the_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let path = manifest(dir.path(), "Cargo.toml", "1.2.3");
    let out = xtask(&["version", &path], "");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(stdout(&out), "1.2.3\n");

    let out = xtask(&["version", s(&dir.path().join("missing.toml"))], "");
    assert!(!out.status.success());
    assert!(stdout(&out).is_empty());
    assert!(stderr(&out).contains("missing.toml"), "{}", stderr(&out));
}

#[test]
fn check_pr_passes_a_pull_request_that_keeps_the_version() {
    let dir = tempfile::tempdir().unwrap();
    let base = manifest(dir.path(), "base.toml", "1.2.3");
    let head = manifest(dir.path(), "head.toml", "1.2.3");
    let out = xtask(&["check-pr", &base, &head], "");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(
        stdout(&out),
        "check-pr: the pull request does not change the version 1.2.3.\n"
    );
}

/// A command of git in `dir` without the system and user config, so that the identity, the
/// signing and the URL rules of the user do not apply. Git does not look above `dir`.
fn isolated<'a>(command: &'a mut Command, dir: &Path) -> &'a mut Command {
    let parent = dir.parent().unwrap();
    // A git hook sets GIT_DIR and other variables for the repository of the project.
    for (name, _) in std::env::vars_os() {
        if name.to_string_lossy().starts_with("GIT_") {
            command.env_remove(name);
        }
    }
    command
        .current_dir(dir)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", parent.join("no-gitconfig"))
        .env("GIT_CEILING_DIRECTORIES", parent)
}

fn git(dir: &Path, args: &[&str]) {
    let out = run(isolated(&mut Command::new("git"), dir).args(args), "");
    assert!(out.status.success(), "git {args:?}: {}", stderr(&out));
}

#[test]
fn check_pr_asks_origin_for_the_tag_of_a_new_version() {
    let dir = tempfile::tempdir().unwrap();
    let (upstream, repo, plain) = (
        dir.path().join("upstream"),
        dir.path().join("repo"),
        dir.path().join("plain"),
    );
    for folder in [&upstream, &repo, &plain] {
        std::fs::create_dir(folder).unwrap();
    }
    git(&upstream, &["init", "-q"]);
    let identity = [
        "-c",
        "user.name=Pixheet",
        "-c",
        "user.email=pixheet@example.com",
    ];
    git(
        &upstream,
        &[
            &identity[..],
            &["commit", "-q", "--allow-empty", "-m", "v1.2.4"],
        ]
        .concat(),
    );
    git(&upstream, &["tag", "v1.2.4"]);
    git(&repo, &["init", "-q"]);
    git(&repo, &["remote", "add", "origin", s(&upstream)]);
    let base = manifest(dir.path(), "base.toml", "1.2.3");
    let check_pr = |folder: &Path, version: &str| {
        let head = manifest(dir.path(), "head.toml", version);
        let mut command = Command::new(env!("CARGO_BIN_EXE_xtask"));
        run(
            isolated(&mut command, folder).args(["check-pr", &base, &head]),
            "",
        )
    };

    // The tag of the new version exists: the merge would not publish a release.
    let out = check_pr(&repo, "1.2.4");
    assert!(!out.status.success());
    assert!(
        stdout(&out).starts_with(
            "::error::The pull request changes the version from 1.2.3 to 1.2.4, but the tag \
             v1.2.4 exists already."
        ),
        "{}",
        stdout(&out)
    );
    assert!(stderr(&out).contains("the pull request has a problem"));

    let out = check_pr(&repo, "1.2.5");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(
        stdout(&out),
        "check-pr: the tag v1.2.5 does not exist. The merge publishes it.\n"
    );

    // Without a repository, git fails: the check must not pass.
    let out = check_pr(&plain, "1.2.5");
    assert!(!out.status.success());
    assert!(stdout(&out).is_empty());
    assert!(
        stderr(&out).contains("git ls-remote stopped with exit code"),
        "{}",
        stderr(&out)
    );
}

#[test]
fn release_notes_print_the_section_or_an_annotation() {
    let dir = tempfile::tempdir().unwrap();
    let changelog = dir.path().join("CHANGELOG.md");
    std::fs::write(&changelog, "# Changelog\n\n## 1.2.3\n\n- Fix the colors.\n").unwrap();

    let out = xtask(&["release-notes", "1.2.3", s(&changelog)], "");
    assert!(out.status.success(), "{}", stderr(&out));
    assert!(
        stdout(&out).starts_with("- Fix the colors.\n"),
        "{}",
        stdout(&out)
    );

    // The workflow shows a line that starts with ::error:: as an annotation.
    let out = xtask(&["release-notes", "9.9.9", s(&changelog)], "");
    assert!(!out.status.success());
    assert!(stderr(&out).starts_with("::error::"), "{}", stderr(&out));

    let missing = dir.path().join("missing.md");
    let out = xtask(&["release-notes", "1.2.3", s(&missing)], "");
    assert!(!out.status.success());
    assert!(
        stderr(&out).starts_with("::error::Cannot read"),
        "{}",
        stderr(&out)
    );
}

#[test]
fn release_plan_tag_prints_one_action() {
    let out = xtask(&["release-plan", "tag", "v1.2.3", "abc", "", "none"], "");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(stdout(&out), "create\n");

    // A tag on another commit without a release: the job must stop with an annotation.
    let out = xtask(&["release-plan", "tag", "v1.2.3", "abc", "old", "none"], "");
    assert!(!out.status.success());
    assert!(stdout(&out).starts_with("::error::"), "{}", stdout(&out));
    assert!(stderr(&out).is_empty(), "{}", stderr(&out));
}

#[test]
fn release_plan_upload_reads_the_existing_files_on_stdin() {
    let args = [
        "release-plan",
        "upload",
        "draft",
        "a",
        "a.sha256",
        "b",
        "b.sha256",
    ];
    let out = xtask(&args, "a\n  a.sha256\r\n\n");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(stdout(&out), "publish\nb\nb.sha256\n");

    // A checksum without its file is a broken upload.
    let out = xtask(&args, "a.sha256\n");
    assert!(!out.status.success());
    assert!(stdout(&out).starts_with("::error::"), "{}", stdout(&out));
}

#[test]
fn release_files_prints_one_name_on_each_line() {
    let out = xtask(&["release-files"], "");
    assert!(out.status.success(), "{}", stderr(&out));
    let text = stdout(&out);
    // The unit tests of release.rs own the list. The workflow needs one name on each
    // line, and each file followed by its checksum.
    let names: Vec<&str> = text.lines().collect();
    assert!(!names.is_empty() && names.len().is_multiple_of(2), "{text}");
    for pair in names.chunks(2) {
        assert!(!pair[0].is_empty() && pair[0].trim() == pair[0], "{text}");
        assert_eq!(pair[1], format!("{}.sha256", pair[0]), "{text}");
    }
}

#[cfg(feature = "images")]
#[test]
fn sample_pixels_prints_json_for_excel_check() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.png");
    image::RgbImage::from_pixel(3, 2, image::Rgb([255, 0, 16]))
        .save(&path)
        .unwrap();
    let out = xtask(&["sample-pixels", s(&path), "--count", "2"], "");
    assert!(out.status.success(), "{}", stderr(&out));
    assert_eq!(
        stdout(&out),
        "{\"width\": 3, \"height\": 2, \"samples\": [[1, 1, \"FF0010\"], [2, 3, \"FF0010\"]]}\n"
    );

    let out = xtask(&["sample-pixels", s(&path), "--background", "nope"], "");
    assert!(!out.status.success());
    assert!(!stderr(&out).is_empty());
}

#[cfg(feature = "images")]
#[test]
fn bench_stops_without_images_or_with_a_failed_command() {
    let dir = tempfile::tempdir().unwrap();
    let out = xtask(&["bench", s(dir.path())], "");
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("Run cargo xtask samples first"),
        "{}",
        stderr(&out)
    );

    // The xtask binary does not know the arguments of pixheet, so the command fails.
    image::RgbImage::new(2, 2)
        .save(dir.path().join("a.png"))
        .unwrap();
    let bin = env!("CARGO_BIN_EXE_xtask");
    let out = xtask(&["bench", s(dir.path()), "--bin", bin, "-n", "1"], "");
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("the command stopped with"),
        "{}",
        stderr(&out)
    );
}
