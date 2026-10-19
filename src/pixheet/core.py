"""The public conversion API."""

import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from PIL import Image

from pixheet._image import RGB, fit, normalize, parse_background
from pixheet._xlsx import write_xlsx

type Source = str | os.PathLike[str] | IO[bytes] | Image.Image
type Destination = str | os.PathLike[str] | IO[bytes]


@dataclass(frozen=True, slots=True)
class ConvertResult:
    """What :func:`convert` did."""

    original_size: tuple[int, int]
    """(width, height) of the source image, after EXIF orientation."""
    size: tuple[int, int]
    """(width, height) of the sheet: one cell per pixel."""
    colors: int
    """Number of distinct colors, i.e. cell styles."""
    cells: int
    """Number of cells written (width * height)."""
    seconds: float
    """Wall-clock time of the whole conversion."""


@contextmanager
def _opened(src: Source) -> Iterator[Image.Image]:
    if isinstance(src, Image.Image):
        yield src
        return
    if not isinstance(src, (str, os.PathLike)):
        with Image.open(src) as im:
            im.load()
            yield im
        return
    # Open the file ourselves: given a file name, Pillow 13 memory-maps uncompressed
    # TIFFs and then scrambles those with an EXIF rotation (orientation 5-8).
    with Path(src).open("rb") as f:
        try:
            im = Image.open(f)
        except Image.UnidentifiedImageError:
            raise Image.UnidentifiedImageError(
                f"cannot identify image file {os.fspath(src)!r}"
            ) from None
        with im:
            im.load()
            yield im


def _check_max_side(max_side: int) -> None:
    if max_side < 0:
        raise ValueError(f"max_side must be >= 0, got {max_side}")


def prepare(
    src: Source, *, max_side: int = 1000, background: str | RGB | None = "white"
) -> Image.Image:
    """Return the RGB image that :func:`convert` would write, one pixel per cell."""
    _check_max_side(max_side)
    bg = parse_background(background)
    with _opened(src) as im:
        out = fit(normalize(im, bg), max_side)
        # Detach from the source: it is closed (or still owned by the caller).
        return out.copy() if out is im else out


def convert(
    src: Source,
    dst: Destination,
    *,
    max_side: int = 1000,
    background: str | RGB | None = "white",
    compact: bool = False,
) -> ConvertResult:
    """Convert an image into an .xlsx spreadsheet where every cell is one pixel.

    Args:
        src: A path, a binary file object, or a ``PIL.Image.Image`` (left open and
            unchanged).
        dst: A path or a writable binary file object. A path is written atomically:
            on failure, an existing file there is left as it was.
        max_side: Cap on the longest side in pixels; 0 means only the sheet limits
            (16,384 columns, 1,048,576 rows) apply.
        background: Color that transparent areas are composited onto: a name such
            as "white" or "black", "#RRGGBB", an (r, g, b) tuple, or "none" to drop
            the alpha channel without compositing.
        compact: Leave out the optional ``r="A1"`` cell references. Smaller and
            faster, but only verified in Excel and openpyxl.

    The image is shrunk, keeping its aspect ratio, to the largest size that fits
    ``max_side``, the sheet grid and 63,999 distinct colors (Excel's cell-format limit).
    """
    start = time.perf_counter()
    _check_max_side(max_side)
    bg = parse_background(background)
    if _same_file(src, dst):
        raise ValueError(f"the output {os.fspath(dst)!r} is the input image; choose another path")
    with _opened(src) as im:
        upright = normalize(im, bg)
        original_size = upright.size
        fitted = fit(upright, max_side)
        colors = _write(fitted, dst, compact)
    w, h = fitted.size
    return ConvertResult(
        original_size=original_size,
        size=(w, h),
        colors=colors,
        cells=w * h,
        seconds=time.perf_counter() - start,
    )


def _same_file(src: Source, dst: Destination) -> bool:
    paths = (str, os.PathLike)
    return (
        isinstance(src, paths)
        and isinstance(dst, paths)
        and Path(dst).exists()
        and Path(src).exists()
        and Path(src).samefile(dst)
    )


def _write(im: Image.Image, dst: Destination, compact: bool) -> int:
    if not isinstance(dst, (str, os.PathLike)):
        return write_xlsx(im, dst, compact=compact)
    path = Path(dst)
    # Write next to the target, then rename over it. The name has a fixed length so a
    # long but valid target name cannot push it past the file-name limit.
    tmp = path.with_name(f".pixheet-{secrets.token_hex(8)}.tmp")
    try:
        with tmp.open("xb") as f:
            colors = write_xlsx(im, f, compact=compact)
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        if e.filename != str(tmp):
            raise
        # Report the target, not the hidden temp file.
        raise type(e)(e.errno, e.strerror, str(path)) from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return colors
