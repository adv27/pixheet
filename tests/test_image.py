import pytest
from images import MODE_CASES, busy, encoded, exif_rotated, many_colors, noise
from PIL import Image

from pixheet import _threads
from pixheet._image import (
    _COUNT_CAP,
    MAX_COLORS,
    _cap_size,
    _color_count,
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
    # Orientation 6: rotate 90 degrees clockwise to display.
    with Image.open(exif_rotated((40, 20), 6)) as im:
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


def search(colors, lo=252, hi=1000, first=413):
    """Run the search on a made-up color curve. Return the answer and the probed sides."""
    tried = []

    def attempt(side):
        tried.append(side)
        return side, colors(side)

    return _largest_passing(lo, hi, attempt, first), tried


# Made-up color curves: smooth like a photo, steep, flat, not monotonic, a jump, exactly
# MAX_COLORS up to a side, and a kink where the slope changes from 0.1 to 4.
CURVES = {
    "photo": lambda s: round(1.9 * s**1.6),
    "steep": lambda s: round(s**3 / 1500),
    "flat": lambda s: 60_000 + s,
    "bumpy": lambda s: round(1.9 * s**1.6) + (9_000 if s % 7 == 0 else 0),
    "step": lambda s: 10 if s < 700 else 10**6,
    "at-limit": lambda s: MAX_COLORS if s <= 600 else MAX_COLORS + 1,
    "kinked": lambda s: round(MAX_COLORS * (s / 865) ** (0.1 if s <= 865 else 4)),
}


@pytest.mark.parametrize("curve", CURVES)
def test_largest_passing_finds_an_edge(curve):
    colors = CURVES[curve]
    side, tried = search(colors)
    # The answer passed, and the side after it failed: tested, or the failing hi.
    assert side in tried
    assert colors(side) <= MAX_COLORS
    assert side + 1 == 1000 or (side + 1 in tried and colors(side + 1) > MAX_COLORS)
    # A loose bound for these curves: 3 times log2(1000 - 252) = 3 * 9.5.
    assert len(tried) <= 3 * 10


def test_largest_passing_needs_few_probes_on_a_photo_curve():
    _, tried = search(CURVES["photo"])
    assert len(tried) <= 5  # binary search needs 10 here


def test_largest_passing_accepts_exactly_max_colors():
    assert search(CURVES["at-limit"])[0] == 600


def test_largest_passing_bisects_when_the_estimates_stall():
    side, tried = search(CURVES["kinked"])
    assert side == 865
    assert len(tried) <= 20  # without the bisection safeguard: 31


def test_largest_passing_keeps_last_pass_not_midpoint():
    # Not monotonic: 7 passes, 8 fails, 9 passes, 10+ fail.
    def colors(side):
        return 1 if side <= 7 or side == 9 else 10**6

    side, tried = search(colors, lo=1, hi=21, first=10)
    assert side in tried
    assert colors(side) == 1
    # Every side tried above the answer failed.
    assert all(s <= side or colors(s) > MAX_COLORS for s in tried)


def test_largest_passing_none():
    assert search(lambda s: 1, lo=5, hi=6)[0] is None  # nothing to test
    assert search(lambda s: 10**6, lo=1, hi=11, first=5)[0] is None  # nothing passes


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


def binary_search_fit(im: Image.Image) -> tuple[int, int]:
    """The fitted size that a plain binary search of the longest side finds."""
    lo, hi = 252, max(im.size)  # lo always fits, hi does not
    best = (lo, lo)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        candidate = im.copy()
        candidate.thumbnail((mid, mid), Image.Resampling.LANCZOS)
        if candidate.getcolors(MAX_COLORS) is None:
            hi = mid
        else:
            lo, best = mid, candidate.size
    return best


@pytest.mark.parametrize(
    "make",
    [
        lambda: noise(400, 300, 70_000, seed=1),
        lambda: busy(1000, 750),
        lambda: busy(600, 900),
        lambda: noise(300, 330, 64_500, seed=2),
        lambda: noise(300, 214, 64_200, seed=3),  # fits at 299: the side before hi
    ],
    ids=["noise", "busy-wide", "busy-tall", "just-over", "fits-at-max-minus-1"],
)
def test_fit_matches_binary_search(make, monkeypatch):
    import pixheet._image as image_module

    im = make()
    counts = []
    real = image_module._color_count
    monkeypatch.setattr(image_module, "_color_count", lambda c: counts.append(c) or real(c))
    out = fit(im, 1000)
    assert len(counts) <= 8  # the search steps; a binary search needs 9 or 10 here
    side = max(out.size)
    assert out.getcolors(MAX_COLORS) is not None
    bigger = im.copy()
    bigger.thumbnail((side + 1, side + 1), Image.Resampling.LANCZOS)
    assert bigger.getcolors(MAX_COLORS) is None  # exact to the pixel
    assert out.size == binary_search_fit(im)


@pytest.mark.parametrize(("size", "first"), [((1000, 750), 413), ((600, 900), 438)])
def test_fit_first_probe_gives_about_two_times_max_colors_pixels(monkeypatch, size, first):
    import pixheet._image as image_module

    probes = []
    real = image_module._color_count
    monkeypatch.setattr(
        image_module, "_color_count", lambda c: probes.append(max(c.size)) or real(c)
    )
    fit(busy(*size), 1000)
    assert probes[0] == first


def test_color_count_is_capped():
    assert _COUNT_CAP < 260_000
    im = noise(600, 500, 260_000, seed=4)
    assert _color_count(im) == _COUNT_CAP + 1
    small = noise(50, 40, 300)
    assert _color_count(small) == 300


def test_fit_searches_capped_image_not_original(monkeypatch):
    big = busy(1600, 1200)
    assert big.getcolors(MAX_COLORS) is None
    sizes = []
    real = _threads.thumbnail

    def spy(im, side):
        sizes.append(im.size)  # the image that each search step shrinks
        return real(im, side)

    monkeypatch.setattr(_threads, "thumbnail", spy)
    out = fit(big, 400)
    assert max(out.size) <= 400
    assert sizes  # the search ran
    assert all(max(s) <= 400 for s in sizes)


def sixteen_bit(values, fmt="PNG", transparency=None) -> Image.Image:
    """A 16-bit grayscale file, decoded again. PNG opens as I;16, PGM ("PPM") as I."""
    im = Image.new("I;16", (len(values), 1))
    im.putdata(values)
    if transparency is not None:
        im.info["transparency"] = transparency
    out = Image.open(encoded(im, fmt))
    out.load()
    return out


@pytest.mark.parametrize(("fmt", "mode"), [("PNG", "I;16"), ("PPM", "I")])
def test_16_bit_scaling_does_not_depend_on_content(fmt, mode):
    alone = sixteen_bit([255, 255], fmt)
    assert alone.mode == mode
    with_brighter = sixteen_bit([255, 256], fmt)
    assert normalize(alone, WHITE).getpixel((0, 0)) == (0, 0, 0)
    assert normalize(with_brighter, WHITE).getpixel((0, 0)) == (0, 0, 0)


def test_32_bit_file_with_8_bit_values_keeps_them():
    # Pillow writes mode I as 32-bit samples in a TIFF; values up to 255 stay as they are.
    im = Image.open(encoded(Image.new("I", (3, 2), 100), "TIFF"))
    im.load()
    assert (im.mode, im.format) == ("I", "TIFF")
    assert normalize(im, WHITE).getpixel((0, 0)) == (100, 100, 100)


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
    out = normalize(sixteen_bit(values, transparency=key), (0, 255, 0))
    assert [out.getpixel((x, 0)) for x in range(len(values))] == expected


@pytest.mark.parametrize("exif", [b"Exif\x00\x00garbage-not-tiff", b"II*\x00", b"MM\x00*"])
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_unreadable_exif_is_ignored(fmt, exif):
    buf = encoded(Image.new("RGB", (40, 20), (9, 9, 9)), fmt, exif=exif, dpi=(300, 300))
    with Image.open(buf) as im:
        assert normalize(im, WHITE).size == (40, 20)


@pytest.mark.parametrize("size", [(0, 5), (5, 0)])
def test_fit_rejects_empty_image(size):
    with pytest.raises(ValueError, match="no pixels"):
        fit(Image.new("RGB", size), 1000)


def test_many_colors_builder():
    im = many_colors()
    assert len(im.getcolors(1 << 16)) == 1 << 16
    assert im.getpixel((10, 200)) == (10, 200, 0)
