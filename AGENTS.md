# AGENTS.md

This file gives instructions to AI agents and other contributors who work in this repository.

## Project

Pixheet converts an image to an `.xlsx` spreadsheet. Each cell is one pixel and has
the color of that pixel. The package has one runtime dependency, Pillow.
Read [README.md](README.md) for usage, the Python API, and the Excel limits.

## Layout

| Path | Content |
|---|---|
| `src/pixheet/__init__.py` | The public API. The `__all__` list is the public surface. |
| `src/pixheet/core.py` | `convert()`, `prepare()`, `ConvertResult`. |
| `src/pixheet/cli.py` | The `pixheet` command. |
| `src/pixheet/gui.py` | The `pixheet-gui` desktop app (Tkinter, with tkinterdnd2 for the file drop and sv-ttk for the theme). |
| `src/pixheet/_plan.py` | Private. The plan of a batch for the desktop app, without Tk: the images of a drop, the output names, the row states and counts, and the texts of the list. |
| `src/pixheet/_image.py` | Private. Opens, normalizes, and fits the image. |
| `src/pixheet/_xlsx.py` | Private. Writes the `.xlsx` XML by hand. |
| `src/pixheet/_threads.py` | Private. Runs the resize and the zip compression on several threads. The resize helpers give the same pixels as the one-thread calls. The zip members decompress to the same bytes. The compressed bytes can differ. |
| `tests/` | Pytest suite. `tests/images.py` generates all test images. No real photos. |
| `bench/bench.py` | Benchmark on local sample photos. |
| `scripts/` | `excel_check.ps1` (Excel COM check, Windows) and `sample_pixels.py`. |
| `packaging/` | The build of `Pixheet.exe`: the PyInstaller spec `pixheet.spec`, the Tcl and Tk trim `trim_tcltk.py`, the entry script, the icon and `make_icon.py`, `build.ps1`, `check_exe.py`, `release_version.py` for the release job, and the hook `runner_job_started.ps1` for a later self-hosted runner. Read `packaging/README.md`. |
| `.github/workflows/` | `ci.yml`: tests and lint on GitHub-hosted Windows. `windows-exe.yml`: the exe build on GitHub-hosted Windows, and the release. |

Modules with a leading underscore are private. Do not import them from outside the
package, except in tests and in `bench/bench.py`, which times each stage.

## Commands

```console
uv sync                                       # install; Python 3.15 per .python-version
uv run pytest                                 # about 20 s
uv run pytest --cov --cov-report=term         # with the coverage, as in CI
uv run ruff check && uv run ruff format --check
uv run pixheet --help
uv run pixheet-gui                            # the desktop app
```

To build the desktop app for Windows into `dist/Pixheet.exe` and test it, and to lint
the workflows, run these commands in PowerShell:

```console
powershell -ExecutionPolicy Bypass -File packaging/build.ps1   # add -OneDir for a folder
uvx zizmor .github/workflows                                   # lint the workflows
uvx --from actionlint-py actionlint
```

The GUI tests in `tests/test_gui.py` need a display. Without a display, they skip.

Run the tests, the lint, and the format check before you report a change as complete.

## Rules for code

- The code targets Python 3.15 only. `requires-python` is `>=3.15` and ruff targets
  `py315`. You can use Python 3.15 syntax and library features.
- Pytest sets `filterwarnings = ["error"]`. Each warning fails the test run.
- The golden test compares `sheet1.xml` and `styles.xml` with the output of
  xlsxwriter, byte for byte. If you change the XML in `_xlsx.py`, make sure that
  test still passes. If you change the test on purpose, say so in the commit body.
- A regular expression in `tests/test_cli.py` matches the CLI report line.
  If you change the report format, update that test.
- The line length is 100. Ruff ignores `E501` only in `_xlsx.py`.
- Add a test for each change in behavior.
- Do not commit `samples/`, `*.xlsx`, `profiles/`, `build/`, or `dist/`. The
  `.gitignore` file excludes them.
- In `gui.py`, only the main thread can call Tk. The worker thread sends messages
  through a queue. A Tk object that Python collects on the worker thread causes an
  error, so the GUI tests collect garbage on the main thread after each test.
- `App` does not load the Sun Valley theme. Only `main()` loads it, so most window tests
  use the default ttk theme. A test that needs the theme calls `apply_theme()`.
- The primary development OS is Windows. Keep paths and shell examples portable.
- `packaging/pixheet.spec` leaves many modules out of `Pixheet.exe`. A new import of
  such a module can break the exe while the tests pass. After a change to the imports in
  `src/pixheet`, run `packaging/build.ps1`.
- The workflows run on GitHub-hosted runners, and the repository is public. Never add a
  `pull_request_target` trigger. Pin each action to a full commit SHA. Only the release
  job of `windows-exe.yml` can write to the repository.
- Before you move a job to a self-hosted runner, you MUST remove the `pull_request`
  trigger of its workflow. Read "Later: self-hosted runner" in `packaging/README.md`.

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
- Scopes: `cli`, `core`, `image`, `xlsx`, `gui`, `packaging`, `ci`, `tests`, `bench`,
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
for example `xlsx`, `LANCZOS`, `pytest`, and `uv`.

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

The current README does not follow this rule. Do not rewrite it only for
style. New text and changed text must follow the rule.
