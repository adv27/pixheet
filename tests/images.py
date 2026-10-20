"""Synthetic test images. Never use real photos as test data."""

import io
import random
from collections.abc import Callable

from PIL import ExifTags, Image


def noise(w: int, h: int, colors: int, seed: int = 0) -> Image.Image:
    """An RGB image with exactly ``colors`` distinct colors (needs ``w * h >= colors``)."""
    assert w * h >= colors
    rng = random.Random(seed)
    palette = rng.sample(range(1 << 24), colors)
    picks = palette + rng.choices(palette, k=w * h - colors)
    rng.shuffle(picks)
    data = b"".join(p.to_bytes(3, "big") for p in picks)
    return Image.frombytes("RGB", (w, h), data)


def gradient(w: int, h: int) -> Image.Image:
    data = bytes(
        channel
        for y in range(h)
        for x in range(w)
        for channel in (x * 255 // max(w - 1, 1), y * 255 // max(h - 1, 1), (x + y) % 256)
    )
    return Image.frombytes("RGB", (w, h), data)


def busy(w: int, h: int) -> Image.Image:
    """A large image with far more than 63,999 colors, built quickly in C."""
    red = Image.linear_gradient("L").resize((w, h))
    green = Image.linear_gradient("L").rotate(90).resize((w, h))
    blue = Image.effect_noise((w, h), 64)
    return Image.merge("RGB", (red, green, blue))


def many_colors() -> Image.Image:
    """256x256 with R = x and G = y: 65,536 colors, more than a sheet allows."""
    ramp = Image.linear_gradient("L")  # 256x256, value = y
    black = Image.new("L", ramp.size)
    return Image.merge("RGB", (ramp.transpose(Image.Transpose.TRANSPOSE), ramp, black))


def palette_with_transparency() -> Image.Image:
    im = Image.new("P", (8, 8), 0)
    im.putpalette([255, 0, 0, 0, 0, 255] + [0] * (256 * 3 - 6))
    for x in range(8):
        im.putpixel((x, x), 1)
    im.info["transparency"] = 0  # red is transparent, blue is opaque
    return im


def encoded(im: Image.Image, fmt: str, **params) -> io.BytesIO:
    """``im`` saved in ``fmt`` to an in-memory file, rewound to the start."""
    buf = io.BytesIO()
    im.save(buf, fmt, **params)
    buf.seek(0)
    return buf


def exif_rotated(size: tuple[int, int], orientation: int, fmt: str = "JPEG") -> io.BytesIO:
    """An image file whose EXIF orientation tells viewers to rotate it."""
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    return encoded(Image.new("RGB", size, (200, 10, 10)), fmt, exif=exif)


# The standard images that most tests use, by name.
SAMPLES: dict[str, Callable[[], Image.Image]] = {
    "gradient": lambda: gradient(64, 48),
    "noise": lambda: noise(50, 40, 300),  # exactly 300 colors
    "one-pixel": lambda: Image.new("RGB", (1, 1), (1, 2, 3)),
    "many-colors": many_colors,
    "palette-transparent": palette_with_transparency,
}

# Every mode that the converter must accept, and the RGB that it gives on white.
MODE_CASES = {
    "L": (Image.new("L", (3, 2), 100), (100, 100, 100)),
    "LA": (Image.new("LA", (3, 2), (100, 255)), (100, 100, 100)),
    "LA-transparent": (Image.new("LA", (3, 2), (100, 0)), (255, 255, 255)),
    "1": (Image.new("1", (3, 2), 1), (255, 255, 255)),
    "P": (Image.new("P", (3, 2), 0), (0, 0, 0)),
    "CMYK": (Image.new("CMYK", (3, 2), (0, 255, 0, 0)), (255, 0, 255)),
    "RGBA-opaque": (Image.new("RGBA", (3, 2), (10, 20, 30, 255)), (10, 20, 30)),
    "RGBA-transparent": (Image.new("RGBA", (3, 2), (10, 20, 30, 0)), (255, 255, 255)),
    "I-8bit": (Image.new("I", (3, 2), 100), (100, 100, 100)),
    "I;16": (Image.new("I;16", (3, 2), 32768), (128, 128, 128)),
    "I;16-dark": (Image.new("I;16", (3, 2), 200), (0, 0, 0)),
    "I;16B": (Image.new("I;16B", (3, 2), 65535), (255, 255, 255)),
    "I-16bit": (Image.new("I", (3, 2), 32768), (128, 128, 128)),
}
