"""Private. The services of the desktop system, without Tk.

The desktop app uses these functions to:

- open a file or a folder with the default app of the system,
- show a file in its folder, with the file selected when the system can do that,
- read the light or dark setting of the system,
- find the path of the settings file,
- find out if the drag and drop extension tkdnd can load into the process.

The functions with code for more than one system have a ``platform`` argument, the same
values as ``sys.platform``. The tests use it to test the code of each system on one
system. Some functions take ``environ``, ``load`` or ``output``, so that the tests can
replace the system. The functions do not use Tk.
"""

import contextlib
import errno
import ntpath
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

TITLE = "Pixheet"
SETTINGS_NAME = "settings.ini"
DND_ENV = "PIXHEET_DND"
"""The environment variable that turns off drag and drop when its value is 0."""
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
COMMAND_TIMEOUT = 2.0
"""The maximum time of a command that reads a setting of the desktop, in seconds."""
SHOW_TIMEOUT = 15.0
"""The maximum time of the D-Bus call that shows a file in the file manager, in seconds.

The call can start the file manager first. Thus it can take some seconds.
"""

_URI_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")
_UINT32 = re.compile(r"\buint32\s+(\d+)")

PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_READ = "org.freedesktop.portal.Settings.Read"
FILE_MANAGER = "org.freedesktop.FileManager1"
FILE_MANAGER_PATH = "/org/freedesktop/FileManager1"


def _is_linux(platform: str) -> bool:
    return platform.startswith("linux")


# --- The environment of a child process ----------------------------------------------


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment for a program of the system, for example xdg-open.

    The one-file app on Linux puts its own folder first in ``LD_LIBRARY_PATH``. A program
    of the system must not load the libraries of the app, because they can be older than
    the libraries of the system. PyInstaller keeps the value from before the start in
    ``LD_LIBRARY_PATH_ORIG``. The function puts that value back, or removes the variable.
    """
    env = dict(os.environ if environ is None else environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _spawn(command: Sequence[str] | str) -> None:
    """Start ``command`` and do not wait for it. Raise OSError when it cannot start.

    Some openers, for example the generic path of xdg-open, run until the user closes the
    file manager.
    """
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=child_env(),
    )
    # Collect the exit status when the opener ends. This prevents a zombie process.
    threading.Thread(target=process.wait, name="pixheet-opener", daemon=True).start()


def _output(command: Sequence[str], timeout: float = COMMAND_TIMEOUT) -> str | None:
    """Run ``command`` and return its output. Return None when the command fails."""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
            env=child_env(),
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout


def has_session_bus(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when the user session has a D-Bus bus.

    Without a bus, gdbus can try to start a new bus. Thus the app does not call it then.
    """
    environ = os.environ if environ is None else environ
    if environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and Path(runtime, "bus").exists()


# --- Open and show files -------------------------------------------------------------


def file_uri(path: str | os.PathLike[str]) -> str:
    """Return the file URI of the absolute POSIX path ``path``, for example file:///a%20b.

    The function encodes each byte that is not a letter, a digit or one of ``-._~/``. Thus
    the URI has no quote and no comma, and it can go into a D-Bus argument as it is.
    pathlib.Path.as_uri() needs urllib, which the built app does not include.
    """
    raw = os.fsencode(path)
    return "file://" + "".join(chr(b) if b in _URI_SAFE else f"%{b:02X}" for b in raw)


def open_path(path: Path, platform: str | None = None) -> None:
    """Open the file or the folder ``path`` with the default app of the system.

    The function does not wait for the app. Raise OSError when the system has no opener,
    or, on Windows, when no app opens this type of file.
    """
    platform = platform or sys.platform
    if platform == "win32":
        os.startfile(path)
        return
    _spawn(["open" if platform == "darwin" else "xdg-open", os.fspath(path)])


DEFAULT_WINDOWS_FOLDER = r"C:\Windows"


def _system_windows_folder() -> str | None:
    """Return the Windows folder from GetSystemWindowsDirectoryW, or None off Windows.

    On a terminal server, GetWindowsDirectoryW can give a folder of the user. Explorer is
    in the shared Windows folder, which GetSystemWindowsDirectoryW gives.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
    except ImportError, AttributeError, OSError:
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else None


def windows_folder(environ: Mapping[str, str] | None = None) -> str:
    r"""Return the full path of the Windows folder, for example ``C:\Windows``.

    The function asks Windows first. Then it reads ``SystemRoot`` and ``WINDIR``. It uses
    only an absolute path, because a relative path can point to the current folder.
    """
    folder = _system_windows_folder()
    if folder and ntpath.isabs(folder):
        return folder
    environ = os.environ if environ is None else environ
    for name in ("SystemRoot", "WINDIR"):
        value = environ.get(name, "")
        if ntpath.isabs(value):
            return value
    return DEFAULT_WINDOWS_FOLDER


def show_items_commands(uri: str) -> list[list[str]]:
    """Return the commands that ask the file manager to show and select the file ``uri``.

    The commands call the method ShowItems of the D-Bus interface FileManager1. GNOME
    Files, Dolphin, Nemo, Caja and Thunar have it. The first command uses gdbus, the
    second uses dbus-send.
    """
    method = f"{FILE_MANAGER}.ShowItems"
    return [
        [
            "gdbus", "call", "--session", "--timeout", f"{SHOW_TIMEOUT:.0f}",
            "--dest", FILE_MANAGER, "--object-path", FILE_MANAGER_PATH,
            "--method", method, f"['{uri}']", "",
        ],
        [
            "dbus-send", "--session", "--print-reply",
            f"--reply-timeout={SHOW_TIMEOUT * 1000:.0f}", f"--dest={FILE_MANAGER}",
            FILE_MANAGER_PATH, method, f"array:string:{uri}", "string:",
        ],
    ]  # fmt: skip


def show_on_linux(path: str, environ: Mapping[str, str] | None = None) -> str:
    """Show the file ``path`` in the file manager. Return the name of the program that did it.

    First, the function asks the file manager through D-Bus to select the file. When that
    fails, it opens the folder with xdg-open. The function waits for the D-Bus call, so
    call it on a worker thread. It does not raise. The result is "" when no program worked.
    """
    if has_session_bus(environ):
        for command in show_items_commands(file_uri(path)):
            if _output(command, SHOW_TIMEOUT) is not None:
                return command[0]
    try:
        _spawn(["xdg-open", str(PurePosixPath(path).parent)])
    except OSError:
        return ""
    return "xdg-open"


def show_in_folder(path: Path, platform: str | None = None) -> None:
    """Show the folder that holds ``path`` in the file manager, with ``path`` selected.

    - On Windows, File Explorer opens the folder and selects the file.
    - On macOS, Finder does the same.
    - On Linux, the file manager selects the file when it has the D-Bus interface
      FileManager1. Else xdg-open opens the folder. A worker thread does this, because
      the D-Bus call can wait for the file manager to start.

    The function does not wait for the file manager. Raise OSError when the system has no
    program that can show the folder.
    """
    platform = platform or sys.platform
    full = os.path.abspath(path)  # noqa: PTH100 - resolve() follows links
    if platform == "win32":
        # Give the full path. With the bare name, Windows looks for explorer.exe in the
        # folder of the app and in the current folder first. Explorer reads its own
        # command line. It needs the comma and the quotes so.
        explorer = ntpath.join(windows_folder(), "explorer.exe")
        _spawn(f'"{explorer}" /select,"{full}"')
    elif platform == "darwin":
        _spawn(["open", "-R", full])
    elif not any(shutil.which(name) for name in ("gdbus", "dbus-send", "xdg-open")):
        raise FileNotFoundError(errno.ENOENT, "No program can show the folder", "xdg-open")
    else:
        threading.Thread(
            target=show_on_linux, args=(full,), name="pixheet-show", daemon=True
        ).start()


# --- The light or dark setting -------------------------------------------------------


def portal_commands() -> list[list[str]]:
    """Return the commands that read the color scheme from the desktop portal.

    The answer is 1 for dark, 2 for light, and 0 when the user has no preference.
    """
    return [
        [
            "gdbus", "call", "--session", "--timeout", f"{COMMAND_TIMEOUT:.0f}",
            "--dest", PORTAL_DEST, "--object-path", PORTAL_PATH, "--method", PORTAL_READ,
            "org.freedesktop.appearance", "color-scheme",
        ],
        [
            "dbus-send", "--session", "--print-reply=literal",
            f"--reply-timeout={COMMAND_TIMEOUT * 1000:.0f}", f"--dest={PORTAL_DEST}",
            PORTAL_PATH, PORTAL_READ, "string:org.freedesktop.appearance", "string:color-scheme",
        ],
    ]  # fmt: skip


GSETTINGS_SCHEME = ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]
GSETTINGS_THEME = ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]


def linux_theme(
    environ: Mapping[str, str] | None = None,
    output: Callable[[Sequence[str]], str | None] | None = None,
) -> str | None:
    """Return "dark" or "light" from the setting of the Linux desktop.

    The function asks these sources, in this order:

    1. The color scheme of the desktop portal, through gdbus or dbus-send. GNOME, KDE
       and other desktops give it.
    2. The GNOME color scheme, through gsettings.
    3. The name of the GTK theme, through gsettings. A name with "dark" is dark.

    Return None when no source answers. ``output`` runs a command and returns its output,
    or None. The tests replace it. The function does not raise.
    """
    output = output or _output
    answered = False
    if has_session_bus(environ):
        for command in portal_commands():
            text = output(command)
            if text is None:
                continue
            answered = True
            match = _UINT32.search(text)
            value = match[1] if match else "0"
            if value == "1":
                return "dark"
            if value == "2":
                return "light"
            break  # no preference: ask GNOME
    text = output(GSETTINGS_SCHEME)
    if text is not None:
        answered = True
        value = text.strip().strip("'")
        if value == "prefer-dark":
            return "dark"
        if value == "prefer-light":
            return "light"
    text = output(GSETTINGS_THEME)
    if text is not None:
        return "dark" if "dark" in text.lower() else "light"
    return "light" if answered else None


def system_theme(platform: str | None = None) -> str:
    """Return "dark" when the system shows apps in dark mode. Else return "light".

    On Windows, the function reads the setting from the registry. On Linux, it asks the
    desktop, see :func:`linux_theme`. On other systems, and when the setting is not
    available, the result is "light". The function does not raise.
    """
    platform = platform or sys.platform
    if _is_linux(platform):
        return linux_theme() or "light"
    if platform != "win32":
        return "light"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except ImportError, OSError:
        return "light"
    return "dark" if value == 0 else "light"


class LatestValue[T]:
    """Call ``read`` on a worker thread, and give the last value at once.

    The first call reads the value on the calling thread and returns it. Each later call
    returns the last value and starts a new read on a worker thread, when no read runs.
    Thus a slow ``read``, for example a command, does not block the window.
    """

    def __init__(self, read: Callable[[], T], name: str = "pixheet-read") -> None:
        self.read = read
        self.name = name
        self._value: T | None = None
        self._has_value = False
        self._thread: threading.Thread | None = None

    def __call__(self) -> T:
        if not self._has_value:
            self._value = self.read()
            self._has_value = True
        elif self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._update, name=self.name, daemon=True)
            self._thread.start()
        return self._value  # type: ignore[return-value]

    def _update(self) -> None:
        with contextlib.suppress(Exception):  # keep the last value
            self._value = self.read()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the read on the worker thread. The tests use it."""
        if self._thread is not None:
            self._thread.join(timeout)


# --- The settings file ---------------------------------------------------------------


def settings_path(
    environ: Mapping[str, str] | None = None, platform: str | None = None
) -> Path | None:
    r"""Return the path of the settings file. Return None when the system gives no folder.

    On Windows, the path is ``%APPDATA%\Pixheet\settings.ini``. On other systems, it is
    ``pixheet/settings.ini`` in ``$XDG_CONFIG_HOME``, or in ``~/.config``. The function
    does not make the file or the folder.
    """
    environ = os.environ if environ is None else environ
    if (platform or sys.platform) == "win32":
        base = Path(environ.get("APPDATA", ""))
        return base / TITLE / SETTINGS_NAME if base.is_absolute() else None
    base = Path(environ.get("XDG_CONFIG_HOME", ""))
    if not base.is_absolute():
        base = Path(environ.get("HOME", ""), ".config")
        if not base.is_absolute():
            return None
    return base / "pixheet" / SETTINGS_NAME


# --- Drag and drop -------------------------------------------------------------------


def x11_supports_tkdnd(load: Callable[[str], object] | None = None) -> bool:
    """Return True when the shared libX11 and libxcb of the system can load tkdnd.

    The Tk of the uv Python (python-build-standalone) has its own static copy of Xlib.
    tkdnd uses the shared libX11, and libX11 uses the shared libxcb. With a library that
    is too old, the load of tkdnd stops the process with SIGABRT, and Python cannot
    catch that. The function makes sure of the two versions:

    - libX11 1.7 or later. Only these versions have the function XSetIOErrorExitHandler.
    - libxcb 1.14 or later. Only these versions have the function xcb_total_read. The
      built app has its own libX11 1.7, but it uses the libxcb of the system. Ubuntu
      18.04, Debian 10 and RHEL 8 have libxcb 1.13.

    ``load`` opens a shared library. The tests replace it. The function does not raise.
    """
    try:
        if load is None:
            import ctypes

            load = ctypes.CDLL
        x11 = load("libX11.so.6")
        if not hasattr(x11, "XSetIOErrorExitHandler"):
            return False
        xcb = load("libxcb.so.1")
    except OSError:
        return False
    return hasattr(xcb, "xcb_total_read")


def dnd_allowed(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    load: Callable[[str], object] | None = None,
) -> bool:
    """Return True when the app can try to load tkdnd.

    The value 0 in the environment variable ``PIXHEET_DND`` turns off drag and drop on
    all systems. On Linux, tkdnd also needs libX11 1.7 or later and libxcb 1.14 or
    later, see :func:`x11_supports_tkdnd`.
    """
    environ = os.environ if environ is None else environ
    if environ.get(DND_ENV, "").strip() == "0":
        return False
    if _is_linux(platform or sys.platform):
        return x11_supports_tkdnd(load)
    return True
