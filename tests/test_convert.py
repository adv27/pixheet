import io
import zipfile
from pathlib import Path

import pytest
from images import SAMPLES, encoded, exif_rotated
from PIL import ExifTags, Image

import pixheet
from pixheet import MAX_COLORS, MAX_COLUMNS, ConvertResult, convert, prepare


def test_public_api():
    assert set(pixheet.__all__) >= {"convert", "ConvertResult"}


@pytest.mark.parametrize("as_path", [Path, str])
def test_convert_path_to_path(tmp_path, gradient_png, rgb_gradient, as_path):
    dst = tmp_path / "out.xlsx"
    result = convert(as_path(gradient_png), as_path(dst))
    assert isinstance(result, ConvertResult)
    assert result.original_size == result.size == (64, 48)
    assert result.cells == 64 * 48
    assert result.colors == len(rgb_gradient.getcolors(64 * 48))
    assert result.seconds > 0
    assert zipfile.ZipFile(dst).testzip() is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.png", "out.xlsx"]


def test_convert_file_objects(rgb_noise):
    dst = io.BytesIO()
    result = convert(encoded(rgb_noise, "PNG"), dst)
    assert result.colors == 300
    assert zipfile.ZipFile(dst).testzip() is None


def test_convert_image_is_left_open_and_unchanged(rgb_gradient, p_transparent):
    for im in (rgb_gradient, p_transparent):
        before = (im.mode, im.size, im.tobytes(), dict(im.info))
        convert(im, io.BytesIO(), background="black")
        assert (im.mode, im.size, im.tobytes(), dict(im.info)) == before
        im.load()  # still usable


def save_animation(path) -> None:
    frames = [Image.new("RGB", (8, 6), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(path, save_all=True, append_images=frames[1:])


def test_convert_closes_files_it_opens(tmp_path):
    # Unlike a single-frame PNG, a GIF keeps its file open after load(): only our
    # `with` closes it. A leak shows up as a ResourceWarning, which pytest turns into
    # an error here.
    src = tmp_path / "in.gif"
    save_animation(src)
    convert(src, tmp_path / "out.xlsx")
    src.unlink()  # also fails on Windows if the file is still open


def test_convert_leaves_callers_file_backed_image_open(tmp_path):
    src = tmp_path / "anim.gif"
    save_animation(src)
    with Image.open(src) as im:
        convert(im, io.BytesIO())
        im.seek(1)
        im.load()


@pytest.mark.parametrize(
    "make",
    [
        *SAMPLES.values(),
        lambda: Image.new("CMYK", (20, 10), (10, 20, 30, 40)),
        lambda: Image.new("LA", (20, 10), (10, 20)),
    ],
    ids=[*SAMPLES, "cmyk", "la"],
)
def test_style_count_matches_fitted_image(make):
    im = make()
    fitted = prepare(im)
    w, h = fitted.size
    result = convert(im, io.BytesIO())
    assert result.size == fitted.size
    assert result.colors == len(fitted.getcolors(w * h))


def test_limits(rgb_many_colors):
    result = convert(rgb_many_colors, io.BytesIO())
    assert result.colors <= MAX_COLORS
    wide = Image.new("RGB", (16_400, 3), (1, 2, 3))
    result = convert(wide, io.BytesIO(), max_side=0)
    assert result.size == (MAX_COLUMNS, 3)


@pytest.mark.parametrize(("max_side", "expected"), [(1000, (1000, 750)), (64, (64, 48)), (0, None)])
def test_max_side(max_side, expected):
    im = Image.new("RGB", (2000, 1500), (10, 20, 30))
    im.paste((200, 100, 0), (500, 500, 1500, 1000))  # few colors: only the cap applies
    result = prepare(im, max_side=max_side)
    assert result.size == (expected or im.size)


def test_prepare_matches_convert(rgb_many_colors):
    a = prepare(rgb_many_colors, max_side=200)
    result = convert(rgb_many_colors, io.BytesIO(), max_side=200)
    assert a.size == result.size == (200, 200)


def test_prepare_returns_detached_copy(gradient_png, rgb_gradient):
    out = prepare(gradient_png)
    assert out.tobytes() == rgb_gradient.tobytes()
    assert prepare(rgb_gradient) is not rgb_gradient


@pytest.mark.parametrize("background", ["nope", "#12345", (1, 2)])
def test_invalid_background(background):
    with pytest.raises(ValueError, match="invalid background"):
        convert(Image.new("RGB", (2, 2)), io.BytesIO(), background=background)


def test_invalid_max_side(tmp_path):
    with pytest.raises(ValueError, match="max_side"):
        convert(Image.new("RGB", (2, 2)), io.BytesIO(), max_side=-1)
    # convert() checks max_side before it opens the source.
    with pytest.raises(ValueError, match="max_side"):
        convert(tmp_path / "missing.png", io.BytesIO(), max_side=-1)


def test_background_option():
    im = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    assert prepare(im, background="#336699").getpixel((0, 0)) == (0x33, 0x66, 0x99)
    assert prepare(im, background=(1, 2, 3)).getpixel((0, 0)) == (1, 2, 3)
    assert prepare(im, background="none").getpixel((0, 0)) == (0, 0, 0)


def test_not_an_image(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("hello")
    with pytest.raises(Image.UnidentifiedImageError, match=r"notes\.txt'$"):
        convert(src, tmp_path / "out.xlsx")
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


def test_failed_write_keeps_existing_output(tmp_path, monkeypatch, rgb_gradient):
    dst = tmp_path / "out.xlsx"
    dst.write_bytes(b"previous")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    # Fail after the zip has been opened and the sheet written.
    monkeypatch.setattr("pixheet._xlsx.styles_xml", boom)
    with pytest.raises(OSError, match="disk full"):
        convert(rgb_gradient, dst)
    assert dst.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["out.xlsx"]


def test_overwrites_existing_output(tmp_path, rgb_gradient):
    dst = tmp_path / "out.xlsx"
    dst.write_bytes(b"previous")
    convert(rgb_gradient, dst)
    assert zipfile.ZipFile(dst).testzip() is None


def test_original_size_is_upright():
    result = convert(exif_rotated((40, 20), 8), io.BytesIO())
    assert result.original_size == result.size == (20, 40)


@pytest.mark.parametrize("mode", ["L", "RGBA", "CMYK", "I;16"])
def test_tiff_with_exif_rotation_opened_by_path(tmp_path, mode):
    # Pillow memory-maps uncompressed TIFFs opened by name and then scrambles the ones
    # with orientation 5-8; convert() must not go through that path.
    gray = Image.frombytes("L", (6, 4), bytes(range(0, 240, 10)))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    src = tmp_path / "scan.tif"
    gray.convert(mode).save(src, exif=exif)
    expected = prepare(gray.convert(mode).transpose(Image.Transpose.ROTATE_270))
    assert expected.size == (4, 6)
    for opened in (src, str(src)):
        out = prepare(opened)
        assert out.size == (4, 6)
        assert out.tobytes() == expected.tobytes()
    assert convert(src, tmp_path / "scan.xlsx").original_size == (4, 6)


def test_refuses_to_overwrite_the_input(gradient_png):
    before = gradient_png.read_bytes()
    with pytest.raises(ValueError, match="is the input image"):
        convert(gradient_png, str(gradient_png))
    assert gradient_png.read_bytes() == before


def test_write_errors_name_the_target(tmp_path, rgb_gradient):
    dst = tmp_path / "missing-folder" / "out.xlsx"
    with pytest.raises(FileNotFoundError) as exc:
        convert(rgb_gradient, dst)
    assert exc.value.filename == str(dst)
    assert ".pixheet-" not in str(exc.value)


def test_long_target_name(tmp_path, rgb_gradient):
    dst = tmp_path / ("x" * 240 + ".xlsx")
    try:
        dst.write_bytes(b"")  # the file system itself must take this name
    except OSError:
        pytest.skip("this file system does not take a 245-character name here")
    convert(rgb_gradient, dst)
    assert zipfile.ZipFile(dst).testzip() is None


@pytest.mark.parametrize("size", [(0, 5), (5, 0), (0, 0)])
def test_empty_image(size):
    with pytest.raises(ValueError, match="no pixels"):
        convert(Image.new("RGB", size), io.BytesIO())
