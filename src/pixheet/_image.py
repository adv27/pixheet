"""Image preparation: open, normalize to RGB, and fit to the spreadsheet limits."""

import math
import os
import struct
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from PIL import ExifTags, Image, ImageColor

# Only images with 16-bit transparency or an EXIF rotation use these.
lazy from PIL import ImageMath, ImageOps

from pixheet._xlsx import MAX_COLORS, MAX_COLUMNS, MAX_ROWS

# Any image whose longest side is at most this has at most 252 * 252 = 63,504
# pixels, so it always fits MAX_COLORS without counting.
_ALWAYS_FITS_SIDE = math.isqrt(MAX_COLORS)

type RGB = tuple[int, int, int]
type Source = str | os.PathLike[str] | IO[bytes] | Image.Image


@contextmanager
def opened(src: Source) -> Generator[Image.Image]:
    """Yield ``src`` as a loaded image. Close it after, unless the caller gave an Image."""
    if isinstance(src, Image.Image):
        yield src
        return
    if not isinstance(src, (str, os.PathLike)):
        with Image.open(src) as im:
            im.load()
            yield im
        return
    # Open the file here: given a file name, Pillow 13 memory-maps uncompressed TIFFs
    # and then scrambles those with an EXIF rotation (orientation 5-8).
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


def parse_background(value: str | RGB | None) -> RGB | None:
    """Parse a background option: a color name, ``#RRGGBB``, an (r, g, b) tuple, or "none".

    Returns None for "none" (drop alpha without compositing).
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        if len(value) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in value):
            return value
        raise ValueError(f"invalid background color {value!r}: expected (r, g, b) in 0..255")
    text = str(value).strip()
    if text.lower() == "none":
        return None
    try:
        rgb = ImageColor.getrgb(text)
    except ValueError:
        raise ValueError(
            f"invalid background color {value!r}: use white, black, #RRGGBB or none"
        ) from None
    return rgb[:3]


def check_max_side(max_side: int) -> None:
    if max_side < 0:
        raise ValueError(f"max_side must be >= 0, got {max_side}")


def _orientation(im: Image.Image) -> int:
    try:
        return im.getexif().get(ExifTags.Base.Orientation, 1)
    except SyntaxError, struct.error:  # an EXIF block that is not valid TIFF data
        return 1


def _to_8_bit(im: Image.Image) -> Image.Image:
    """Keep the top 8 bits of 16-bit samples (mode I) instead of letting RGB clip them."""
    key = im.info.get("transparency")
    out = im.point(lambda v: v / 256)
    out.info.pop("transparency", None)
    if isinstance(key, int):
        # The transparent gray level is a 16-bit value too: mask before scaling.
        mask = ImageMath.lambda_eval(lambda a: (a["im"] != key) * 255, im=im).convert("L")
        out = out.convert("L")
        out.putalpha(mask)
    return out


def normalize(im: Image.Image, background: RGB | None) -> Image.Image:
    """Return ``im`` upright and in mode RGB, with transparency composited onto ``background``.

    ``background=None`` drops alpha without compositing. Can return ``im`` itself.
    """
    # Pillow decodes a 16-bit PGM to mode I, scaled to the full 16-bit range. Other
    # mode I images (32-bit TIFF, IM, or made in memory) have no known scale, so their
    # values decide. Read the format first: the transposed image has no format.
    pgm_16_bit = im.format == "PPM"
    if _orientation(im) != 1:
        im = ImageOps.exif_transpose(im)
    if im.mode.startswith("I;16"):
        im = _to_8_bit(im.convert("I"))
    elif im.mode == "I" and (pgm_16_bit or im.getextrema()[1] > 255):
        im = _to_8_bit(im)
    if im.has_transparency_data:
        if im.mode == "La":
            im = im.convert("LA")
        rgba = im if im.mode == "RGBA" else im.convert("RGBA")  # paste only reads it
        if background is None:
            return rgba.convert("RGB")
        out = Image.new("RGB", rgba.size, background)
        out.paste(rgba, mask=rgba)
        return out
    return im if im.mode == "RGB" else im.convert("RGB")


def _cap_size(w: int, h: int, max_side: int) -> tuple[int, int]:
    """Largest size with the same aspect ratio that fits ``max_side`` and the sheet grid."""
    max_w = min(MAX_COLUMNS, max_side or MAX_COLUMNS)
    max_h = min(MAX_ROWS, max_side or MAX_ROWS)
    scale = min(1.0, max_w / w, max_h / h)
    if scale == 1.0:
        return w, h
    return (
        min(max_w, max(1, round(w * scale))),
        min(max_h, max(1, round(h * scale))),
    )


def _largest_passing(
    lo: int, hi: int, attempt: Callable[[int], Image.Image | None]
) -> Image.Image | None:
    """Binary-search ``lo..hi`` for the largest side whose ``attempt`` succeeds.

    The color count is not strictly monotonic in size, so this returns the result of
    the last attempt that passed, never an untested or failed midpoint.
    """
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        result = attempt(mid)
        if result is None:
            hi = mid - 1
        else:
            best, lo = result, mid + 1
    return best


def fit(im: Image.Image, max_side: int) -> Image.Image:
    """Shrink an RGB image until it fits the sheet grid, ``max_side`` and MAX_COLORS.

    First one LANCZOS resize caps the size; then a binary search finds the largest
    longest side (to the pixel) whose color count is within MAX_COLORS. Every search
    step resizes the capped image, never the (possibly much larger) original.
    Can return ``im`` itself.
    """
    check_max_side(max_side)
    if 0 in im.size:
        raise ValueError(f"the image has no pixels ({im.size[0]}x{im.size[1]})")
    size = _cap_size(*im.size, max_side)
    if size != im.size:
        im = im.resize(size, Image.Resampling.LANCZOS)
    if im.getcolors(MAX_COLORS) is not None:
        return im

    def shrunk(side: int) -> Image.Image:
        out = im.copy()
        out.thumbnail((side, side), Image.Resampling.LANCZOS)
        return out

    def attempt(side: int) -> Image.Image | None:
        candidate = shrunk(side)
        return candidate if candidate.getcolors(MAX_COLORS) is not None else None

    best = _largest_passing(_ALWAYS_FITS_SIDE + 1, max(im.size) - 1, attempt)
    return best if best is not None else shrunk(_ALWAYS_FITS_SIDE)


def fitted(
    src: Source, max_side: int, background: str | RGB | None
) -> tuple[tuple[int, int], Image.Image]:
    """Open, normalize and fit ``src``. Return the upright size and the fitted image.

    The fitted image does not depend on the source: it stays valid after the source
    file closes, and it is never the caller's own Image object.
    """
    check_max_side(max_side)
    bg = parse_background(background)
    with opened(src) as im:
        upright = normalize(im, bg)
        out = fit(upright, max_side)
        return upright.size, (out.copy() if out is im else out)
