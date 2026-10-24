"""Shared fixtures; the images themselves are built in images.py."""

import contextlib
import os
import sys

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
SHOW_WINDOWS = os.environ.get("PIXHEET_TEST_SHOW_WINDOWS") == "1"
if tkinter is not None and not SHOW_WINDOWS:
    _hide_new_windows(tkinter.Tk)
    _hide_new_windows(tkinter.Toplevel)

LSFW_LOCK, LSFW_UNLOCK = 1, 2


def _lock_foreground(action: int) -> bool:
    """Call LockSetForegroundWindow with ``action``. Return False outside Windows."""
    if sys.platform != "win32" or SHOW_WINDOWS:
        return False
    import ctypes

    return bool(ctypes.windll.user32.LockSetForegroundWindow(action))


@pytest.fixture(scope="session", autouse=True)
def foreground_lock():
    """On Windows, stop the test windows from taking the foreground from the user.

    Each Tk window becomes the foreground window when it shows for the first time, also
    off the screen. Then the user loses the focus many times during a test run. While
    this process holds the lock, Windows refuses these changes for all processes. The
    value is True if this process holds the lock.

    The lock stops when the user presses Alt or clicks a window. Thus each test takes
    the lock again, in renew_foreground_lock. Windows also removes the lock when this
    process stops, also after a crash. PIXHEET_TEST_SHOW_WINDOWS=1 turns off the lock.
    """
    locked = _lock_foreground(LSFW_LOCK)
    yield locked
    if locked:
        _lock_foreground(LSFW_UNLOCK)


@pytest.fixture(autouse=True)
def renew_foreground_lock(foreground_lock):
    """Take the foreground lock again before each test, after a click of the user.

    While this process holds the lock, the call fails and changes nothing.
    """
    if foreground_lock:
        _lock_foreground(LSFW_LOCK)


@pytest.fixture(autouse=True)
def no_system_clipboard(monkeypatch):
    """Make a test fail when it writes the system clipboard.

    The clipboard belongs to the user. A test that copies a text must replace App._copy
    or the clipboard methods of its root.
    """
    if tkinter is None:
        return

    def fail(*args, **kwargs):
        pytest.fail("the test writes the system clipboard of the user")

    monkeypatch.setattr(tkinter.Misc, "clipboard_clear", fail)
    monkeypatch.setattr(tkinter.Misc, "clipboard_append", fail)


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
