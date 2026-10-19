# Pixheet

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
) -> ConvertResult
```

`ConvertResult` has `original_size` and `size` (width, height), `colors`, `cells` and
`seconds`. When `src` is a `PIL.Image.Image`, it is left open and unchanged. When `dst`
is a path, the file is written next to it and renamed into place, so a failed conversion
never leaves a half-written file and keeps any previous file.

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
   binary search finds the largest longest side, to the pixel, whose resized image fits.
   Fewer pixels means fewer colors, so the search shrinks the image only as much as it
   has to.

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
On the sample photos, writing the sheet took about a third less time and files were 25 to
50% smaller.

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
is git-ignored.

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
