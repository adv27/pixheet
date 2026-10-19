import io

import pytest
from images import MODE_CASES, busy, noise
from PIL import Image

from pixheet._image import (
    MAX_COLORS,
    _cap_size,
    _largest_passing,
    fit,
    normalize,
    parse_background,
)

WHITE = (255, 255, 255)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("white", (255, 255, 255)),
        ("black", (0, 0, 0)),
        ("#12AbEf", (0x12, 0xAB, 0xEF)),
        ("  NONE ", None),
        ("none", None),
        (None, None),
        ((1, 2, 3), (1, 2, 3)),
        ("#11223344", (0x11, 0x22, 0x33)),
    ],
)
def test_parse_background(value, expected):
    assert parse_background(value) == expected


@pytest.mark.parametrize(
    "value", ["", "nope", "#12345", "#GGGGGG", (1, 2), (0, 0, 256), (1.0, 2, 3)]
)
def test_parse_background_rejects(value):
    with pytest.raises(ValueError, match="invalid background"):
        parse_background(value)


@pytest.mark.parametrize("case", MODE_CASES)
def test_normalize_modes(case):
    im, expected = MODE_CASES[case]
    out = normalize(im, WHITE)
    assert out.mode == "RGB"
    assert out.size == im.size
    assert out.getpixel((0, 0)) == expected


def test_normalize_premultiplied_la():
    out = normalize(Image.new("La", (2, 2), (0, 0)), WHITE)
    assert out.mode == "RGB"
    assert out.getpixel((0, 0)) == WHITE


@pytest.mark.parametrize(
    ("background", "expected"),
    [((255, 255, 255), (255, 127, 127)), ((0, 0, 0), (128, 0, 0)), (None, (255, 0, 0))],
)
def test_normalize_composites_alpha(background, expected):
    im = Image.new("RGBA", (2, 2), (255, 0, 0, 128))
    got = normalize(im, background).getpixel((0, 0))
    assert all(abs(a - b) <= 1 for a, b in zip(got, expected, strict=True))


def test_normalize_palette_transparency(p_transparent):
    out = normalize(p_transparent, (0, 255, 0))
    assert out.getpixel((0, 0)) == (0, 0, 255)  # opaque blue diagonal
    assert out.getpixel((1, 0)) == (0, 255, 0)  # transparent red -> background
    assert normalize(p_transparent, None).getpixel((1, 0)) == (255, 0, 0)


def test_normalize_rgb_transparency_key():
    im = Image.new("RGB", (2, 1), (9, 9, 9))
    im.putpixel((1, 0), (1, 1, 1))
    im.info["transparency"] = (9, 9, 9)
    out = normalize(im, (200, 100, 0))
    assert out.getpixel((0, 0)) == (200, 100, 0)
    assert out.getpixel((1, 0)) == (1, 1, 1)


def test_normalize_keeps_rgb_as_is(rgb_gradient):
    assert normalize(rgb_gradient, WHITE) is rgb_gradient


def test_normalize_applies_exif_orientation():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 10, 10)).save(buf, "JPEG", exif=exif)
    with Image.open(buf) as im:
        assert im.size == (40, 20)
        assert normalize(im, WHITE).size == (20, 40)


@pytest.mark.parametrize(
    ("size", "max_side", "expected"),
    [
        ((100, 50), 1000, (100, 50)),
        ((2000, 1000), 1000, (1000, 500)),
        ((1000, 2000), 1000, (500, 1000)),
        ((1536, 2048), 1000, (750, 1000)),
        ((3, 2000), 1000, (2, 1000)),
        ((5000, 1), 1000, (1000, 1)),
        ((20000, 10), 0, (16384, 8)),
        ((20000, 10), 30000, (16384, 8)),
        ((10, 2_000_000), 0, (5, 1_048_576)),
        ((16384, 1_048_576), 0, (16384, 1_048_576)),
    ],
)
def test_cap_size(size, max_side, expected):
    assert _cap_size(*size, max_side) == expected


def test_largest_passing_keeps_last_pass_not_midpoint():
    # Not monotonic: 7 passes, 8 fails, 9 passes, 10+ fail.
    def passes(side):
        return side <= 7 or side == 9

    tried = []

    def attempt(side):
        tried.append(side)
        return f"img{side}" if passes(side) else None

    best = _largest_passing(1, 20, attempt)
    assert best is not None
    side = int(best.removeprefix("img"))
    assert side in tried
    assert passes(side)
    # Everything tried above the answer failed.
    assert all(s <= side or not passes(s) for s in tried)


def test_largest_passing_none():
    assert _largest_passing(5, 4, lambda side: "x") is None
    assert _largest_passing(1, 10, lambda side: None) is None


def test_fit_keeps_small_images(rgb_gradient):
    assert fit(rgb_gradient, 1000) is rgb_gradient
    assert fit(rgb_gradient, 0) is rgb_gradient


def test_fit_respects_max_side(rgb_gradient):
    out = fit(rgb_gradient, 16)
    assert out.size == (16, 12)


def test_fit_rejects_negative(rgb_gradient):
    with pytest.raises(ValueError, match="max_side"):
        fit(rgb_gradient, -1)


def test_fit_wide_image_hits_column_limit():
    im = Image.new("RGB", (16_400, 2), (5, 5, 5))
    im.paste((250, 0, 0), (0, 0, 8_200, 2))
    out = fit(im, 0)
    assert out.size == (16_384, 2)
    assert fit(im, 1000).size == (1000, 1)


def test_fit_tall_image_hits_row_limit():
    out = fit(Image.new("RGB", (1, 1_100_000), (1, 2, 3)), 0)
    assert out.size == (1, 1_048_576)


def test_fit_too_many_colors(rgb_many_colors):
    assert rgb_many_colors.getcolors(MAX_COLORS) is None
    out = fit(rgb_many_colors, 1000)
    assert out.mode == "RGB"
    assert out.getcolors(MAX_COLORS) is not None
    # 253 x 253 = 64,009 pixels, and LANCZOS makes nearly every one of them unique,
    # so no size above the always-fits 252 x 252 passes: the fallback is used.
    assert out.size == (252, 252)


def test_fit_is_exact_to_the_pixel():
    im = noise(400, 300, 70_000, seed=1)
    out = fit(im, 1000)
    side = max(out.size)
    assert out.getcolors(MAX_COLORS) is not None
    bigger = im.copy()
    bigger.thumbnail((side + 1, side + 1), Image.Resampling.LANCZOS)
    assert bigger.getcolors(MAX_COLORS) is None


def test_fit_searches_capped_image_not_original(monkeypatch):
    big = busy(4000, 3000)
    assert big.getcolors(MAX_COLORS) is None
    sizes = []
    real = Image.Image.thumbnail

    def spy(self, size, *args, **kwargs):
        sizes.append(self.size)
        return real(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "thumbnail", spy)
    out = fit(big, 1000)
    assert max(out.size) <= 1000
    assert all(max(s) <= 1000 for s in sizes)


def sixteen_bit_png(values, transparency=None) -> Image.Image:
    """A 16-bit grayscale PNG (opens as I;16), round-tripped through the codec."""
    im = Image.new("I;16", (len(values), 1))
    for x, v in enumerate(values):
        im.putpixel((x, 0), v)
    if transparency is not None:
        im.info["transparency"] = transparency
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    out = Image.open(buf)
    out.load()
    assert out.mode == "I;16"
    return out


def test_16_bit_scaling_does_not_depend_on_content():
    alone = normalize(sixteen_bit_png([255, 255]), WHITE).getpixel((0, 0))
    with_brighter = normalize(sixteen_bit_png([255, 256]), WHITE).getpixel((0, 0))
    assert alone == with_brighter == (0, 0, 0)


@pytest.mark.parametrize(
    ("values", "key", "expected"),
    [
        # The transparent level is above 255: only those pixels become background.
        ([1000, 40000], 1000, [(0, 255, 0), (156, 156, 156)]),
        # Opaque pixels that scale down to the key's 8-bit value stay opaque.
        ([200, 300], 0, [(0, 0, 0), (1, 1, 1)]),
        ([0, 300], 0, [(0, 255, 0), (1, 1, 1)]),
    ],
)
def test_16_bit_transparency_key(values, key, expected):
    out = normalize(sixteen_bit_png(values, key), (0, 255, 0))
    assert [out.getpixel((x, 0)) for x in range(len(values))] == expected


@pytest.mark.parametrize("exif", [b"Exif\x00\x00garbage-not-tiff", b"II*\x00", b"MM\x00*"])
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_unreadable_exif_is_ignored(fmt, exif):
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (9, 9, 9)).save(buf, fmt, exif=exif, dpi=(300, 300))
    buf.seek(0)
    with Image.open(buf) as im:
        assert normalize(im, WHITE).size == (40, 20)


@pytest.mark.parametrize("size", [(0, 5), (5, 0)])
def test_fit_rejects_empty_image(size):
    with pytest.raises(ValueError, match="no pixels"):
        fit(Image.new("RGB", size), 1000)
