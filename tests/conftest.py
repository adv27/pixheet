"""Shared fixtures; the images themselves are built in images.py."""

import contextlib
import os

import pytest
from images import SAMPLES
from PIL import Image

try:
    import tkinter
except ImportError:  # a Python without Tk: the GUI tests skip
    tkinter = None


def _hide_window(window) -> None:
    """Move a test window off the screen.

    Some tests must show a window, for example to test the title bar. Without this,
    each of these windows comes up on the desktop of the person who runs the tests.
    The app sets the -alpha attribute itself, so this function does not change it.
    """
    with contextlib.suppress(tkinter.TclError):
        window.wm_geometry("+-32000+-32000")


def _hide_new_windows(cls) -> None:
    init = cls.__init__

    def hidden_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        _hide_window(self)

    cls.__init__ = hidden_init


# Set PIXHEET_TEST_SHOW_WINDOWS=1 to see the test windows, for example to debug a test.
if tkinter is not None and os.environ.get("PIXHEET_TEST_SHOW_WINDOWS") != "1":
    _hide_new_windows(tkinter.Tk)
    _hide_new_windows(tkinter.Toplevel)


@pytest.fixture
def rgb_gradient() -> Image.Image:
    return SAMPLES["gradient"]()


@pytest.fixture
def rgb_noise() -> Image.Image:
    return SAMPLES["noise"]()


@pytest.fixture
def rgb_one_pixel() -> Image.Image:
    return SAMPLES["one-pixel"]()


@pytest.fixture
def rgb_many_colors() -> Image.Image:
    return SAMPLES["many-colors"]()


@pytest.fixture
def p_transparent() -> Image.Image:
    return SAMPLES["palette-transparent"]()


@pytest.fixture
def gradient_png(tmp_path, rgb_gradient):
    """The gradient image saved as tmp_path/in.png."""
    path = tmp_path / "in.png"
    rgb_gradient.save(path)
    return path
