"""Shared fixtures; the images themselves are built in images.py."""

import contextlib
import os
import sys

import pytest
from images import SAMPLES
from PIL import Image

from pixheet.gui import OFF_SCREEN, _lock_foreground

try:
    import tkinter
except ImportError:  # a Python without Tk: the GUI tests skip
    tkinter = None


def _hide_window(window) -> None:
    """Move a test window off the screen. On Windows, a root also gets no taskbar button.

    Some tests must show a window, for example to test the title bar. Without this,
    each of these windows comes up on the desktop of the person who runs the tests.
    Off the screen, a root still gets a taskbar button. The button flashes when Windows
    refuses to make the window the foreground window. The -toolwindow attribute removes
    the button, as in the hidden mode of the app. It must come before the window shows.
    A transient Toplevel has no button. The app sets the -alpha attribute itself, so this
    function does not change it.
    """
    with contextlib.suppress(tkinter.TclError):
        window.wm_geometry(OFF_SCREEN)
    if sys.platform == "win32" and isinstance(window, tkinter.Tk):
        with contextlib.suppress(tkinter.TclError):
            window.attributes("-toolwindow", True)


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
    locked = not SHOW_WINDOWS and _lock_foreground(True)
    yield locked
    if locked:
        _lock_foreground(False)


@pytest.fixture(autouse=True)
def renew_foreground_lock(foreground_lock):
    """Take the foreground lock again before each test, after a click of the user.

    While this process holds the lock, the call fails and changes nothing.
    """
    if foreground_lock:
        _lock_foreground(True)


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
def hold_open():
    """Return a function that opens a file as Excel does, on Windows only.

    Other apps can read the file, but they cannot write, rename or delete it. The
    function returns a function that closes the file. The fixture closes the files that
    are still open at the end of the test.
    """
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, generic_write, file_share_read, open_existing = 0x80000000, 0x40000000, 1, 3
    handles = []

    def close(handle):
        if handle in handles:
            handles.remove(handle)
            kernel32.CloseHandle(handle)

    def hold(path):
        handle = kernel32.CreateFileW(
            str(path), generic_read | generic_write, file_share_read, None, open_existing, 0, None
        )
        assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
        handles.append(handle)
        return lambda: close(handle)

    yield hold
    while handles:
        close(handles[-1])


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
