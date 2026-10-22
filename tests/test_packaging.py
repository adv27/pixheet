"""Tests of the scripts in packaging/: the icon and the version resource of the exe."""

import importlib.util
import tomllib
from pathlib import Path

import pytest
from PIL import Image

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def load(name: str):
    """Import a module from packaging/, which is not a package."""
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icon = load("make_icon")
version_info = load("version_info")


def frames(path: Path) -> dict[int, bytes]:
    """Return the RGBA pixels of each size in an .ico file."""
    with Image.open(path) as icon:
        result = {}
        for width, height in sorted(icon.info["sizes"]):
            icon.size = (width, height)
            icon.load()
            result[width] = icon.convert("RGBA").tobytes()
        return result


def test_committed_icon_is_the_output_of_make_icon(tmp_path):
    fresh = make_icon.write(tmp_path / "pixheet.ico")
    assert frames(make_icon.ICON) == frames(fresh)
    assert sorted(frames(fresh)) == list(make_icon.SIZES)


@pytest.mark.parametrize("size", make_icon.SIZES)
def test_icon_cells_have_the_picture_colors(size):
    image = make_icon.draw(size)
    cell = size // make_icon.GRID
    for row, letters in enumerate(make_icon.PICTURE):
        for column, letter in enumerate(letters):
            center = (column * cell + cell // 2, row * cell + cell // 2)
            assert image.getpixel(center) == (*make_icon.PALETTE[letter], 255)


def test_icon_has_grid_lines_only_from_48_pixels():
    line = (*make_icon.LINE, 255)
    small = make_icon.draw(32)
    assert line not in {color for _count, color in small.getcolors(32 * 32)}
    assert make_icon.draw(48).getpixel((6, 20)) == line


def test_draw_refuses_a_size_that_does_not_fit_the_grid():
    with pytest.raises(ValueError, match="multiple of 8"):
        make_icon.draw(20)


@pytest.mark.parametrize(
    ("version", "numbers"),
    [
        ("0.1.0", (0, 1, 0, 0)),
        ("1", (1, 0, 0, 0)),
        ("2.3.4.5.6", (2, 3, 4, 5)),
        ("1.2.0rc1", (1, 2, 0, 0)),
        ("0.2.0.dev3", (0, 2, 0, 0)),
        ("1.0+local.7", (1, 0, 0, 0)),
        ("v1.4", (1, 4, 0, 0)),
    ],
)
def test_numeric_version(version, numbers):
    assert version_info.numeric_version(version) == numbers


@pytest.mark.parametrize("version", ["", "dev", "1.70000"])
def test_numeric_version_refuses_bad_text(version):
    with pytest.raises(ValueError, match="version"):
        version_info.numeric_version(version)


def test_metadata_comes_from_pyproject():
    with (PACKAGING.parent / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    metadata = version_info.read_metadata()
    strings = metadata.strings("Pixheet.exe")
    assert metadata.version == project["version"]
    assert strings["ProductName"] == "Pixheet"
    assert strings["FileVersion"] == strings["ProductVersion"] == project["version"]
    assert strings["CompanyName"] == "Dinh-Anh Vu"
    assert strings["LegalCopyright"].startswith("Copyright (c) ")
    assert strings["LegalCopyright"].endswith("Dinh-Anh Vu")
    assert "License: MIT." in strings["Comments"]
    assert strings["OriginalFilename"] == "Pixheet.exe"


def test_spec_is_not_ignored_by_git():
    gitignore = (PACKAGING.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "!packaging/*.spec" in gitignore
    assert gitignore.index("!packaging/*.spec") > gitignore.index("*.spec")
