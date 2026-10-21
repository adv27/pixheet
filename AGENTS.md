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
| `src/pixheet/_image.py` | Private. Opens, normalizes, and fits the image. |
| `src/pixheet/_xlsx.py` | Private. Writes the `.xlsx` XML by hand. |
| `src/pixheet/_threads.py` | Private. Runs the resize and the zip compression on several threads. The resize helpers give the same pixels as the one-thread calls. The zip members decompress to the same bytes. The compressed bytes can differ. |
| `tests/` | Pytest suite. `tests/images.py` generates all test images. No real photos. |
| `bench/bench.py` | Benchmark on local sample photos. |
| `scripts/` | `excel_check.ps1` (Excel COM check, Windows) and `sample_pixels.py`. |

Modules with a leading underscore are private. Do not import them from outside the
package, except in tests and in `bench/bench.py`, which times each stage.

## Commands

```console
uv sync                                       # install; Python 3.15 per .python-version
uv run pytest                                 # about 20 s
uv run ruff check && uv run ruff format --check
uv run pixheet --help
```

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
- Do not commit `samples/`, `*.xlsx`, or `profiles/`. The `.gitignore` file
  excludes them.
- The primary development OS is Windows. Keep paths and shell examples portable.

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
- Scopes: `cli`, `core`, `image`, `xlsx`, `tests`, `bench`, `scripts`, `docs`.
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
