# Pixheet

[![CI](https://github.com/adv27/pixheet/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/adv27/pixheet/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/adv27/pixheet/graph/badge.svg?token=6ZUOTE5GOF)](https://codecov.io/gh/adv27/pixheet)

Pixheet turns any image into an `.xlsx` spreadsheet where every cell is one pixel,
filled with that pixel's color. Open the file in a spreadsheet app, zoom out, and you
see the picture.

```console
$ pixheet photo.jpg
photo.jpg  3400x2267 -> 480x320  63,942 colors  153,600 cells  0.55s
```

The output is a plain `.xlsx` with one sheet, written for Microsoft Excel (verified in
Excel 16 on Windows) and readable by any .xlsx reader.

## Install

Pixheet needs Python 3.15 or newer and depends only on [Pillow](https://python-pillow.org/).
It is not on PyPI yet; install it from a checkout with [uv](https://docs.astral.sh/uv/):

```console
$ uv tool install .        # puts `pixheet` on your PATH
$ uv run pixheet --help    # or run it from the checkout without installing
```

`pip install .` works too.

## Command line

```
pixheet INPUT [INPUT ...] [-o OUTPUT] [--max-side N] [--background COLOR] [--compact] [-q]
```

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output OUTPUT` | `INPUT` with an `.xlsx` suffix | Output path. Only allowed with a single input. |
| `--max-side N` | `1000` | Cap on the longest side, in pixels. `0` means only the sheet limits apply. |
| `--background COLOR` | `white` | Color that transparent areas are blended onto: `white`, `black`, `#RRGGBB` (any CSS color name works), or `none` to drop the alpha channel as is. |
| `--compact` | off | Leave out cell references. See [Compact mode](#compact-mode). |
| `-q`, `--quiet` | off | Print errors only. |

Each converted file prints one line: input, original size, sheet size, colors, cells and
time. If an input fails, Pixheet reports it, carries on with the rest, and exits with
status 1. Wildcards such as `photos\*.jpg` are expanded even in shells that don't do it
themselves (cmd, PowerShell). `python -m pixheet` works too.

## Desktop app

Pixheet also has a desktop app. Drop images on its window, and the app converts each
image to an `.xlsx` file in the same folder. Install the app with the `gui` extra:

```console
$ uv tool install ".[gui]"     # or: pip install ".[gui]"
$ pixheet-gui                  # or: uv run --extra gui pixheet-gui from the checkout
```

- To convert images, drop them on the window, or click **Browse...**.
- Set the maximum side, the background, and compact mode before you start.
- A progress bar shows the progress of the current file. **Cancel** stops the batch.
- When an output file exists, the app asks one time for each batch. If you click
  **No**, the app does not convert those images.
- When a file has an error, the app converts the other files. At the end, it shows a
  list of the errors.
- **Open folder** shows the folder of the last output file.

The app uses Tkinter from the Python standard library. The `gui` extra adds
[tkinterdnd2](https://pypi.org/project/tkinterdnd2/) for the drop of files. Without
tkinterdnd2, the app opens without the drop function, and **Browse...** still works.
You can also give image paths as arguments: `pixheet-gui photo.jpg`.

The `gui` extra also adds [sv-ttk](https://pypi.org/project/sv-ttk/). It gives the
window the look of Windows 11:

- The toggle at the top right sets the theme: **System**, **Light**, or **Dark**. The
  title bar on Windows gets the same theme. You can use Tab and the arrow keys.
- With **System** (the default), the window is light or dark, the same as the Windows
  setting for apps. When you change the setting, the window changes in about 2 seconds.
  On Linux, the app reads the dark style setting of the desktop: the color scheme of
  the desktop portal, or else the GNOME setting. On macOS, **System** is light.
- The app saves your choice in `%APPDATA%\Pixheet\settings.ini`. On other systems, the
  file is `pixheet/settings.ini` in `$XDG_CONFIG_HOME`, or else in `~/.config`. A
  damaged file gives **System**.
- The environment variable `PIXHEET_THEME` sets the theme at the start: `system`,
  `light`, or `dark`. It wins over the saved choice.
- Without sv-ttk, the app opens with the standard Tk look and has no toggle.

On Linux, drag and drop needs libX11 1.7 or later, for example Ubuntu 22.04 or Debian 11.
With an older libX11, tkdnd stops the app when it loads. Thus the app does not load it,
and you use **Browse...**. The environment variable `PIXHEET_DND=0` turns off drag and
drop on all systems.

To build the Windows app yourself, see [Windows app](#windows-app). For Linux, see
[Linux app](#linux-app).

## Windows app

Pixheet.exe is the desktop app in one file. It does not need Python.

To get Pixheet.exe, do one of these steps:

- Download `Pixheet.exe` from the [Releases](https://github.com/adv27/pixheet/releases)
  page. Each merge that changes the version publishes a release.
- Download the newest build from the
  [Actions](https://github.com/adv27/pixheet/actions/workflows/release.yml) page.
  Open a run, and download the artifact `Pixheet-windows-x64`. You must sign in to
  GitHub to download an artifact. A GitHub-hosted Windows runner builds the exe for
  each pull request and for each push to `master`.

Each download includes the file `Pixheet.exe.sha256`. To compare the hash, run this
command in PowerShell:

```console
> (Get-FileHash Pixheet.exe -Algorithm SHA256).Hash.ToLower()
```

The exe does not have a code signature yet. Thus Windows SmartScreen can show the
message "Windows protected your PC". To start the app, click **More info**, then
**Run anyway**. If you do not want to run an exe without a signature, build it yourself.

To build Pixheet.exe from a checkout, run this command in PowerShell:

```console
> powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script builds `dist\Pixheet.exe`, about 13.8 MB, and tests it. Add `-OneDir` to
build the folder `dist\Pixheet\`. The folder starts faster, because the exe does not
extract itself each time. [packaging/README.md](packaging/README.md) gives more data
about the build and the CI.

To publish a release, change `version` in `pyproject.toml`, run `uv lock`, and merge the
pull request.

The exe has all the features of the app, but it does not have these parts:

- The Python modules for network, crypto, mail, XML, processes, and the command line,
  and the OpenSSL DLLs. The app does not use them.
- The Pillow modules that pixheet never calls, for example `ImageTk` and `ImageShow`.
- The docstrings and the `assert` statements in the bytecode.
- The Tcl and Tk files that the app does not use on Windows. Examples are the time zone
  data, most encodings, and the messages for other languages.

These changes make the exe about 6 MB smaller. The exe does not use UPX, because
antivirus programs flag UPX-packed exes.

## Linux app

`Pixheet-linux-x86_64` is the desktop app for Linux in one file. It does not need
Python. It runs on x86_64 Linux with glibc 2.27 or later, for example Ubuntu 18.04,
Debian 10, RHEL 8, and later versions.

To get the file, do one of these steps:

- Download `Pixheet-linux-x86_64` and `Pixheet-linux-x86_64.sha256` from the
  [Releases](https://github.com/adv27/pixheet/releases) page.
- Download the artifact `Pixheet-linux-x86_64` from the
  [Actions](https://github.com/adv27/pixheet/actions/workflows/release.yml) page.

Then run these commands in the download folder. A download does not keep the
permission to run the file, so `chmod +x` adds it:

```console
$ sha256sum -c Pixheet-linux-x86_64.sha256
$ chmod +x Pixheet-linux-x86_64
$ ./Pixheet-linux-x86_64
```

The app needs an X server. On a Wayland desktop, it runs through Xwayland. It also
needs `libxcb.so.1`, which each X11 desktop has.

Known limits:

- The text uses the core X fonts of Tk, without antialiasing, so it looks plain. The Tk
  in the app has no Xft. Some letters, for example Vietnamese letters in a file name,
  can show as boxes.
- When drag and drop cannot load, the app shows "Drag and drop is not available. Use
  Browse.". The app has its own libX11 1.7.2, so this is not usual.
- At each start, the app extracts itself to a folder `/tmp/_MEI...`. If `/tmp` does not
  let programs run (`noexec`), set `TMPDIR` to a different folder.
- To open a folder, the app uses `xdg-open`.
- The file has no menu entry and no desktop icon.

A GitHub-hosted runner builds the file in a Debian 11 container on `ubuntu-latest`. The
build passes the smoke test and the conversion test under Xvfb. Nobody has tested the
file on a real Linux desktop yet. Please report a problem in an issue.

To build the file yourself, use Debian 11 or a `debian:11` container. Install `git`,
`binutils`, `libx11-6`, `libxcursor1`, `xvfb` and `xauth`, then run this command:

```console
$ bash packaging/build.sh
```

The script builds `dist/Pixheet-linux-x86_64`, about 19.3 MB, and tests it. The build
stops on a system with a libX11 older than 1.7, because then tkdnd stops the app. It
also stops when the app would need a glibc newer than 2.27.
[packaging/README.md](packaging/README.md) tells why.

## Python API

```python
from pixheet import convert

result = convert("photo.jpg", "photo.xlsx")
print(result.size, result.colors, f"{result.seconds:.2f}s")
```

```python
convert(
    src,                    # path, binary file object, or PIL.Image.Image
    dst,                    # path or writable binary file object
    *,
    max_side=1000,          # 0 = no cap; only the sheet limits apply
    background="white",     # "white", "black", "#RRGGBB", (r, g, b), or "none"
    compact=False,
    progress=None,          # function that gets (rows_done, rows_total)
) -> ConvertResult
```

`ConvertResult` has `original_size` and `size` (width, height), `colors`, `cells` and
`seconds`. When `src` is a `PIL.Image.Image`, it is left open and unchanged. When `dst`
is a path, the file is written next to it and renamed into place, so a failed conversion
never leaves a half-written file and keeps any previous file.

Use `progress` to show the progress of a conversion, for example in a progress bar:

- `convert` calls `progress(rows_done, rows_total)` on the thread that called `convert`.
- The first call is `(0, rows_total)`. Then one call comes after each row of the sheet.
- The last call is `(rows_total, rows_total)`.
- An exception from `progress` stops the conversion, and `convert` raises it again.
  When `dst` is a path, an existing file at `dst` does not change.
  When `dst` is a stream, the stream then holds an incomplete file. Discard it.
- `convert` does not limit the number of calls. A slow `progress` function makes the
  conversion slow.

```python
def show(done, total):
    print(f"\r{done * 100 // total}%", end="")


convert("photo.jpg", "photo.xlsx", progress=show)
```

`prepare(src, max_side=..., background=...)` returns the RGB image that `convert` would
write, one pixel per cell, without writing anything.

## What happens to the image

1. **Orientation and color mode.** The EXIF orientation is applied, so phone photos come
   out upright. Images with transparency (RGBA, LA, palette or RGB/L with a transparent
   color) are blended onto the background color. Every other mode (palette, grayscale,
   CMYK, 16-bit grayscale, ...) is converted to RGB.
2. **Size cap.** If the image is larger than `max_side` or the sheet grid, it is resized
   once (LANCZOS, aspect ratio kept).
3. **Color fit.** If the image still has more distinct colors than a sheet can style, a
   search finds the largest longest side whose resized image fits. The result is exact
   to the pixel. The color count of each test size tells the search where to look
   next. For a photo, the search needs about 4 tests. A binary search needs about 9.

Pixheet uses up to 8 threads, one for each CPU, to resize the image and to compress
the sheet. The result is the same as with one thread: the same pixels and the same
cells. Only the compressed bytes in the file can differ a little.

## Limits

| Limit | Value | Why |
|---|---|---|
| Distinct colors | 63,999 | Excel allows 64,000 cell formats, and format 0 is the default style. Every color needs its own format. |
| Columns | 16,384 | Excel's column limit (A to XFD). |
| Rows | 1,048,576 | Excel's row limit. |
| Longest side | `max_side`, 1000 by default | Keeps files small and fast to open. |

Most photos have far more than 63,999 colors at full size, so they end up somewhere
around 100,000 to 250,000 cells (for example 480x320 or 300x440). Flat images such as
logos, pixel art and screenshots keep their full size up to `max_side`.

## Compact mode

`--compact` (`compact=True`) leaves out the optional `r="A1"` reference on every cell.
The spreadsheet format allows this: a reader puts each cell right after the previous one.
On the sample photos, files were 25 to 50% smaller. Writing the sheet took 13% less time
with 8 threads, and 23% less with one thread.

It is off by default because it has only been verified in Excel 16 and openpyxl. Don't
rely on it for Google Sheets, LibreOffice or Numbers until it has been tested there.

## Development

```console
$ uv sync                              # Python 3.15 per .python-version
$ uv run pytest                        # tests, including the golden test against xlsxwriter
$ uv run ruff check && uv run ruff format --check
```

Test images are generated in `tests/images.py`; no real photos are used. The golden test
builds the same fitted image with xlsxwriter (a dev dependency only) and compares
`sheet1.xml` and `styles.xml` byte for byte.

**Benchmark.** `bench/bench.py` converts every image in one or more folders and reports
best-of-N time, size, colors, cells and cells per second (`--stages` adds a per-stage
breakdown):

```console
$ uv run python bench/bench.py --samples path\to\photos -n 3 --stages
```

Folders come from `--samples` (repeatable), else `PIXHEET_SAMPLES` (several separated by
`;` on Windows), else `./samples`. Keep sample photos out of the repository: `samples/`
is git-ignored. The last line of the output gives the number of worker threads. To time
one thread, add `-X cpu_count=1`: `uv run python -X cpu_count=1 bench/bench.py ...`.

**Profiling.** Python 3.15's sampling profiler works on the benchmark as is:

```console
$ uv run python -m profiling.sampling run --flamegraph -o profiles/flamegraph.html bench/bench.py -n 3
```

**Excel check (Windows).** `scripts/excel_check.ps1` opens every `.xlsx` in a folder with
Excel through COM automation and reports the used range, row height and column width,
and compares 80 random cell colors with the source image of the same name. Excel runs
hidden with alerts off, and in that mode it refuses to open a file it would offer to
repair, so a pass also means "no repair prompt". `-Snapshot DIR` exports each sheet as a
PDF rendered by Excel. Excel prints very narrow columns wider than it shows them, so these
PDFs look about 1.2x stretched sideways; on screen every cell is a 0.75 x 0.75 pt square.

```console
$ uv run pixheet samples\*.jpg
$ powershell -ExecutionPolicy Bypass -File scripts\excel_check.ps1 -Folder samples -Snapshot out
```

## License

MIT
