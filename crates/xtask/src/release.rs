//! The release tasks: the version, the check of a pull request, the release notes, and
//! the plan of the release job. The workflow release.yml calls them.

use std::path::Path;
use std::process::Command;

pub const REPOSITORY: &str = "https://github.com/adv27/pixheet";
const CHECKSUM_SUFFIX: &str = ".sha256";

/// The version in `[workspace.package]` of the root `Cargo.toml` at `path`.
///
/// A `pyproject.toml` with `[project] version` also works: the base branch of the pull
/// request that ported the project to Rust has only that file.
pub fn read_version(path: &Path) -> Result<String, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("Cannot read {}: {e}", path.display()))?;
    let table: toml::Table = text
        .parse()
        .map_err(|e| format!("{} is not valid TOML: {e}", path.display()))?;
    let workspace = table.get("workspace").and_then(|w| w.get("package"));
    workspace
        .or_else(|| table.get("project"))
        .and_then(|p| p.get("version"))
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .ok_or_else(|| format!("{} has no [workspace.package] version", path.display()))
}

/// True when the tag is on the remote `origin`.
///
/// `git ls-remote --exit-code` stops with exit code 2 when no ref agrees. Another exit
/// code that is not 0 is an error, for example no network.
pub fn remote_tag_exists(tag: &str) -> Result<bool, String> {
    let out = Command::new("git")
        .args([
            "ls-remote",
            "--exit-code",
            "--tags",
            "origin",
            &format!("refs/tags/{tag}"),
        ])
        .output()
        .map_err(|e| format!("Cannot start git: {e}"))?;
    match out.status.code() {
        Some(0) => Ok(true),
        Some(2) => Ok(false),
        code => Err(format!(
            "git ls-remote stopped with exit code {code:?}: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )),
    }
}

/// The problems of a pull request that changes the version `base` to `head`.
pub fn pr_problems(
    base: &str,
    head: &str,
    tag_exists: impl Fn(&str) -> Result<bool, String>,
) -> Result<Vec<String>, String> {
    if base == head {
        return Ok(Vec::new());
    }
    let tag = format!("v{head}");
    if !tag_exists(&tag)? {
        return Ok(Vec::new());
    }
    Ok(vec![format!(
        "The pull request changes the version from {base} to {head}, but the tag {tag} exists \
         already. After the merge, the workflow does not publish a release. Use a new version."
    )])
}

/// The binaries of a release. Each one has a `.sha256` file too.
pub const BINARIES: &[&str] = &[
    "pixheet-cli-windows-x64.exe",
    "pixheet-app-windows-x64.exe",
    "pixheet-cli-linux-x64",
    "pixheet-app-linux-x64",
    "pixheet-cli-macos-arm64",
    "pixheet-app-macos-arm64",
];

/// The licenses of the third-party code in the binaries. The job "Third-party licenses" of
/// release.yml makes this file with cargo-about (about.toml and about.hbs).
pub const NOTICES: &str = "THIRD-PARTY-LICENSES.html";

/// The files of a release, in the order of the upload: each binary, then the third-party
/// licenses, and each file with its checksum.
pub fn release_files() -> Vec<String> {
    BINARIES
        .iter()
        .chain([&NOTICES])
        .flat_map(|b| [b.to_string(), format!("{b}{CHECKSUM_SUFFIX}")])
        .collect()
}

fn downloads(version: &str) -> String {
    format!(
        "## Downloads

- `pixheet-cli-*` is the command `pixheet`, and `pixheet-app-*` is the desktop app. Each \
file has a `.sha256` file to check the download.
- Windows (x64): `pixheet-app-windows-x64.exe` needs WebView2, which Windows 10 and 11 have.
- Linux (x64): the files need glibc 2.35 or later. The app needs WebKitGTK 4.1 and GTK 3, \
for example Ubuntu 22.04 or Debian 12. A download does not keep the permission to run the \
file: run `chmod +x` first.
- macOS (Apple silicon): the files have no code signature yet.
- The files have no code signature. Windows SmartScreen can show \"Windows protected your \
PC\": click **More info**, then **Run anyway**.
- `{NOTICES}` gives the licenses of the third-party code in the files. The files are \
based in part on the work of the Independent JPEG Group.
- The [README]({REPOSITORY}/blob/v{version}/README.md) of this version tells more.
"
    )
}

/// The section of `version` in the changelog `text`, without its heading.
pub fn changelog_section(text: &str, version: &str) -> Result<String, String> {
    if version.is_empty() || version.contains(char::is_whitespace) {
        return Err(format!("The version {version:?} is not correct."));
    }
    let lines: Vec<&str> = text.lines().collect();
    let is_heading = |line: &str| {
        let line = line.trim_end();
        let rest = line.strip_prefix("## ").unwrap_or("");
        let name = rest.split_whitespace().next().unwrap_or("");
        line.starts_with("## ") && (name == version || name == format!("[{version}]"))
    };
    let Some(start) = lines.iter().position(|l| is_heading(l)) else {
        return Err(format!(
            "CHANGELOG.md has no section '## {version}'. Add the section."
        ));
    };
    let body: Vec<&str> = lines[start + 1..]
        .iter()
        .take_while(|l| !l.starts_with("## "))
        .map(|l| l.trim_end())
        .collect();
    let section = body.join("\n").trim_matches('\n').to_string();
    if section.trim().is_empty() {
        return Err(format!(
            "The section '## {version}' of CHANGELOG.md is empty. Add the changes."
        ));
    }
    Ok(section + "\n")
}

fn starts_list_item(line: &str) -> bool {
    let t = line.trim_start();
    if let Some(rest) = t.strip_prefix(['-', '*', '+']) {
        return rest.starts_with(char::is_whitespace);
    }
    let digits = t.chars().take_while(char::is_ascii_digit).count();
    digits > 0 && t[digits..].starts_with('.') && t[digits + 1..].starts_with(char::is_whitespace)
}

/// Join the wrapped lines of each paragraph and list item into one line. GitHub can show
/// each line end of release notes as a line break. A code block does not change.
pub fn unwrap(text: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    let mut fence = false;
    for line in text.lines() {
        if line.trim_start().starts_with("```") {
            fence = !fence;
            out.push(line.to_string());
            continue;
        }
        let starts_block =
            line.trim().is_empty() || line.starts_with('#') || starts_list_item(line);
        let previous = out.last().cloned().unwrap_or_default();
        if fence
            || starts_block
            || previous.trim().is_empty()
            || previous.starts_with('#')
            || previous.starts_with("```")
        {
            out.push(line.to_string());
        } else if let Some(last) = out.last_mut() {
            *last = format!("{previous} {}", line.trim());
        }
    }
    out.join("\n") + "\n"
}

/// The release notes of `version`: its changelog section, then the downloads.
pub fn release_notes(text: &str, version: &str) -> Result<String, String> {
    Ok(unwrap(&format!(
        "{}\n{}",
        changelog_section(text, version)?,
        downloads(version)
    )))
}

/// What the release job does with the tag: "create", "publish" or "skip".
///
/// `tag_commit` is the commit of the tag, empty when it does not exist. `sha` is the
/// commit of the push. `release` is "none", "draft" or "published". A tag at another
/// commit without a published release is an error that a person must repair.
pub fn tag_action(
    tag: &str,
    tag_commit: &str,
    sha: &str,
    release: &str,
) -> Result<&'static str, String> {
    check_state(release)?;
    if tag_commit.is_empty() {
        return Ok("create");
    }
    if tag_commit == sha {
        return Ok("publish");
    }
    if release == "published" {
        return Ok("skip");
    }
    let (found, delete) = if release == "none" {
        ("has no release", format!("the tag {tag}"))
    } else {
        (
            "has only a draft release",
            format!("the draft release and the tag {tag}"),
        )
    };
    Err(format!(
        "The tag {tag} is at the commit {tag_commit}, but the version {found}. An earlier run \
         made the tag, and then it stopped. This job does not publish the files of a different \
         commit. To repair the release, re-run all jobs of the Release run of the commit \
         {tag_commit}. Or delete {delete} on GitHub, and then start the Release workflow on \
         main again."
    ))
}

fn check_state(release: &str) -> Result<(), String> {
    if ["none", "draft", "published"].contains(&release) {
        Ok(())
    } else {
        Err(format!("unknown state of the release: {release:?}"))
    }
}

fn partner(name: &str) -> String {
    match name.strip_suffix(CHECKSUM_SUFFIX) {
        Some(file) => file.to_string(),
        None => format!("{name}{CHECKSUM_SUFFIX}"),
    }
}

/// The action for the release and the files to upload, in the order of `wanted`.
///
/// The action is "create" without a release and "publish" for a draft. For a published
/// release it is "upload" when files are missing, else "done". `existing` holds the
/// files that uploaded completely. A release with only one file of a pair is an error.
pub fn upload_plan(
    release: &str,
    existing: &[String],
    wanted: &[String],
) -> Result<(&'static str, Vec<String>), String> {
    check_state(release)?;
    if release == "none" {
        return Ok(("create", wanted.to_vec()));
    }
    let missing: Vec<String> = wanted
        .iter()
        .filter(|n| !existing.contains(n))
        .cloned()
        .collect();
    for name in &missing {
        let other = partner(name);
        if wanted.contains(&other) && existing.contains(&other) {
            let immutable = if release == "published" {
                " Nobody can change a published immutable release. To correct one, release a new patch version."
            } else {
                ""
            };
            return Err(format!(
                "The release has {other}, but not a complete {name}. A new build can be \
                 different, and then the checksum is not correct. Delete {other} in the release \
                 by hand. Also delete {name} if the release has a file with that name that did \
                 not upload completely. Then run the job again.{immutable}"
            ));
        }
    }
    if release == "draft" {
        return Ok(("publish", missing));
    }
    if missing.is_empty() {
        return Ok(("done", missing));
    }
    Ok(("upload", missing))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_version_comes_from_the_workspace() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Cargo.toml");
        std::fs::write(
            &path,
            "[workspace]\nmembers = []\n[workspace.package]\nversion = \"1.2.3\"\n",
        )
        .unwrap();
        assert_eq!(read_version(&path).unwrap(), "1.2.3");
        std::fs::write(&path, "[workspace]\n").unwrap();
        assert!(read_version(&path).is_err());
        std::fs::write(
            &path,
            "[project]\nname = \"pixheet\"\nversion = \"0.1.0\"\n",
        )
        .unwrap();
        assert_eq!(read_version(&path).unwrap(), "0.1.0");
    }

    #[test]
    fn a_pull_request_with_a_used_tag_has_a_problem() {
        assert!(
            pr_problems("1.0.0", "1.0.0", |_| panic!("no call"))
                .unwrap()
                .is_empty()
        );
        assert!(
            pr_problems("1.0.0", "1.1.0", |_| Ok(false))
                .unwrap()
                .is_empty()
        );
        let problems = pr_problems("1.0.0", "1.1.0", |tag| Ok(tag == "v1.1.0")).unwrap();
        assert!(problems[0].contains("the tag v1.1.0 exists already"));
    }

    #[test]
    fn the_notes_are_the_section_of_the_version() {
        let text = "# Changelog\n\n## 0.2.0 - 2026-11-07\n\nA paragraph\non two lines.\n\n- An item\n  on two lines.\n\n```\ncode\nblock\n```\n\n## 0.1.0\n\nOld.\n";
        let section = changelog_section(text, "0.2.0").unwrap();
        assert!(section.starts_with("A paragraph\non two lines."));
        assert!(!section.contains("Old."));
        let notes = release_notes(text, "0.2.0").unwrap();
        assert!(notes.starts_with("A paragraph on two lines.\n\n- An item on two lines.\n"));
        assert!(notes.contains("```\ncode\nblock\n```"));
        assert!(notes.contains("## Downloads"));
        assert!(notes.contains("/blob/v0.2.0/README.md"));
    }

    #[test]
    fn the_release_has_the_third_party_licenses() {
        let files = release_files();
        assert_eq!(files.len(), 2 * (BINARIES.len() + 1));
        assert_eq!(
            files[files.len() - 2..],
            [NOTICES.to_string(), format!("{NOTICES}.sha256")]
        );
        let notes = release_notes("## 0.2.0\n\nText.\n", "0.2.0").unwrap();
        assert!(notes.contains("`THIRD-PARTY-LICENSES.html` gives the licenses"));
        assert!(notes.contains("Independent JPEG Group"));
    }

    #[test]
    fn a_missing_or_empty_section_is_an_error() {
        assert!(
            changelog_section("## 0.1.0\n\nText.\n", "0.2.0")
                .unwrap_err()
                .contains("no section")
        );
        assert!(
            changelog_section("## 0.2.0\n\n## 0.1.0\n", "0.2.0")
                .unwrap_err()
                .contains("is empty")
        );
        assert!(changelog_section("## [0.2.0]\n\nText.\n", "0.2.0").is_ok());
    }

    #[test]
    fn tag_actions() {
        assert_eq!(tag_action("v1", "", "abc", "none"), Ok("create"));
        assert_eq!(tag_action("v1", "abc", "abc", "draft"), Ok("publish"));
        assert_eq!(tag_action("v1", "old", "abc", "published"), Ok("skip"));
        assert!(
            tag_action("v1", "old", "abc", "none")
                .unwrap_err()
                .contains("has no release")
        );
        assert!(
            tag_action("v1", "old", "abc", "draft")
                .unwrap_err()
                .contains("only a draft")
        );
        assert!(tag_action("v1", "", "abc", "bad").is_err());
    }

    #[test]
    fn upload_plans() {
        let wanted = s(&["a", "a.sha256", "b", "b.sha256"]);
        assert_eq!(
            upload_plan("none", &[], &wanted).unwrap(),
            ("create", wanted.clone())
        );
        assert_eq!(
            upload_plan("draft", &s(&["a", "a.sha256"]), &wanted).unwrap(),
            ("publish", s(&["b", "b.sha256"]))
        );
        assert_eq!(
            upload_plan("published", &wanted, &wanted).unwrap(),
            ("done", vec![])
        );
        assert_eq!(
            upload_plan("published", &s(&["a", "a.sha256", "x"]), &wanted).unwrap(),
            ("upload", s(&["b", "b.sha256"]))
        );
        let err = upload_plan("published", &s(&["a.sha256"]), &wanted).unwrap_err();
        assert!(err.contains("The release has a.sha256, but not a complete a."));
        assert!(err.contains("immutable"));
    }
}
