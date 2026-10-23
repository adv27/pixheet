"""Private. The services of the desktop system, without Tk.

The desktop app uses these functions to find out if the drag and drop extension tkdnd
can load into the process.

Each function has a ``platform`` argument, the same values as ``sys.platform``. The tests
use it to test the code of each system on one system. The functions do not use Tk.
"""

import os
import sys
from collections.abc import Callable, Mapping

DND_ENV = "PIXHEET_DND"
"""The environment variable that turns off drag and drop when its value is 0."""


def _is_linux(platform: str) -> bool:
    return platform.startswith("linux")


# --- Drag and drop -------------------------------------------------------------------


def x11_supports_tkdnd(load: Callable[[str], object] | None = None) -> bool:
    """Return True when the shared libX11 of the system is version 1.7 or later.

    The Tk of the uv Python (python-build-standalone) has its own static copy of Xlib.
    tkdnd uses the shared libX11. With a libX11 older than 1.7, the load of tkdnd stops
    the process with SIGABRT, and Python cannot catch that. Only libX11 1.7 and later
    have the function XSetIOErrorExitHandler. ``load`` opens a shared library. The
    tests replace it. The function does not raise.
    """
    try:
        if load is None:
            import ctypes

            load = ctypes.CDLL
        library = load("libX11.so.6")
    except OSError:
        return False
    return hasattr(library, "XSetIOErrorExitHandler")


def dnd_allowed(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    load: Callable[[str], object] | None = None,
) -> bool:
    """Return True when the app can try to load tkdnd.

    The value 0 in the environment variable ``PIXHEET_DND`` turns off drag and drop on
    all systems. On Linux, tkdnd also needs libX11 1.7 or later, see
    :func:`x11_supports_tkdnd`.
    """
    environ = os.environ if environ is None else environ
    if environ.get(DND_ENV, "").strip() == "0":
        return False
    if _is_linux(platform or sys.platform):
        return x11_supports_tkdnd(load)
    return True
