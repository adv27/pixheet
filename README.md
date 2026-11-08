# Pixheet

[![CI](https://github.com/adv27/pixheet/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/adv27/pixheet/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/adv27/pixheet/graph/badge.svg?token=6ZUOTE5GOF)](https://codecov.io/gh/adv27/pixheet)

Pixheet turns an image into an `.xlsx` spreadsheet. Each cell is one pixel and has the
color of that pixel. Open the file in a spreadsheet app, zoom out, and you see the
picture.

```console
$ pixheet photo.jpg
photo.jpg  4032x3024 -> 297x223  63,758 colors  66,231 cells  0.13s
```

The output is a plain `.xlsx` file with one sheet. It is written for Microsoft Excel
(verified in Excel 16 on Windows), and any `.xlsx` reader can read it.

Pixheet is written in Rust. It has three parts:

- `pixheet`, a command for the terminal;
- Pixheet, a desktop app for Windows, Linux and macOS;
- `pixheet-core`, the library that both use.

The first versions of Pixheet were in Python. The branch
[`python-legacy`](https://github.com/adv27/pixheet/tree/python-legacy) keeps the last
Python version (v0.1.0).

## Install

Build Pixheet from a checkout. You need Rust 1.88 or later, `cmake`, and on x86-64 also
`nasm` (for libjpeg-turbo).

```console
$ cargo install --path crates/pixheet-cli      # puts `pixheet` on your PATH
$ cargo build --release -p pixheet-app         # the desktop app: target/release/pixheet-app
```

On Linux, the desktop app needs WebKitGTK 4.1 and GTK 3, for example Ubuntu 22.04,
Debian 12 or later. To build it, install `libwebkit2gtk-4.1-dev`, `libgtk-3-dev` and
`librsvg2-dev`.

The release workflow builds both programs for Windows x64, Linux x64 and macOS arm64.
The next release after v0.1.0 will have these files on the
[Releases](https://github.com/adv27/pixheet/releases) page.

## Command line

```
pixheet INPUT [INPUT ...] [-o OUTPUT] [--max-side N] [--background COLOR] [--compact] [-q]
```

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output OUTPUT` | `INPUT` with an `.xlsx` suffix | The output path. Only for one input. |
| `--max-side N` | `1000` | The cap on the longest side, in pixels. `0` means only the sheet limits apply. |
| `--background COLOR` | `white` | The color under transparent areas: a color name (`white`, `black`, all CSS names), `#RRGGBB`, `rgb(...)`, `hsl(...)`, or `none` to drop the alpha. |
| `--compact` | off | Leave out the cell references. Read [Compact mode](#compact-mode). |
| `-q`, `--quiet` | off | Print only the errors. |

Each converted file prints one line: the input, the original size, the sheet size, the
colors, the cells and the time. When an input fails, Pixheet tells why, converts the
other inputs, and stops with the exit status 1. The command expands wildcards such as
`photos\*.jpg` itself, also in cmd and PowerShell.

The command uses all CPUs, at most 8. To set the number of threads, set the environment
variable `RAYON_NUM_THREADS`.

## Desktop app

Drop images or folders on the window, or click **Add images** or **Add folder**. Each
image gets a card. After the fit, the card shows a preview of the sheet. A done card can
open the file, show it in its folder, or convert it again.

The settings are in the sidebar:

- **Longest side**: the cap on the size, the same as `--max-side`.
- **Transparent areas**: white, black, the colors as they are, or a color of your own.
- **Save to**: next to each image, or in one folder.
- **If the file exists**: keep both (`photo (2).xlsx`), replace it, or skip the image.
- **Start when images are added**, **Include subfolders**, and **Compact file**.
- The button at the top right sets the theme: system, light or dark.

A change of the settings applies to the images that have not started. The app saves the
settings in `settings.json` in its config folder, for example
`%APPDATA%\io.github.adv27.pixheet` on Windows or `~/.config/io.github.adv27.pixheet` on
Linux.

## Library

```rust
use std::path::Path;
use pixheet_core::{Options, convert};

let result = convert(Path::new("photo.jpg"), Path::new("photo.xlsx"), &Options::default(), None)?;
println!("{}x{} {} colors", result.size.0, result.size.1, result.colors);
```

`convert` writes a temporary file next to the output, then renames it over the output.
Thus a failed conversion never leaves a half-written file, and an old file stays as it
was. The optional progress function gets `(rows_done, rows_total)` and can stop the
conversion. `load`, `fit_image` and `write` are the three steps of `convert`, and
`prepare` gives the image that `convert` writes.

## What happens to the image

1. **Orientation and color mode.** Pixheet applies the EXIF orientation, so photos from
   a phone come out upright. It blends transparent areas onto the background color. It
   converts each other mode (palette, gray, CMYK, 16-bit) to 8-bit RGB.
2. **Size cap.** When the image is larger than `--max-side` or the sheet grid, one
   LANCZOS resize makes it smaller, with the same aspect ratio.
3. **Color fit.** When the image still has more colors than a sheet can style, a search
   finds the largest size whose colors fit. The color counts of the tested sizes guide
   the search, so a photo needs about 4 tests.

The decode, the resize and the color rules give the same pixels as Pillow 13, the image
library of the Python version. The golden test checks this for 139 cases.

## Limits

| Limit | Value | Why |
|---|---|---|
| Colors | 63,999 | Excel allows 64,000 cell formats, and format 0 is the default style. Each color needs its own format. |
| Columns | 16,384 | The column limit of Excel (A to XFD). |
| Rows | 1,048,576 | The row limit of Excel. |
| Longest side | `--max-side`, 1000 by default | Small files open fast. |

Most photos have far more than 63,999 colors at full size. Thus they become about
100,000 to 250,000 cells, for example 480x320. Flat images such as logos, pixel art and
screenshots keep their full size up to `--max-side`.

## Compact mode

`--compact` leaves out the optional `r="A1"` reference of each cell. The spreadsheet
format allows this: a reader puts each cell after the cell before it. The files are 25 to
50% smaller. Only Excel 16 and openpyxl are tested with compact files, so the mode is
off by default.

## Speed

On a 4-core Linux machine, the command converts a 12 MP photo in about 0.13 s, 3.8 times
faster than the Python version. A batch of 5 sample images takes 0.25 s, 4.8 times
faster. [docs/rust-port/PROFILE.md](docs/rust-port/PROFILE.md) has the profile and the
numbers.

## Development

```console
$ cargo fmt --all --check
$ cargo clippy --workspace --all-targets -- -D warnings
$ cargo test --workspace
$ cargo llvm-cov --workspace --open            # the coverage (cargo-llvm-cov)
$ cargo run -p pixheet-app                     # the desktop app
$ cargo xtask samples local-samples            # synthetic sample images
$ cargo build --release -p pixheet-cli && cargo xtask bench local-samples
```

[AGENTS.md](AGENTS.md) has the layout, the commands and the rules of the project.
[docs/rust-port/](docs/rust-port/) records the port from Python to Rust.

**Excel check (Windows).** `scripts/excel_check.ps1` opens each `.xlsx` file of a folder
with Excel through COM automation. It reports the used range, the row height and the
column width, and it compares 80 random cell colors with the source image.

```console
> cargo run --release -p pixheet-cli -- samples\*.jpg
> powershell -ExecutionPolicy Bypass -File scripts\excel_check.ps1 -Folder samples
```

## License

MIT. Read [LICENSE](LICENSE).
