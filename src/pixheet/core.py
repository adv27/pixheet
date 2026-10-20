"""The public conversion API."""

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from PIL import Image

from pixheet._image import RGB, Source, fitted
from pixheet._xlsx import write_xlsx

type Destination = str | os.PathLike[str] | IO[bytes]

DEFAULT_MAX_SIDE = 1000
DEFAULT_BACKGROUND = "white"


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


def prepare(
    src: Source,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    background: str | RGB | None = DEFAULT_BACKGROUND,
) -> Image.Image:
    """Return the RGB image that :func:`convert` would write, one pixel per cell."""
    return fitted(src, max_side, background)[1]


def convert(
    src: Source,
    dst: Destination,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    background: str | RGB | None = DEFAULT_BACKGROUND,
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
    if _same_file(src, dst):
        raise ValueError(f"the output {os.fspath(dst)!r} is the input image; choose another path")
    original_size, image = fitted(src, max_side, background)
    colors = _write(image, dst, compact)
    w, h = image.size
    return ConvertResult(
        original_size=original_size,
        size=(w, h),
        colors=colors,
        cells=w * h,
        seconds=time.perf_counter() - start,
    )


def _same_file(src: Source, dst: Destination) -> bool:
    paths = (str, os.PathLike)
    if not (isinstance(src, paths) and isinstance(dst, paths)):
        return False
    try:
        return Path(src).samefile(dst)
    except OSError, ValueError:  # a path that does not exist, or that is not valid
        return False


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
    except BaseException as e:
        tmp.unlink(missing_ok=True)
        if isinstance(e, OSError) and e.filename == str(tmp):
            # Report the target, not the hidden temp file.
            raise type(e)(e.errno, e.strerror, str(path)) from e
        raise
    return colors
