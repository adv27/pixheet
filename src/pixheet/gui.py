"""Desktop app: drop images on the window to convert each one to an .xlsx file.

Start it with ``pixheet-gui`` or ``python -m pixheet.gui``. The app converts the image
paths that you give as arguments when it starts. The option ``--smoke`` opens the window
and closes it when the app has nothing to do. Use it to test a built app.

Each output file goes next to its image, with the suffix ``.xlsx``. A worker thread does
the conversion, so the window stays responsive. The worker sends messages to a queue,
and the window reads the queue each 50 ms.

With sv-ttk, the window has the Sun Valley theme, the look of Windows 11. A toggle at the
top right sets the theme to System, Light or Dark. With System, the theme is the same as
the system setting, and it changes when the setting changes. The app saves the choice in
a settings file.
"""

import configparser
import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # a Python build without Tk
    tk = None

from PIL import UnidentifiedImageError

from pixheet._image import RGB, check_max_side, parse_background
from pixheet.core import DEFAULT_BACKGROUND, DEFAULT_MAX_SIDE, ConvertResult, convert

TITLE = "Pixheet"
MIN_WIDTH, MIN_HEIGHT = 460, 400
"""The smallest size of the window. The window can need more, for example with large text."""
POLL_MS = 50
"""Time between two reads of the message queue, in milliseconds."""
PROGRESS_INTERVAL = 0.05
"""Minimum time between two progress messages from the worker, in seconds."""
MAX_LISTED = 10
"""Maximum number of file names in one dialog."""

DROP_TEXT = "Drop images here"
NO_DND_TEXT = "Drag and drop is not available. Use Browse."
BUSY_DROP_TEXT = "The app did not accept the files that you dropped during a conversion."
IMAGE_TYPES = [
    (
        "Images",
        "*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.webp *.avif *.ico "
        "*.ppm *.pgm *.pbm *.pnm *.tga *.psd",
    ),
    ("All files", "*.*"),
]
BACKGROUNDS = ("white", "black", "none")


# --- Helpers that do not need Tk -----------------------------------------------------


def default_output(src: Path) -> Path:
    """Return the output path of ``src``: the same folder and name, with an .xlsx suffix."""
    return src.with_suffix(".xlsx")


def _key(path: Path) -> str:
    """Return a key that is the same for two spellings of one path."""
    return os.path.normcase(path.absolute())


def parse_drop(data: str, splitlist: Callable[[str], Sequence[str]]) -> list[Path]:
    """Return the files in the data of a file drop, in order and without duplicates.

    ``splitlist`` is ``root.tk.splitlist``. It removes the braces that Tk puts around a
    path with spaces. The result does not include folders and paths that do not exist.
    """
    files: list[Path] = []
    seen: set[str] = set()
    for item in splitlist(data):
        path = Path(item)
        key = _key(path)
        if key not in seen and path.is_file():
            seen.add(key)
            files.append(path)
    return files


class Throttle:
    """Let a progress update through at most once in each ``interval`` seconds.

    The first update and the last update, where ``done`` is ``total``, always go through.
    """

    def __init__(
        self, interval: float = PROGRESS_INTERVAL, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.interval = interval
        self.clock = clock
        self._last: float | None = None

    def __call__(self, done: int, total: int) -> bool:
        now = self.clock()
        if done >= total or self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False


@dataclass(frozen=True, slots=True)
class Options:
    """The conversion options from the window."""

    max_side: int = DEFAULT_MAX_SIDE
    background: RGB | None = (255, 255, 255)
    compact: bool = False


def parse_options(max_side: str, background: str, compact: bool) -> Options:
    """Make :class:`Options` from the text in the window. Raise ValueError for bad text."""
    try:
        side = int(max_side.strip())
        check_max_side(side)
    except ValueError:
        raise ValueError(
            f"The maximum side must be a whole number, 0 or more. The value is {max_side!r}."
        ) from None
    try:
        color = parse_background(background)
    except ValueError:
        raise ValueError(
            f"The background must be white, black, none, or #RRGGBB. The value is {background!r}."
        ) from None
    return Options(max_side=side, background=color, compact=compact)


@dataclass(frozen=True, slots=True)
class Job:
    """One image to convert, and the file to write."""

    src: Path
    dst: Path


@dataclass(slots=True)
class Plan:
    """The jobs of a batch, before the user decides about files that exist."""

    jobs: list[Job]
    existing: list[Job]
    """The jobs whose output file exists. Ask the user before the app replaces them."""
    errors: list[str]
    """One "name: reason" line for each input that the app cannot convert."""


def plan_jobs(sources: Iterable[Path]) -> Plan:
    """Find the output of each source, and find the sources that the app cannot convert."""
    jobs: list[Job] = []
    errors: list[str] = []
    outputs: dict[str, Path] = {}  # output key -> the source that writes it
    for src in sources:
        dst = default_output(src)
        key = _key(dst)
        if key in outputs:
            errors.append(f"{src.name}: {dst.name} is also the output of {outputs[key].name}")
            continue
        if _same_file(src, dst):
            errors.append(f"{src.name}: the output file is the input file")
            continue
        outputs[key] = src
        jobs.append(Job(src, dst))
    existing = [job for job in jobs if job.dst.exists()]
    return Plan(jobs=jobs, existing=existing, errors=errors)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


# --- The worker ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Started:
    index: int
    """The number of this file in the batch, from 1."""
    count: int
    src: Path


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class Converted:
    job: Job
    result: ConvertResult


@dataclass(frozen=True, slots=True)
class Failed:
    src: Path
    error: str


@dataclass(frozen=True, slots=True)
class Finished:
    converted: int
    cancelled: bool


type Message = Started | Progress | Converted | Failed | Finished


class _Cancelled(Exception):
    """The user stopped the batch. The progress function raises it to stop convert()."""


def _progress_hook(
    cancel: threading.Event, post: Callable[[Message], None], throttle: Throttle
) -> Callable[[int, int], None]:
    def progress(done: int, total: int) -> None:
        if cancel.is_set():
            raise _Cancelled
        if throttle(done, total):
            post(Progress(done, total))

    return progress


NOT_AN_IMAGE = "the app cannot read this file as an image"
"""The error text for a file that Pillow cannot open.

The text of Pillow has the full path in quotes, with two backslashes for each one. The
dialog already shows the file name.
"""


def _describe(error: Exception) -> str:
    if isinstance(error, UnidentifiedImageError):
        return NOT_AN_IMAGE
    return str(error) or type(error).__name__


def run_batch(
    jobs: Sequence[Job],
    options: Options,
    post: Callable[[Message], None],
    cancel: threading.Event,
    interval: float = PROGRESS_INTERVAL,
) -> None:
    """Convert the jobs one after the other, and post a message for each step.

    The worker thread runs this function. An error in one job does not stop the batch.
    When ``cancel`` is set, the batch stops. The last message is always
    :class:`Finished`.
    """
    converted = 0
    cancelled = False
    try:
        for index, job in enumerate(jobs, 1):
            if cancel.is_set():
                cancelled = True
                break
            post(Started(index, len(jobs), job.src))
            progress = _progress_hook(cancel, post, Throttle(interval))
            try:
                result = convert(
                    job.src,
                    job.dst,
                    max_side=options.max_side,
                    background=options.background,
                    compact=options.compact,
                    progress=progress,
                )
            except _Cancelled:
                cancelled = True
                break
            except Exception as e:
                post(Failed(job.src, _describe(e)))
                continue
            converted += 1
            post(Converted(job, result))
    finally:
        post(Finished(converted, cancelled))


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def summary(dst: Path, result: ConvertResult) -> str:
    """Return the status line for one converted file."""
    w, h = result.size
    return f"{dst}  {w}x{h}  {result.colors:,} colors  {result.seconds:.2f} s"


def open_folder(path: Path, platform: str | None = None) -> None:
    """Show the folder that holds ``path`` in the file manager of the system.

    The function does not wait for the file manager. Some openers, for example the
    generic path of xdg-open, run until the user closes the file manager. Raise OSError
    when the system has no opener.
    """
    folder = path.parent
    platform = platform or sys.platform
    if platform == "win32":
        os.startfile(folder)
        return
    opener = "open" if platform == "darwin" else "xdg-open"
    process = subprocess.Popen(
        [opener, os.fspath(folder)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Collect the exit status when the opener ends. This prevents a zombie process.
    threading.Thread(target=process.wait, name="pixheet-opener", daemon=True).start()


# --- The window ----------------------------------------------------------------------


def _load_tkdnd() -> tuple[Any, str]:
    """Import tkinterdnd2. Return its TkinterDnD module and the type name for files."""
    # A plain import, not "lazy import", so that PyInstaller finds the package.
    from tkinterdnd2 import DND_FILES, TkinterDnD

    return TkinterDnD, DND_FILES


ICON_NAME = "pixheet.ico"


def icon_path() -> Path | None:
    """Return the path of the window icon, or None when the app has no icon file.

    The built app has the icon in its own folder. From a checkout, the icon is in
    ``packaging/``. An installed package does not include the icon.
    """
    if getattr(sys, "frozen", False):
        folder = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        folder = Path(__file__).resolve().parents[2] / "packaging"
    path = folder / ICON_NAME
    return path if path.is_file() else None


def set_icon(root: tk.Tk, platform: str | None = None) -> bool:
    """Show the Pixheet icon on ``root`` and on all windows that come later.

    Return True when the window has the icon. Only Tk on Windows can read an .ico file.
    On other systems, and when the icon file is not found, the window keeps the Tk icon.
    """
    path = icon_path()
    if path is None or (platform or sys.platform) != "win32":
        return False
    try:
        root.iconbitmap(default=os.fspath(path))
    except tk.TclError:
        return False
    return True


def enable_dnd(root: tk.Tk) -> str | None:
    """Load the tkdnd extension into ``root``. Return the drop type for files.

    Return None when tkinterdnd2 is not installed or tkdnd does not load.
    """
    try:
        tkdnd, files = _load_tkdnd()
        tkdnd.require(root)
    except ImportError, RuntimeError, tk.TclError:
        return None
    return files


# --- The theme -----------------------------------------------------------------------

THEME_ENV = "PIXHEET_THEME"
"""The environment variable that sets the theme at the start: light, dark, or system."""
THEME_CHOICES = ("system", "light", "dark")
"""The choices of the theme toggle. "system" follows the light or dark setting of the system."""
THEME_POLL_MS = 2000
"""Time between two reads of the system light or dark setting, in milliseconds."""
SUN_VALLEY = ("sun-valley-light", "sun-valley-dark")
"""The names of the ttk themes of sv-ttk."""
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
LIGHT_DISABLED_ACCENT_FG = "#575757"
"""The text color of a disabled accent button in the light theme.

sv-ttk draws this text white on light gray, with a contrast of 1.7:1. This color gives
4.1:1, about the same as the dark theme.
"""
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
SWP_REDRAW_FRAME = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
"""SetWindowPos flags: no size, no move, no z-order, no activate, frame changed."""
SHOW_DELAY_MS = 30
"""Time from the first map of the window until the window becomes visible, in milliseconds.

In this time, Windows draws the title bar in the theme mode, and Tk draws the widgets.
"""


def system_theme(platform: str | None = None) -> str:
    """Return "dark" when Windows shows apps in dark mode. Else return "light".

    The function reads the Windows setting from the registry. On other systems, and when
    the setting is not available, the result is "light". The function does not raise.
    """
    if (platform or sys.platform) != "win32":
        return "light"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except ImportError, OSError:
        return "light"
    return "dark" if value == 0 else "light"


def theme_override(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the theme choice that the environment variable ``PIXHEET_THEME`` sets.

    The result is "system", "light" or "dark". Return None for no value and for an
    unknown value. Then the app uses the saved choice.
    """
    value = (os.environ if environ is None else environ).get(THEME_ENV, "")
    value = value.strip().lower()
    return value if value in THEME_CHOICES else None


SETTINGS_NAME = "settings.ini"
SETTINGS_SECTION = "appearance"


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


def _read_settings(path: Path) -> configparser.ConfigParser:
    """Read the settings file. Raise configparser.Error or ValueError for a damaged file.

    A file that does not exist, or that the app cannot open, gives no settings.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")  # read() ignores a file that it cannot open
    return parser


def read_theme_choice(path: Path | None) -> str:
    """Return the saved theme choice: "system", "light" or "dark".

    No path, a missing file, a damaged file and an unknown value give "system". The
    function does not raise.
    """
    if path is None:
        return "system"
    try:
        parser = _read_settings(path)
    except configparser.Error, ValueError:  # ValueError includes UnicodeDecodeError
        return "system"
    value = parser.get(SETTINGS_SECTION, "theme", fallback="").strip().lower()
    return value if value in THEME_CHOICES else "system"


def write_theme_choice(path: Path, choice: str) -> bool:
    """Save the theme choice in the settings file. Return True when the file has it.

    The function writes a temporary file, then puts it in the place of the old file. Thus
    the file is never half written. The function keeps the other settings, but it replaces
    a damaged file. For an error of the file system, it writes the cause to stderr and
    does not raise.
    """
    try:
        parser = _read_settings(path)
    except configparser.Error, ValueError:
        parser = configparser.ConfigParser(interpolation=None)
    if not parser.has_section(SETTINGS_SECTION):
        parser.add_section(SETTINGS_SECTION)
    parser.set(SETTINGS_SECTION, "theme", choice)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8") as file:
            parser.write(file)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    except OSError as e:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        _error(f"cannot save the settings in {path}: {_describe(e)}")
        return False
    return True


def _load_sv_ttk() -> Any:
    """Import sv_ttk. The tests replace this function."""
    # A plain import, not "lazy import", so that PyInstaller finds the package.
    import sv_ttk

    return sv_ttk


def theme_name(root: tk.Tk) -> str:
    """Return the name of the ttk theme of ``root``, for example "sun-valley-dark"."""
    return ttk.Style(root).theme_use()


def apply_theme(root: tk.Tk, mode: str) -> str | None:
    """Show ``root`` in the light or dark Sun Valley theme. Return the ttk theme name.

    Return None when sv-ttk is not installed or does not load. Then the window keeps the
    ttk theme that it has, and the function writes the cause to stderr. The function does
    not raise, because the theme only changes the look of the app.
    """
    try:
        sv_ttk = _load_sv_ttk()
        first = SUN_VALLEY[0] not in ttk.Style(root).theme_names()
        sv_ttk.set_theme(mode, root)
        if first:
            _adjust_sun_valley(root)
    except Exception as e:
        _error(f"cannot use the Sun Valley theme: {_describe(e)}")
        return None
    name = theme_name(root)
    return name if name in SUN_VALLEY else None


def _adjust_sun_valley(root: tk.Tk) -> None:
    """Change the Sun Valley themes in ``root`` once, after sv-ttk loads them."""
    ttk.Style(root).theme_settings(
        "sun-valley-light",
        {
            "Accent.TButton": {
                "map": {
                    "foreground": [("pressed", "#c1d8ee"), ("disabled", LIGHT_DISABLED_ACCENT_FG)]
                }
            }
        },
    )
    # The list of a Combobox is a Tk Listbox. sv-ttk does not give it a font, so its text
    # was smaller than the text in the field.
    root.option_add("*TCombobox*Listbox.font", "SunValleyBodyFont")
    # sv-ttk gives its fonts a size in pixels. Tk does not scale a size in pixels for the
    # screen resolution. Scale the fonts, so that the text has the same size as in other
    # apps at 125 % or 150 %.
    scale = root.winfo_fpixels("1i") / 96
    if abs(scale - 1) < 0.01:
        return
    for name in tkfont.names(root):
        if name.startswith("SunValley"):
            font = tkfont.nametofont(name, root=root)
            size = int(font.cget("size"))
            if size < 0:
                font.configure(size=-round(-size * scale))


def set_title_bar(root: tk.Tk, dark: bool, platform: str | None = None) -> bool:
    """Make the title bar of ``root`` dark or light. Return True when Windows accepts it.

    Only Windows 10 version 20H1 and later can do this. On other systems, and for a window
    that Tk did not show yet, the function does nothing. It does not raise.
    """
    if (platform or sys.platform) != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        dwmapi = ctypes.WinDLL("dwmapi")
        # winfo_id() is the client area. Its parent is the frame with the title bar.
        hwnd = user32.GetParent(root.winfo_id())
        if not hwnd:
            return False
        value = ctypes.c_int(1 if dark else 0)
        result = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if result != 0:
            return False
        # Windows draws the new title bar only when the frame changes. This call redraws
        # the frame and does not hide the window.
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, SWP_REDRAW_FRAME)
    except Exception:
        return False
    return True


class ThemeFollower:
    """Show ``root`` in the Sun Valley theme, light or dark, as ``choice`` tells.

    ``choice`` is "system", "light" or "dark". ``source`` returns the system setting,
    "light" or "dark". With "system", the class reads ``source`` each ``interval``
    milliseconds and changes the theme when the value changes. With "light" or "dark", it
    does not read ``source``. :meth:`choose` changes the choice. The title bar on Windows
    gets the same mode. When sv-ttk does not load, ``theme`` is None, and the window keeps
    its ttk theme.

    On Windows, the window is transparent until it shows for the first time. Windows draws
    the first frame before Tk can set the title bar, and Tk fills the window with white
    before it draws the theme. Without this, a dark window starts with a white flash.
    """

    def __init__(
        self,
        root: tk.Tk,
        source: Callable[[], str] = system_theme,
        *,
        choice: str = "system",
        interval: int = THEME_POLL_MS,
    ) -> None:
        if choice not in THEME_CHOICES:
            raise ValueError(f"unknown theme choice: {choice!r}")
        self.root = root
        self.source = source
        self.interval = interval
        self.choice = choice
        self.mode = source() if choice == "system" else choice
        self.theme = apply_theme(root, self.mode)
        self._after_id: str | None = None
        self._hidden = False
        if self.theme is None:
            return
        if sys.platform == "win32" and not root.winfo_ismapped():
            root.attributes("-alpha", 0.0)
            self._hidden = True
        root.bind("<Map>", self._on_map, add="+")
        root.bind("<Destroy>", self._on_destroy, add="+")
        if choice == "system":
            self._after_id = root.after(interval, self.poll)

    def _on_map(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        set_title_bar(self.root, dark=self.mode == "dark")
        if self._hidden:
            self._hidden = False
            self.root.update_idletasks()
            self.root.after(SHOW_DELAY_MS, self._show)

    def _show(self) -> None:
        with contextlib.suppress(tk.TclError):  # the window can be gone
            self.root.attributes("-alpha", 1.0)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self.stop()

    def poll(self) -> None:
        """Read ``source``. Change the theme and the title bar if the value changed.

        If the theme does not change, the reads stop. Then the window keeps its theme, and
        the app does not write the same error each ``interval``.
        """
        self._after_id = None
        mode = self.source()
        if mode != self.mode:
            theme = apply_theme(self.root, mode)
            if theme is None:
                return
            self.mode = mode
            self.theme = theme
            set_title_bar(self.root, dark=mode == "dark")
        self._after_id = self.root.after(self.interval, self.poll)

    def stop(self) -> None:
        """Stop the reads of ``source``."""
        if self._after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._after_id)
            self._after_id = None

    def choose(self, choice: str) -> bool:
        """Use the theme choice "system", "light" or "dark". Return True when it is in use.

        "light" and "dark" stop the reads of ``source``. "system" reads ``source`` at once
        and starts the reads again. When the theme cannot change, for example without
        sv-ttk, the choice stays as it was, and the result is False.
        """
        if choice not in THEME_CHOICES:
            raise ValueError(f"unknown theme choice: {choice!r}")
        if self.theme is None:
            return False
        self.stop()
        mode = self.source() if choice == "system" else choice
        if mode != self.mode:
            theme = apply_theme(self.root, mode)
            if theme is None:
                return False
            self.mode = mode
            self.theme = theme
            set_title_bar(self.root, dark=mode == "dark")
        self.choice = choice
        if choice == "system":
            self._after_id = self.root.after(self.interval, self.poll)
        return True


def follow_theme(root: tk.Tk, settings: Path | None = None) -> ThemeFollower:
    """Show ``root`` in the theme choice of ``PIXHEET_THEME``, or else in the saved choice.

    ``settings`` is the settings file. Without a choice, the window follows the system
    light or dark mode.
    """
    choice = theme_override() or read_theme_choice(settings)
    return ThemeFollower(root, system_theme, choice=choice)


class App:
    """The main window. Make it on a Tk root, then call ``root.mainloop()``.

    ``dnd_type`` is the result of :func:`enable_dnd`. When it is None, the window has no
    drop zone, and the user must use Browse.

    ``follower`` is the result of :func:`follow_theme`. With a follower in the Sun Valley
    theme, the header row shows the theme toggle. A choice in the toggle goes to the
    settings file ``settings``. Without ``settings``, the app does not save the choice.

    Without sv-ttk, the window has no toggle. The standard ttk theme has only one look, so
    the choices cannot change the window. A toggle that does nothing only confuses.
    """

    def __init__(
        self,
        root: tk.Tk,
        *,
        dnd_type: str | None,
        follower: ThemeFollower | None = None,
        settings: Path | None = None,
    ) -> None:
        self.root = root
        self.follower = follower if follower is not None and follower.theme else None
        self.settings = settings
        self.queue: queue.SimpleQueue[Message] = queue.SimpleQueue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self._dialogs = 0
        """The number of open modal dialogs. A modal dialog runs a nested event loop."""
        self.last_output: Path | None = None
        self.errors: list[str] = []
        self._poll_id: str | None = None
        self._inputs = 0
        self._skipped = 0
        self._last_summary = ""
        # The dialogs. The tests replace them, because a modal dialog stops the test.
        self.ask_yes_no: Callable[..., bool] = messagebox.askyesno
        self.show_error: Callable[..., object] = messagebox.showerror
        self.ask_files: Callable[..., Sequence[str]] = filedialog.askopenfilenames

        self._build()
        self.has_icon = set_icon(root)
        self.dnd = dnd_type is not None and self._register_drop(dnd_type)
        if not self.dnd:
            self.zone_label.configure(text="Choose images to convert")
            self.or_label.grid_remove()  # Browse is the only way
            self.status_var.set(NO_DND_TEXT)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._report_exception
        self.fit_min_size()

    # Layout

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        sun_valley = theme_name(root) in SUN_VALLEY
        if sun_valley:
            # The heading font of sv-ttk. Its size follows the screen resolution.
            self._big_font = tkfont.nametofont("SunValleySubtitleFont", root=root)
        else:
            self._big_font = tkfont.nametofont("TkDefaultFont", root=root).copy()
            size = self._big_font.cget("size")
            self._big_font.configure(size=round(size * 1.6))

        body = ttk.Frame(root, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.body = body

        # The header row. Column 0 stretches. It keeps the left part free for more controls.
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.header = header
        self._build_theme_toggle(header, sun_valley)

        if sun_valley:
            zone = ttk.Frame(body, style="Card.TFrame", padding=24)
        else:
            zone = ttk.Frame(body, padding=24, borderwidth=2, relief="groove")
        zone.grid(row=1, column=0, sticky="nsew")
        zone.columnconfigure(0, weight=1)
        zone.rowconfigure(0, weight=1)
        zone.rowconfigure(3, weight=1)
        self.zone = zone
        self.zone_label = ttk.Label(zone, text=DROP_TEXT, font=self._big_font, anchor="center")
        self.zone_label.grid(row=1, column=0, pady=(0, 8))
        self.or_label = ttk.Label(zone, text="or")
        self.or_label.grid(row=2, column=0, pady=(0, 8))
        # Other themes show the style Accent.TButton as a normal button.
        self.browse_button = ttk.Button(
            zone, text="Browse...", style="Accent.TButton", command=self.browse
        )
        self.browse_button.grid(row=3, column=0, sticky="n")

        options = ttk.Frame(body, padding=(0, 12, 0, 0))
        options.grid(row=2, column=0, sticky="ew")
        self.max_side_var = tk.StringVar(root, value=str(DEFAULT_MAX_SIDE))
        self.background_var = tk.StringVar(root, value=DEFAULT_BACKGROUND)
        self.compact_var = tk.BooleanVar(root, value=False)
        ttk.Label(options, text="Maximum side:").grid(row=0, column=0, padx=(0, 4))
        self.max_side_box = ttk.Spinbox(
            options, from_=0, to=1_048_576, increment=100, width=8, textvariable=self.max_side_var
        )
        self.max_side_box.grid(row=0, column=1, padx=(0, 16))
        ttk.Label(options, text="Background:").grid(row=0, column=2, padx=(0, 4))
        self.background_box = ttk.Combobox(
            options, values=BACKGROUNDS, width=9, textvariable=self.background_var
        )
        self.background_box.grid(row=0, column=3, padx=(0, 16))
        self.compact_check = ttk.Checkbutton(
            options, text="Compact file", variable=self.compact_var
        )
        self.compact_check.grid(row=0, column=4)
        for box in (self.max_side_box, self.background_box):
            box.bind("<<ThemeChanged>>", self._use_style_foreground, add="+")

        self.progress_var = tk.DoubleVar(root, value=0.0)
        self.progressbar = ttk.Progressbar(
            body, mode="determinate", maximum=1.0, variable=self.progress_var
        )
        self.progressbar.grid(row=3, column=0, sticky="ew", pady=(12, 4))
        self.title_var = tk.StringVar(root, value="")
        ttk.Label(body, textvariable=self.title_var).grid(row=4, column=0, sticky="w")
        self.status_var = tk.StringVar(root, value="")
        self.status_label = ttk.Label(body, textvariable=self.status_var, justify="left")
        self.status_label.grid(row=5, column=0, sticky="ew")
        body.bind("<Configure>", self._wrap_status, add="+")
        # A note that the worker messages do not replace, for example about a refused drop.
        self.note_var = tk.StringVar(root, value="")
        ttk.Label(body, textvariable=self.note_var).grid(row=6, column=0, sticky="w")

        buttons = ttk.Frame(body, padding=(0, 12, 0, 0))
        buttons.grid(row=7, column=0, sticky="e")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button.grid_remove()
        self.open_button = ttk.Button(buttons, text="Open folder", command=self.open_output)
        self.open_button.grid(row=0, column=1)
        self.open_button.state(["disabled"])

    def _build_theme_toggle(self, header: ttk.Frame, sun_valley: bool) -> None:
        """Make the System, Light and Dark toggle at the right of the header row.

        The toggle is one stop of the Tab key. The arrow keys select the next choice or
        the previous choice, as in a group of radio buttons on Windows.
        """
        follower = self.follower
        self.theme_var = tk.StringVar(
            self.root, value=follower.choice if follower is not None else "system"
        )
        self.theme_buttons: list[ttk.Radiobutton] = []
        if follower is None or not sun_valley:
            return
        toggle = ttk.Frame(header)
        toggle.grid(row=0, column=1, sticky="e", pady=(0, 12))
        for column, choice in enumerate(THEME_CHOICES):
            button = ttk.Radiobutton(
                toggle,
                text=choice.title(),
                value=choice,
                variable=self.theme_var,
                style="Toggle.TButton",
                command=self.choose_theme,
            )
            button.grid(row=0, column=column)
            for key, step in (("<Left>", -1), ("<Up>", -1), ("<Right>", 1), ("<Down>", 1)):
                button.bind(key, lambda event, step=step: self._step_theme(step))
            self.theme_buttons.append(button)
        self._theme_tab_stop()

    def _theme_tab_stop(self) -> None:
        """Let the Tab key stop only at the selected choice of the theme toggle."""
        choice = self.theme_var.get()
        for button in self.theme_buttons:
            button.configure(takefocus=int(button.cget("value") == choice))

    def _step_theme(self, step: int) -> str:
        """Select the choice ``step`` places from the current choice, and give it the focus."""
        index = THEME_CHOICES.index(self.theme_var.get()) + step
        button = self.theme_buttons[index % len(self.theme_buttons)]
        button.focus_set()
        button.invoke()
        return "break"

    def choose_theme(self) -> None:
        """Use the choice of the theme toggle, and save it in the settings file."""
        follower = self.follower
        choice = self.theme_var.get()
        if follower is not None and choice != follower.choice:
            if not follower.choose(choice):
                self.theme_var.set(follower.choice)
            elif self.settings is not None:
                write_theme_choice(self.settings, choice)
        self._theme_tab_stop()

    def _use_style_foreground(self, event: tk.Event) -> None:
        """Let the style set the text color of the Spinbox and the Combobox.

        On each theme change, sv-ttk calls tk_setPalette. tk_setPalette gives each widget
        a fixed text color. Then the text does not change to gray when the widget is
        disabled, and the user cannot see that the options are locked during a batch.
        ttk::ThemeChanged sends the event to the root first, so tk_setPalette is done here.
        """
        event.widget.configure(foreground="")

    def fit_min_size(self) -> None:
        """Make the minimum size of the window large enough for all the widgets.

        The size that the widgets need changes with the theme, the fonts, and the screen
        resolution. A smaller window cuts off a part of the options row.
        """
        self.root.update_idletasks()
        self.root.minsize(
            max(MIN_WIDTH, self.root.winfo_reqwidth()), max(MIN_HEIGHT, self.root.winfo_reqheight())
        )

    def _wrap_status(self, event: tk.Event) -> None:
        self.status_label.configure(wraplength=max(event.width - 32, 100))

    def _register_drop(self, dnd_type: str) -> bool:
        try:
            for widget in (self.body, self.zone):
                widget.drop_target_register(dnd_type)
                widget.dnd_bind("<<Drop>>", self.on_drop)
        except tk.TclError:
            return False
        return True

    # Events

    @property
    def busy(self) -> bool:
        """True while a batch runs or a modal dialog is open.

        A batch runs from its start until the window reads its last message. While a
        dialog is open, events still come in. The app refuses a drop in that time, so
        that a second batch cannot start in the middle of the code that opened the dialog.
        """
        return self.worker is not None or self._dialogs > 0

    def _modal[T](self, dialog: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Show a modal dialog. The app is busy until the dialog closes."""
        self._dialogs += 1
        try:
            return dialog(*args, **kwargs)
        finally:
            self._dialogs -= 1

    def _close_if_asked(self) -> bool:
        """Destroy the window if the user closed it while a dialog was open.

        Call this after each dialog. Return True when the window is gone.
        """
        if not self.closing or self.busy:
            return False
        self.dispose()
        with contextlib.suppress(tk.TclError):
            self.root.destroy()
        return True

    def on_drop(self, event: Any) -> str:
        """Handle a file drop. Return the drop action for tkdnd."""
        if self.busy or self.closing:
            if not self.closing:
                self.note_var.set(BUSY_DROP_TEXT)
            return "refuse_drop"
        files = parse_drop(event.data, self.root.tk.splitlist)
        # Return to tkdnd at once: the file manager waits until the drop handler ends.
        self.root.after_idle(self.start, files)
        return "copy"

    def browse(self) -> None:
        """Let the user choose images, then convert them."""
        if self.busy or self.closing:
            return
        names = self._modal(
            self.ask_files, parent=self.root, title="Choose images", filetypes=IMAGE_TYPES
        )
        if self._close_if_asked():
            return
        if names:
            self.start([Path(name) for name in names])

    def start(self, sources: Sequence[Path]) -> None:
        """Convert ``sources`` on a worker thread. Refuse them while the app is busy."""
        if self.closing:
            return
        if self.busy:
            self.note_var.set(BUSY_DROP_TEXT)
            return
        self.note_var.set("")  # before the dialogs, which can refuse a new drop
        if not sources:
            self.status_var.set("The drop has no files. Drop image files, not folders.")
            return
        try:
            options = parse_options(
                self.max_side_var.get(), self.background_var.get(), self.compact_var.get()
            )
        except ValueError as e:
            self._modal(self.show_error, TITLE, str(e), parent=self.root)
            self._close_if_asked()
            return
        plan = plan_jobs(sources)
        jobs = plan.jobs
        self._skipped = 0
        replace = not plan.existing or self._replace(plan.existing)
        if self._close_if_asked():
            return
        if not replace:
            skip = set(plan.existing)
            jobs = [job for job in jobs if job not in skip]
            self._skipped = len(skip)
        self.errors = list(plan.errors)
        self._inputs = len(sources)
        self._last_summary = ""
        # A new queue for each batch, so that no message of an earlier batch can arrive.
        self.queue = queue.SimpleQueue()
        self.cancel_event = threading.Event()
        self.worker = threading.Thread(
            target=run_batch,
            args=(jobs, options, self.queue.put, self.cancel_event),
            name="pixheet-convert",
            daemon=True,
        )
        self._set_busy(True)
        self.progress_var.set(0.0)
        self.worker.start()
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def _replace(self, existing: Sequence[Job]) -> bool:
        """Ask once if the batch can replace the output files that exist."""
        names = [str(job.dst) for job in existing[:MAX_LISTED]]
        if len(existing) > MAX_LISTED:
            names.append(f"and {len(existing) - MAX_LISTED:,} more")
        if len(existing) == 1:
            question = "An output file exists. Do you want to replace it?"
        else:
            question = f"{len(existing):,} output files exist. Do you want to replace them?"
        detail = "If you click No, the app does not convert these images."
        message = f"{question}\n\n" + "\n".join(names) + f"\n\n{detail}"
        return bool(self._modal(self.ask_yes_no, TITLE, message, parent=self.root))

    def cancel(self) -> None:
        """Stop the batch. The current file stops at its next row."""
        if self.worker is None:
            return
        self.cancel_event.set()
        self.cancel_button.state(["disabled"])
        self.status_var.set("Stopping.")

    def close(self) -> None:
        """Close the window. During a batch, stop it first and close when the worker ends.

        The worker must end before the process does. If it does not, a temporary file
        can stay next to the output.
        """
        if not self.busy:
            self.dispose()
            self.root.destroy()
            return
        self.closing = True
        if self.worker is None:
            return  # a dialog is open; the window closes after the dialog
        self.cancel()
        self.status_var.set("Stopping. The window closes when the current file stops.")

    def dispose(self) -> None:
        """Cancel the timer that reads the queue."""
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None

    def open_output(self) -> None:
        if self.last_output is None:
            return
        try:
            open_folder(self.last_output)
        except OSError as e:
            self._modal(self.show_error, TITLE, f"Cannot open the folder: {e}", parent=self.root)
            self._close_if_asked()

    def _set_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        for widget in (
            self.browse_button,
            self.max_side_box,
            self.background_box,
            self.compact_check,
        ):
            widget.state(state)
        if busy:
            self.cancel_button.state(["!disabled"])
            self.cancel_button.grid()
            self.open_button.state(["disabled"])
        else:
            self.cancel_button.grid_remove()
            if self.last_output is not None:
                self.open_button.state(["!disabled"])

    # Messages from the worker

    def _poll(self) -> None:
        self._poll_id = None
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            self._handle(message)
            if self.worker is None:  # the batch ended, and the window can be gone
                return
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def _handle(self, message: Message) -> None:
        match message:
            case Started(index, count, src):
                self.title_var.set(f"File {index:,} of {count:,}: {src.name}")
                self.progress_var.set(0.0)
                if not self.cancel_event.is_set():  # keep the text "Stopping."
                    self.status_var.set("Reading the image.")
            case Progress(done, total):
                self.progress_var.set(done / total)
                if not self.cancel_event.is_set():
                    self.status_var.set(f"Writing row {done:,} of {total:,}.")
            case Converted(job, result):
                self.last_output = job.dst
                self._last_summary = summary(job.dst, result)
                self.progress_var.set(1.0)
                self.status_var.set(self._last_summary)
            case Failed(src, error):
                self.errors.append(f"{src.name}: {error}")
            case Finished(converted, cancelled):
                self._finish(converted, cancelled)

    def _finish(self, converted: int, cancelled: bool) -> None:
        if self.worker is not None:
            self.worker.join()
            self.worker = None
        if self.closing:
            self.root.destroy()
            return
        self._set_busy(False)
        self.title_var.set(self._result_text(converted, cancelled))
        lines = [self._last_summary] if self._last_summary else []
        if cancelled:
            self.progress_var.set(0.0)
            lines.append("The app did not write the current file.")
        else:
            self.progress_var.set(1.0)
        if self.errors:
            lines.append(f"{_plural(len(self.errors), 'file')} not converted.")
        self.status_var.set("\n".join(lines))
        if self.errors:
            lines = self.errors[:MAX_LISTED]
            if len(self.errors) > MAX_LISTED:
                lines.append(f"and {len(self.errors) - MAX_LISTED:,} more")
            self._modal(
                self.show_error,
                TITLE,
                "The app did not convert these files:\n\n" + "\n".join(lines),
                parent=self.root,
            )
            self._close_if_asked()

    def _result_text(self, converted: int, cancelled: bool) -> str:
        if cancelled:
            text = f"Stopped. Converted {converted:,} of {_plural(self._inputs, 'file')}."
        elif converted == self._inputs:
            text = f"Converted {_plural(converted, 'file')}."
        else:
            text = f"Converted {converted:,} of {_plural(self._inputs, 'file')}."
        if self._skipped:
            text += f" Skipped {_plural(self._skipped, 'file')} that exist."
        return text

    def _report_exception(self, exc: type[BaseException], value: BaseException, tb) -> None:
        """Show an error in a Tk callback in a dialog. A windowed app has no console."""
        if sys.stderr is not None:
            import traceback

            traceback.print_exception(exc, value, tb)
        self._modal(
            self.show_error, TITLE, f"An unexpected error occurred:\n\n{value!r}", parent=self.root
        )
        self._close_if_asked()


# --- Start ---------------------------------------------------------------------------


def _error(text: str) -> None:
    # A windowed app on Windows has no stderr.
    if sys.stderr is not None:
        print(f"pixheet-gui: {text}", file=sys.stderr)


def _set_dpi_awareness() -> None:
    """On Windows, tell the system that the app draws for the screen resolution.

    Without this, Windows scales the window as a bitmap, and the text is not sharp.
    """
    if sys.platform != "win32":
        return
    import ctypes

    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware


def smoke_test(app: App) -> list[str]:
    """Prepare ``app`` for a smoke test, and return the list that collects the problems.

    The window closes 200 ms after the start, or later when a batch runs, after the
    batch. The app does not show dialogs. It records each error message as a problem,
    and it does not replace output files that exist. A built app must also have the
    window icon, drag and drop, and the Sun Valley theme.
    """
    problems: list[str] = []
    if getattr(sys, "frozen", False):
        if not app.has_icon:
            problems.append("The window icon is missing.")
        if not app.dnd:
            problems.append("Drag and drop is not available.")
        if theme_name(app.root) not in SUN_VALLEY:
            problems.append("The Sun Valley theme is not available.")

    def record(title: str, message: str, **kwargs: Any) -> None:
        problems.append(message)

    app.show_error = record
    app.ask_yes_no = lambda *args, **kwargs: False

    def close_when_idle() -> None:
        if app.busy:
            app.root.after(POLL_MS, close_when_idle)
        else:
            app.close()

    app.root.after(200, close_when_idle)
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Open the window. Convert the image paths in ``argv``, if there are some.

    With ``--smoke``, the window closes when it has nothing to do. Then the exit status is
    1 if the app found a problem. See :func:`smoke_test`. The window uses the theme choice
    in the settings file. Without a choice, it follows the system light or dark mode. The
    environment variable ``PIXHEET_THEME`` can set "system", "light" or "dark" at the
    start. It wins over the settings file.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = "--smoke" in args
    files = [Path(arg) for arg in args if arg != "--smoke"]
    if tk is None:
        _error("tkinter is not available. Install a Python that includes Tk.")
        return 1
    _set_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as e:
        _error(f"cannot open a window: {e}")
        return 1
    # Load tkdnd into the plain tk.Tk root first, then the theme. The tests use this order.
    dnd_type = enable_dnd(root)
    settings = settings_path()
    follower = follow_theme(root, settings)
    app = App(root, dnd_type=dnd_type, follower=follower, settings=settings)
    problems = smoke_test(app) if smoke else []
    if files:
        root.after_idle(app.start, files)
    if smoke and sys.stdout is not None:
        print(f"pixheet-gui: drag and drop {'on' if app.dnd else 'off'}", flush=True)
        print(f"pixheet-gui: theme {theme_name(root)}", flush=True)
    root.mainloop()
    for problem in problems:
        _error(problem.replace("\n", " "))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
