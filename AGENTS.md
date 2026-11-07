# AGENTS.md

This file gives instructions to AI agents and other contributors who work in this repository.

## Project

Pixheet converts an image to an `.xlsx` spreadsheet. Each cell is one pixel and has the
color of that pixel. The project is a Cargo workspace in Rust: a library, a command, a
desktop app (Tauri 2), and a crate of project tasks. Read [README.md](README.md) for the
usage, the library API and the Excel limits.

The first versions were in Python. The branch `python-legacy` keeps the last one (v0.1.0).
[docs/rust-port/](docs/rust-port/) records the port.

## Layout

| Path | Content |
|---|---|
| `Cargo.toml`, `Cargo.lock` | The workspace. `[workspace.package] version` is the version of the project. |
| `.cargo/config.toml` | The alias `cargo xtask`. |
| `rust-toolchain.toml` | The pinned toolchain (1.99.0). rustup installs it for local builds, CI and the release builds. |
| `crates/pixheet-core/` | The library: `decode.rs` (open, EXIF, color modes), `resample.rs` (the LANCZOS resize and the reduce of Pillow), `fit.rs` (the size cap and the color search), `xlsx.rs` (the XML and the zip), `color.rs` (the background colors), `convert.rs` (the API and the atomic write). |
| `crates/pixheet-core/tests/golden.rs`, `tests/fixtures/` | The golden test: the output of the Python version for 139 cases. Read "Rules for code". |
| `crates/pixheet-cli/` | The `pixheet` command. `tests/cli.rs` runs the binary. |
| `crates/pixheet-app/` | The desktop app: Tauri 2 with a page in plain HTML, CSS and JavaScript (`ui/`). `batch.rs` holds the state and has the tests; `worker.rs` converts; `files.rs` finds the images and names the outputs; `settings.rs` saves the settings. |
| `crates/xtask/` | The project tasks: `version`, `check-pr`, `release-notes`, `release-plan`, `release-files`, `samples`, `bench`, `sample-pixels`. |
| `scripts/excel_check.ps1` | Opens the outputs in Excel through COM (Windows) and compares cell colors with `cargo xtask sample-pixels`. |
| `docs/rust-port/` | The history of the port: the spike, the plan and the profile. |
| `.github/workflows/` | `ci.yml`: the format and clippy on Ubuntu; the tests, the debug builds and the smoke test of the app on Windows, Ubuntu and macOS; and the check of the version tag and of `CHANGELOG.md`. CI caches the dependencies and builds without debug info. `release.yml`: the release builds for the three systems and the release. On a pull request, it runs only when the code, the manifests or the toolchain change. |
| `CHANGELOG.md` | The changes of each version. The release job uses the section of the version as the release notes. |

## Commands

rustup installs the toolchain of `rust-toolchain.toml` for you. The minimum version in
`rust-version` is 1.88. You also need `cmake`, and on x86-64 also `nasm`: the
`turbo` feature of `pixheet-core` (the default) builds libjpeg-turbo. On Linux, the app also needs
WebKitGTK 4.1 and GTK 3 (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`).

```console
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --release -p pixheet-cli                  # target/release/pixheet
cargo run -p pixheet-app                              # the desktop app
cargo xtask samples local-samples                     # synthetic sample images
cargo xtask bench local-samples                       # time target/release/pixheet
```

The smoke test of the app opens the window off the screen and stops when the page is
ready. Under Xvfb on Linux, set `WEBKIT_DISABLE_DMABUF_RENDERER=1`:

```console
PIXHEET_HIDDEN_WINDOW=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 xvfb-run -a target/debug/pixheet-app --smoke
```

To lint the workflows, run `actionlint` and `zizmor --offline .github/workflows`.

Run the format check, clippy and the tests before you report a change as complete. For
a change to the app, also run the smoke test.

## Rules for code

- The golden fixtures in `crates/pixheet-core/tests/fixtures/` are the output of the
  Python version (Pillow 13.0.0). They keep the parity with Pillow. Do not change
  `expected.tsv` to make the test pass. Change it only on purpose, and say why in the
  commit body.
- `resample.rs`, `decode.rs` and `color.rs` port the rounding of Pillow 13. Test a change
  there against the golden test, not against an estimate. A faster loop must give the
  same bytes: integer sums can change their order, floating-point sums cannot.
- The crates forbid `unsafe` code (`unsafe_code = "forbid"` in the workspace lints).
- To update the toolchain, change the version in `rust-toolchain.toml`, fix the new
  clippy lints, and say so in the commit body. Do not let CI use "stable": a new lint
  then fails a pull request that did not change the code.
- The `.xlsx` writer must keep `sheet1.xml` and `styles.xml` byte for byte. The
  compressed bytes can differ.
- Add a test for each change in behavior. Tests must not show windows on the screen:
  the app tests drive `batch.rs` without a window, and the smoke test uses
  `PIXHEET_HIDDEN_WINDOW=1`.
- A message of the worker of the app changes only its card and the counts.
  `a_tick_of_500_cards_changes_one_card` in `batch.rs` checks this.
- The page of the app has no npm packages and no build step. It writes text only with
  `textContent`. The content security policy blocks inline scripts and inline `style`
  attributes: use classes, or set styles from JavaScript.
- The line length is 100 (`rustfmt` default).
- Do not commit `samples/`, `local-samples/`, `*.xlsx`, `profiles/` or `target/`. The
  `.gitignore` file excludes them.
- The primary development OS is Windows. Keep paths and shell examples portable.
- The workflows run on GitHub-hosted runners, and the repository is public. Never add a
  `pull_request_target` trigger. Pin each action to a full commit SHA. Only the release
  job of `release.yml` can write to the repository. The release workflow uses no cache.
- The release tasks of `xtask` must build with `--no-default-features`, without the image
  code. The release job runs them that way.
- When you change `version` in `Cargo.toml`, add a section `## <version>` at the top of
  `CHANGELOG.md`. Without it, the CI job "Check the version tag" fails. A merge that
  changes the version publishes a release.

## Commits

Use the semantic commit format with a gitmoji prefix. Use this format for all
commits unless the user asks for a different one.

```
<gitmoji> <type>(<scope>): <subject>

<body>

<footer>
```

Example: `✨ feat(cli): add --compact flag`

| Type | Gitmoji | Use for |
|---|---|---|
| `feat` | ✨ | A new feature |
| `fix` | 🐛 | A bug fix |
| `perf` | ⚡️ | A performance improvement |
| `refactor` | ♻️ | A code change that does not change behavior |
| `docs` | 📝 | Documentation only |
| `test` | ✅ | Tests only |
| `style` | 🎨 | Format or structure only |
| `build` | 📦 | Build system or dependencies |
| `ci` | 👷 | CI configuration |
| `chore` | 🔧 | Configuration and other maintenance |
| `revert` | ⏪ | Reverts a commit |
| any, with `!` | 💥 | A breaking change. This gitmoji replaces the gitmoji of the type. |

Rules:

- Use the Unicode emoji, not the `:shortcode:`. Use one gitmoji per commit.
- For a gitmoji that is not in the table, use the list at <https://gitmoji.dev>.
- Scopes: `cli`, `core`, `image`, `xlsx`, `gui`, `xtask`, `ci`, `tests`, `bench`,
  `scripts`, `docs`.
  Omit the scope when a commit touches several areas.
- Write the subject in the imperative, in lower case, and without a final period.
- Keep the first line, with the prefix, to 72 characters or fewer.
- Add `!` after the scope for a breaking change, and explain it in the footer:
  `💥 feat(core)!: ...` with `BREAKING CHANGE: ...`.
- Wrap the body at 72 characters. Say what changed and why.
- Put trailers such as `Co-Authored-By:` in the footer.
- Make one commit per logical change.

## Writing: ASD-STE100

Write all prose in Simplified Technical English (ASD-STE100). This applies to
docstrings, comments, README changes, commit messages, pull request text, error
messages, and replies to the user. Technical names and code identifiers are exempt,
for example `xlsx`, `LANCZOS`, `cargo`, and `rustfmt`.

Core rules:

- Write one topic per sentence.
- Keep instructions to 20 words or fewer, and descriptive sentences to 25 words or
  fewer.
- Keep paragraphs to six sentences or fewer.
- Use the active voice and the present tense.
- Use the imperative for instructions. Example: "Run the tests."
- Use `must` for a requirement and `can` for ability. Do not use `should`, `may`,
  or `might`.
- Keep the articles `a`, `an`, and `the`.
- Use a verb, not a noun made from a verb. Write "install the package", not
  "do the installation of the package".
- Use approved words: "make sure", not "ensure"; "do", not "perform"; "use", not
  "utilize"; "before", not "prior to"; "to", not "in order to"; "for example",
  not "e.g.".
- Do not join two words with a slash, for example "and/or".
- Use lists for steps and for parallel items.

New text and changed text must follow the rule.
