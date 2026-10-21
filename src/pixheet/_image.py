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

from pixheet import _threads
from pixheet._xlsx import MAX_COLORS, MAX_COLUMNS, MAX_ROWS

# Any image whose longest side is at most this has at most 252 * 252 = 63,504
# pixels, so it always fits MAX_COLORS without counting.
_ALWAYS_FITS_SIDE = math.isqrt(MAX_COLORS)

type RGB = tuple[int, int, int]
type Source = str | bytes | os.PathLike[str] | IO[bytes] | Image.Image


@contextmanager
def opened(src: Source) -> Generator[Image.Image]:
    """Yield ``src`` as a loaded image. Close it after, unless the caller gave an Image."""
    if isinstance(src, Image.Image):
        yield src
        return
    if not isinstance(src, (str, bytes, os.PathLike)):
        with Image.open(src) as im:
            im.load()
            yield im
        return
    # Open the file here: given a file name, Pillow 13 memory-maps uncompressed TIFFs
    # and then scrambles those with an EXIF rotation (orientation 5-8).
    with Path(os.fsdecode(src)).open("rb") as f:
        try:
            im = Image.open(f)
        except Image.UnidentifiedImageError:
            raise Image.UnidentifiedImageError(
                f"cannot identify image file {os.fsdecode(src)!r}"
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


# With one tested end and no measured slope above _MIN_SLOPE, guess that colors grow
# as side ** 1.6. This is about what the sample photos show between 250 and 1000 px.
_GROWTH = 1.6
_MIN_SLOPE = 0.2


def _largest_passing(
    lo: int,
    hi: int,
    attempt: Callable[[int], tuple[Image.Image, int]],
    first: int,
) -> Image.Image | None:
    """Find the largest side in ``lo..hi`` whose image has at most MAX_COLORS colors.

    ``lo`` passes and ``hi`` fails without a test. ``attempt(side)`` gives the image
    and its color count (see ``_color_count``).

    The color counts guide each next probe. Colors grow about as a power of the side.
    The estimate is the side where a line through the two bracket ends crosses
    MAX_COLORS on a log-log scale. With only one tested end, the line goes through its
    two newest probes when their slope is more than _MIN_SLOPE. If not, the line goes
    through the newest probe with the slope _GROWTH.

    After a pass, the search tests just above the estimate. After a fail, it tests
    just below it. Thus the bracket closes from both ends. When the results repeat on
    one side, the estimate is off in one direction, so each next probe goes 1, 2, 4,
    ... sides further. If a tested bracket does not halve in four probes, the next
    probe bisects it. So the search needs a few probes when the estimates are good. On
    unusual color curves, it needs up to about 3 times as many probes as a binary
    search.

    The result is the image of the largest side that passed, and the side after it
    failed. The color count is not strictly monotonic in size, so the search never
    returns an untested or failed side.
    """
    best = None
    # (log side, log(colors / MAX_COLORS)) of the tested probes on each side, newest
    # last: passes (<= 0) and fails (> 0).
    passes: list[tuple[float, float]] = []
    fails: list[tuple[float, float]] = []
    widths = [hi - lo]
    side = first
    last_passed: bool | None = None
    streak = 0
    while hi - lo > 1:
        stalled = passes and fails and len(widths) > 4 and widths[-1] * 2 > widths[-5]
        if not lo < side < hi or stalled:
            side = (lo + hi) // 2
            widths = [hi - lo]
        image, colors = attempt(side)
        passed = colors <= MAX_COLORS
        point = (math.log(side), math.log(colors / MAX_COLORS))
        if passed:
            best, lo = image, side
            passes.append(point)
        else:
            hi = side
            fails.append(point)
        widths.append(hi - lo)
        streak = streak + 1 if passed == last_passed else 1
        last_passed = passed

        if passes and fails:
            (x0, f0), (x1, f1) = passes[-1], fails[-1]
        else:
            known = passes or fails
            (x0, f0), (x1, f1) = known[-2:] if len(known) > 1 else (known[0], known[0])
            slope = (f1 - f0) / (x1 - x0) if x1 != x0 else 0.0
            x0, f0, x1 = x1, f1, x1 + 1.0
            f1 = f0 + (slope if slope > _MIN_SLOPE else _GROWTH)
        estimate = math.floor(math.exp(x0 - f0 * (x1 - x0) / (f1 - f0)))
        step = 2 ** (streak - 1)
        side = max(estimate + 1, lo + step) if passed else min(estimate, hi - step)
    return best


# getcolors() makes a hash table as large as its limit. A limit of w * h would use a lot
# of memory on a large probe, and a count far above MAX_COLORS fails anyway.
_COUNT_CAP = 4 * MAX_COLORS


def _color_count(im: Image.Image) -> int:
    """The number of colors in ``im``: exact up to _COUNT_CAP, _COUNT_CAP + 1 above it."""
    colors = im.getcolors(min(_COUNT_CAP, im.width * im.height))
    return _COUNT_CAP + 1 if colors is None else len(colors)


def fit(im: Image.Image, max_side: int) -> Image.Image:
    """Shrink an RGB image until it fits the sheet grid, ``max_side`` and MAX_COLORS.

    First one LANCZOS resize caps the size. Then a search that the color counts guide
    finds the largest longest side, to the pixel, whose color count is within
    MAX_COLORS. Every search step resizes the capped image, never the (possibly much
    larger) original. Can return ``im`` itself.
    """
    check_max_side(max_side)
    if 0 in im.size:
        raise ValueError(f"the image has no pixels ({im.size[0]}x{im.size[1]})")
    size = _cap_size(*im.size, max_side)
    if size != im.size:
        im = _threads.resize(im, size)  # the same as im.resize(size, LANCZOS)
    if im.getcolors(MAX_COLORS) is not None:
        return im

    def shrunk(side: int) -> Image.Image:
        return _threads.thumbnail(im, side)  # a copy after thumbnail((side, side))

    def attempt(side: int) -> tuple[Image.Image, int]:
        candidate = shrunk(side)
        return candidate, _color_count(candidate)

    # Start at the side that gives about 2 * MAX_COLORS pixels: most photos fit there.
    w, h = im.size
    first = round(max(w, h) * math.sqrt(2 * MAX_COLORS / (w * h)))
    best = _largest_passing(_ALWAYS_FITS_SIDE, max(w, h), attempt, first)
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
