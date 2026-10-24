"""Desktop app: drop images or folders on the window to convert each image to an .xlsx file.

Start it with ``pixheet-gui`` or ``python -m pixheet.gui``. The app converts the image
paths that you give as arguments when it starts. The option ``--smoke`` opens the window
and closes it when the app has nothing to do. Use it to test a built app.

Each output file goes next to its image, with the suffix ``.xlsx``, or into the folder
that "Save to" names. One image shows as today: the drop zone, a progress bar and the
result. Two or more images show in a list, with one row for each image and its state.

A worker thread does the conversion, so the window stays responsive. The worker takes
jobs from a queue that the window can add to during a run. It sends messages to a
second queue, and the window reads that queue each 50 ms. Only the window thread uses Tk.

With sv-ttk, the window has the Sun Valley theme, the look of Windows 11. A toggle at the
top right sets the theme to System, Light or Dark. With System, the theme is the same as
the system setting, and it changes when the setting changes. The app saves the choice in
a settings file.
"""

import configparser
import contextlib
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # a Python build without Tk
    tk = None

from PIL import UnidentifiedImageError

from pixheet._desktop import (
    LatestValue,
    dnd_allowed,
    open_folder,
    open_path,
    settings_path,
    system_theme,
)
from pixheet._image import RGB, check_max_side, parse_background
from pixheet._plan import (
    CONVERTING,
    DONE,
    ELLIPSIS,
    FAILED,
    FINISHED_STATES,
    FOLDER_IN,
    NOT_AN_IMAGE,
    SEP,
    SKIPPED,
    STOPPED,
    WAITING,
    Converted,
    DropResult,
    Failed,
    Finished,
    Job,
    JobQueue,
    Message,
    Progress,
    ReplaceQuestion,
    Row,
    Started,
    Stopped,
    Tally,
    absolute,
    apply_message,
    assign_outputs,
    drop_note,
    eta_text,
    expand_drop,
    failure_detail,
    failure_kind,
    folder_labels,
    middle_ellipsis,
    output_key,
    path_key,
    path_problem,
    progress_text,
    status_text,
    subfolder_note,
    summary_line,
)
from pixheet.core import DEFAULT_BACKGROUND, DEFAULT_MAX_SIDE, ConvertResult, convert

TITLE = "Pixheet"
MIN_WIDTH, MIN_HEIGHT = 620, 460
"""The smallest size of the window. The window can need more, for example with large text."""
BODY_PADDING = 16
"""The space at the left and at the right of the window content, in pixels."""
SUMMARY_SAMPLE = "Stopped. Converted 9,999 of 99,999 files. 9,999 failed. 9,999 skipped."
"""A long progress line after a run. The minimum width of the window includes it."""
POLL_MS = 50
"""Time between two reads of the message queue, in milliseconds."""
BUSY_POLL_MS = 250
"""Time until the next read of the message queue after a read that found messages."""
PROGRESS_INTERVAL = 0.25
"""Minimum time between two progress messages from the worker, in seconds.

Each message changes the Status cell, and Tk then draws the whole list again. With more
than about 4 messages each second, clicks and scrolls are slow during a run.
"""
BAR_STEP = 0.01
"""The smallest move of the progress bar during a run, as a part of the full bar."""
NOTE_MS = 10_000
"""Time until a note without a button closes, in milliseconds."""
FOLLOW_ABOVE = 3
"""The maximum number of rows above the converting row when the list follows it."""
STATUS_WIDTH = 220
"""The width of the Status column at 100 % scale, in pixels."""
STATUS_PADDING = 12
"""The space at the two sides of the text in a cell, at 100 % scale, in pixels."""
COUNT_GAP = 8
"""The space between the count line and the Add files button, in pixels."""
WAITING_AS = f"Waiting{SEP}as "
"""The start of the status of a waiting row with a tagged output."""
LINE_PATH_LIMITS = (40, 28, 16)
"""The lengths of the paths in line 2 of the details. A long reason uses a shorter one."""
ARROW = " → "

DROP_TEXT = "Drop images or folders here"
NO_DND_TEXT = "Drag and drop is not available. Use Add files or Add folder."
FOLDER_HINT = "A folder adds only the images directly in it."
DIALOG_DROP_TEXT = "Close the dialog first. The app did not add the dropped files."
CANCELLED_TEXT = "The app did not add the dropped images."
NOT_STARTED_TEXT = "The app did not start the run."
NEXT_TO_IMAGE = "Next to each image"
CHOOSE_FOLDER = "Choose a folder..."
IMAGE_TYPES = [
    (
        "Images",
        "*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.webp *.avif *.ico "
        "*.ppm *.pgm *.pbm *.pnm *.tga *.psd",
    ),
    ("All files", "*.*"),
]
"""The file types of the file dialog on Windows."""
BACKGROUNDS = ("white", "black", "none")

ROW_COLORS = {
    "light": {"failed": "#c42b1c", "converting": "#005fb8", "muted": "#6b6b6b"},
    "dark": {"failed": "#ff99a4", "converting": "#60cdff", "muted": "#9d9d9d"},
}
"""The text colors of the rows. The theme does not change them, so they are fixed."""
NOTE_COLORS = {"light": ("#e8f1fb", "#1c1c1c"), "dark": ("#1f3346", "#ffffff")}
"""The background color and the text color of the note bar."""
STATE_TAGS = {
    WAITING: "muted",
    CONVERTING: "converting",
    DONE: "done",
    FAILED: "failed",
    SKIPPED: "muted",
    STOPPED: "muted",
}
"""The tag of a row in each state. The tag sets the text color."""


def image_types(platform: str | None = None) -> list[tuple[str, str]]:
    """Return the file types of the file dialog for the system.

    Windows compares the patterns without case. The Tk file dialog on X11 compares them
    with case, so each letter of a suffix gets a pattern for both cases, for example
    ``*.[pP][nN][gG]``. On X11, ``*.*`` does not show a file without a dot, so "All
    files" is ``*``.
    """
    if (platform or sys.platform) == "win32":
        return IMAGE_TYPES
    patterns = [
        "*." + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in p[2:])
        for p in IMAGE_TYPES[0][1].split()
    ]
    return [(IMAGE_TYPES[0][0], " ".join(patterns)), ("All files", "*")]


# --- Helpers that do not need Tk -----------------------------------------------------


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


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def changed_text(path: Path) -> str:
    """Return the time of the last change of ``path``, for example "2026-10-15 10:25"."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError, OverflowError, ValueError:
        return ""


# --- The worker ----------------------------------------------------------------------


class _Cancelled(Exception):
    """The user stopped the queue or the job. The progress function raises it."""


def _progress_hook(
    cancel: threading.Event, job: Job, post: Callable[[Message], None], throttle: Throttle
) -> Callable[[int, int], None]:
    def progress(done: int, total: int) -> None:
        if cancel.is_set() or job.skip.is_set():
            raise _Cancelled
        if throttle(done, total):
            post(Progress(job.id, done, total))

    return progress


def _describe(error: BaseException) -> str:
    """Return the text of an error for the user.

    The text of Pillow for a file that it cannot open has the full path in quotes, with
    two backslashes for each one. The row already shows the file, so the text is plain.
    The text of a system error has the error number, for example "[Errno 2]", and the
    paths in the same quotes. Thus the text is the reason and the plain paths.
    """
    if isinstance(error, UnidentifiedImageError):
        return NOT_AN_IMAGE
    if isinstance(error, OSError) and error.strerror:
        names = [str(name) for name in (error.filename, error.filename2) if name is not None]
        return f"{error.strerror}: {' -> '.join(names)}" if names else error.strerror
    return str(error) or type(error).__name__


def as_sentence(text: str) -> str:
    """Return ``text`` with an upper-case first letter and a final period.

    The reasons of errors are in lower case, because the app also shows them after a
    colon, for example "Failed: the app cannot read this file as an image".
    """
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


def run_queue(
    jobs: JobQueue,
    options: Options,
    post: Callable[[Message], None],
    cancel: threading.Event,
    interval: float = PROGRESS_INTERVAL,
) -> None:
    """Convert the jobs in ``jobs`` one after the other, and post a message for each step.

    The worker thread runs this function. It takes jobs until the queue is empty. The
    window can add jobs during the run. An error in one job does not stop the queue.

    - When the window sets the ``skip`` event of a job, the job stops, and the worker
      posts :class:`Stopped` for it. Then the worker continues with the next job.
    - When the window sets ``cancel``, the current job stops, and the worker posts
      :class:`Stopped` for it. Then the queue stops.

    The last message is always :class:`Finished`.
    """
    stopped = False
    try:
        while True:
            if cancel.is_set():
                stopped = True
                break
            job = jobs.take()
            if job is None:
                break
            if job.skip.is_set():
                post(Stopped(job.id))
                continue
            post(Started(job.id))
            progress = _progress_hook(cancel, job, post, Throttle(interval))
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
                post(Stopped(job.id))
                continue
            except Exception as e:
                post(Failed(job.id, _describe(e), failure_kind(e, job.dst)))
                continue
            size = 0
            with contextlib.suppress(OSError):
                size = job.dst.stat().st_size
            post(Converted(job.id, result, size))
    finally:
        post(Finished(stopped))


def _set_if_changed(var: tk.Variable, value: object) -> None:
    """Set ``var`` only when ``value`` is new. Each set makes Tk draw the widgets again."""
    if var.get() != value:
        var.set(value)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def summary(dst: Path, result: ConvertResult) -> str:
    """Return the status line for one converted file."""
    w, h = result.size
    return f"{dst}  {w}x{h}  {result.colors:,} colors  {result.seconds:.2f} s"


def result_text(result: ConvertResult) -> str:
    """Return the size, the colors and the time of a conversion, for the details line."""
    w, h = result.size
    return SEP.join([f"{w}x{h}", f"{result.colors:,} colors", f"{result.seconds:.2f} s"])


# --- The window ----------------------------------------------------------------------


def _load_tkdnd() -> tuple[Any, str]:
    """Import tkinterdnd2. Return its TkinterDnD module and the type name for files."""
    # A plain import, not "lazy import", so that PyInstaller finds the package.
    from tkinterdnd2 import DND_FILES, TkinterDnD

    return TkinterDnD, DND_FILES


ICON_NAME = "pixheet.ico"
"""The window icon on Windows."""
ICON_PNG = "pixheet.png"
"""The window icon on other systems. Tk reads PNG files without Pillow."""


def icon_path(name: str = ICON_NAME) -> Path | None:
    """Return the path of the icon file ``name``, or None when the app has no such file.

    The built app has the icon in its own folder. From a checkout, the icon is in
    ``packaging/``. An installed package does not include the icon.
    """
    if getattr(sys, "frozen", False):
        folder = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        folder = Path(__file__).resolve().parents[2] / "packaging"
    path = folder / name
    return path if path.is_file() else None


def set_icon(root: tk.Tk, platform: str | None = None) -> bool:
    """Show the Pixheet icon on ``root`` and on all windows that come later.

    Return True when the window has the icon. On Windows, Tk reads the .ico file. On
    other systems, Tk reads the PNG file. When the app does not find the icon file, the
    window keeps the Tk icon.
    """
    if (platform or sys.platform) == "win32":
        path = icon_path()
        if path is None:
            return False
        try:
            root.iconbitmap(default=os.fspath(path))
        except tk.TclError:
            return False
        return True
    path = icon_path(ICON_PNG)
    if path is None:
        return False
    try:
        image = tk.PhotoImage(master=root, file=os.fspath(path))
        root.iconphoto(True, image)
    except tk.TclError:
        return False
    root._pixheet_icon = image  # Tk deletes an image when Python has no reference to it
    return True


def enable_dnd(root: tk.Tk, platform: str | None = None) -> str | None:
    """Load the tkdnd extension into ``root``. Return the drop type for files.

    Return None when tkinterdnd2 is not installed or tkdnd does not load. Also return
    None when :func:`pixheet._desktop.dnd_allowed` is False: then the app does not load
    tkdnd, because the load can stop the process.
    """
    if not dnd_allowed(platform):
        return None
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


def theme_override(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the theme choice that the environment variable ``PIXHEET_THEME`` sets.

    The result is "system", "light" or "dark". Return None for no value and for an
    unknown value. Then the app uses the saved choice.
    """
    value = (os.environ if environ is None else environ).get(THEME_ENV, "")
    value = value.strip().lower()
    return value if value in THEME_CHOICES else None


SETTINGS_SECTION = "appearance"


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


def follow_theme(
    root: tk.Tk, settings: Path | None = None, platform: str | None = None
) -> ThemeFollower:
    """Show ``root`` in the theme choice of ``PIXHEET_THEME``, or else in the saved choice.

    ``settings`` is the settings file. Without a choice, the window follows the system
    light or dark mode. On Linux, the app runs commands to read the system setting. A
    worker thread runs them, so that a slow command does not block the window.
    """
    choice = theme_override() or read_theme_choice(settings)
    source: Callable[[], str] = system_theme
    if (platform or sys.platform) != "win32":
        source = LatestValue(system_theme, name="pixheet-theme")
    return ThemeFollower(root, source, choice=choice)


def _configure_style(root: tk.Tk, style: str, option: str, value: object) -> None:
    """Set an option of a ttk style in the current theme, when the value is new.

    A change of a style sends <<ThemeChanged>> to all widgets. The app sets its colors
    again on that event, so a change of the same value each time never ends.
    """
    styles = ttk.Style(root)
    if str(styles.configure(style, query_opt=option)) != str(value):
        styles.configure(style, **{option: value})


def style_font(root: tk.Misc, style: str) -> Any:
    """Return the font of a ttk style, or TkDefaultFont when the style has no font."""
    name = ttk.Style(root).lookup(style, "font") or "TkDefaultFont"
    try:
        return tkfont.nametofont(name, root=root)
    except tk.TclError:
        return tkfont.nametofont("TkDefaultFont", root=root)


def end_ellipsis(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters or fewer. Cut the end, and add "…"."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def middle_cut(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters or fewer. Put "…" in the middle.

    The end keeps a little more than the start, for example the type and ".xlsx".
    """
    if len(text) <= limit:
        return text
    keep = max(limit - 1, 0)
    tail = (keep + 1) // 2
    return f"{text[: keep - tail].rstrip()}{ELLIPSIS}{text[len(text) - tail :]}"


def take_line(font: Any, width: int, text: str) -> tuple[str, str]:
    """Split ``text`` into its first line of ``width`` pixels or fewer, and the rest.

    The line ends at a space, as the lines of a Tk label with a wraplength. A word that
    is too long for one line, for example a path, ends at the last letter that fits.
    """
    text = text.strip()
    if font.measure(text) <= width:
        return text, ""
    spaces = [index for index, char in enumerate(text) if char == " "]
    low, high, best = 0, len(spaces) - 1, -1
    while low <= high:
        middle = (low + high) // 2
        if font.measure(text[: spaces[middle]]) <= width:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    if best >= 0:
        end = spaces[best]
        return text[:end].rstrip(), text[end:].strip()
    low, high, end = 1, len(text) - 1, 1
    while low <= high:
        middle = (low + high) // 2
        if font.measure(text[:middle]) <= width:
            end, low = middle, middle + 1
        else:
            high = middle - 1
    return text[:end], text[end:].strip()


def fit_text(
    font: Any,
    width: int,
    text: str,
    shorten: Callable[[str, int], str] = middle_ellipsis,
    *,
    prefix: str = "",
    suffix: str = "",
) -> str:
    """Return ``prefix + text + suffix``, with ``text`` shortened to fit ``width`` pixels.

    ``shorten(text, limit)`` gives ``text`` with ``limit`` characters or fewer. The result
    uses the largest limit that fits. When no limit fits, the result has the shortest text.
    """
    full = f"{prefix}{text}{suffix}"
    if font.measure(full) <= width or len(text) <= 1:
        return full
    low, high = 1, len(text) - 1
    best = f"{prefix}{shorten(text, 1)}{suffix}"
    while low <= high:
        limit = (low + high) // 2
        line = f"{prefix}{shorten(text, limit)}{suffix}"
        if font.measure(line) <= width:
            best, low = line, limit + 1
        else:
            high = limit - 1
    return best


@dataclass(slots=True)
class _StatusFit:
    """The font and the width in pixels for the text of a Status cell, and the cells.

    ``cells`` maps (status, tagged) to the text of the cell. ``tagged`` is True for a
    waiting row with a tagged output.
    """

    font: Any
    room: int
    cells: dict[tuple[str, bool], str] = field(default_factory=dict)


class ReplaceDialog:
    """Ask one time what to do with the output files of a drop that exist.

    The answer is "replace", "skip" or "cancel". The Skip button has the focus, so Enter
    does not replace files. When the drop has no other images, the dialog has no Skip
    button, and Cancel has the focus. Esc and the close box give "cancel".

    A message box cannot name its buttons, so the dialog is a Toplevel. The list shows
    one bold row for each folder, and below it the files in that folder.
    """

    def __init__(
        self,
        root: tk.Tk,
        question: ReplaceQuestion,
        *,
        heading_font: Any,
        strong_font: Any,
        dark: bool = False,
    ) -> None:
        self.root = root
        self.answer = "cancel"
        self.dark = dark
        scale = root.winfo_fpixels("1i") / 96
        top = self.top = tk.Toplevel(root)
        top.withdraw()  # place it first, then show it
        top.title(TITLE)
        top.transient(root)
        top.resizable(False, False)
        body = ttk.Frame(top, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=question.heading, font=heading_font).pack(anchor="w")
        ttk.Label(body, text=question.text, wraplength=round(460 * scale), justify="left").pack(
            anchor="w", pady=(6, 10)
        )
        self.tree: ttk.Treeview | None = None
        if len(question.existing) == 1:
            # The folder gets "…" in the middle, so that a deep folder does not make the
            # dialog wider than the screen. The heading names the file.
            path = question.existing[0]
            changed = changed_text(path)
            line = fit_text(
                style_font(root, "TLabel"),
                round(460 * scale),
                str(path.parent),
                suffix=f"{SEP}{changed}" if changed else "",
            )
            ttk.Label(body, text=line).pack(anchor="w")
        else:
            self.tree = self._build_list(body, question, strong_font, scale)
        buttons = ttk.Frame(body, padding=(0, 16, 0, 0))
        buttons.pack(fill="x")
        self.buttons: dict[str, ttk.Button] = {}
        self.buttons["cancel"] = ttk.Button(
            buttons, text="Cancel", command=lambda: self.close("cancel")
        )
        self.buttons["cancel"].pack(side="right")
        if question.skip_label is not None:
            self.buttons["skip"] = ttk.Button(
                buttons,
                text=question.skip_label,
                style="Accent.TButton",
                command=lambda: self.close("skip"),
            )
            self.buttons["skip"].pack(side="right", padx=(0, 8))
        self.buttons["replace"] = ttk.Button(
            buttons, text=question.replace_label, command=lambda: self.close("replace")
        )
        self.buttons["replace"].pack(side="right", padx=(0, 8))
        self.focus = self.buttons.get("skip", self.buttons["cancel"])
        for button in self.buttons.values():
            button.bind("<Return>", lambda event: event.widget.invoke())
        top.bind("<Escape>", lambda event: self.close("cancel"))
        top.protocol("WM_DELETE_WINDOW", lambda: self.close("cancel"))
        top.bind("<Map>", self._on_map, add="+")
        top.update_idletasks()
        x, y = self._position(root, top)
        top.geometry(f"+{x}+{y}")

    @staticmethod
    def _position(root: tk.Tk, top: tk.Toplevel) -> tuple[int, int]:
        """Return the position of the dialog: centered on the main window, near its top.

        A monitor at the left of the primary monitor, or above it, has negative values.
        Thus the dialog stays on the screen of all monitors (the virtual screen), not in
        the area from 0 to the right. A main window off the screen, for example in the
        hidden mode, keeps its dialog off the screen too.
        """
        main_x, main_y = root.winfo_rootx(), root.winfo_rooty()
        width, height = top.winfo_reqwidth(), top.winfo_reqheight()
        x = main_x + (root.winfo_width() - width) // 2
        y = main_y + max(20, (root.winfo_height() - height) // 3)
        left, top_edge = root.winfo_vrootx(), root.winfo_vrooty()
        right = left + root.winfo_vrootwidth()
        bottom = top_edge + root.winfo_vrootheight()
        if left <= main_x < right and top_edge <= main_y < bottom:
            x = max(left, min(x, right - width))
            y = max(top_edge, min(y, bottom - height))
        return x, y

    @staticmethod
    def _build_list(
        body: ttk.Frame, question: ReplaceQuestion, strong_font: Any, scale: float
    ) -> ttk.Treeview:
        # A flat list with bold folder rows, not a tree. With Tk 9, sv-ttk shows the
        # wrong arrows for the rows of a tree.
        groups = question.folders
        count = len(question.existing) + len(groups)
        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            box,
            columns=("file", "changed"),
            show="headings",
            height=min(9, count),
            selectmode="none",
            takefocus=0,
        )
        tree.heading("file", text="File", anchor="w")
        tree.heading("changed", text="Changed", anchor="w")
        tree.column("file", width=round(330 * scale), anchor="w")
        tree.column("changed", width=round(140 * scale), stretch=False, anchor="w")
        tree.tag_configure("folder", font=strong_font)
        for folder, files in groups:
            label = middle_ellipsis(str(folder), 48)
            tree.insert("", "end", values=(label, _plural(len(files), "file")), tags=("folder",))
            for path in files:
                tree.insert("", "end", values=(f"      {path.name}", changed_text(path)))
        tree.pack(side="left", fill="both", expand=True)
        if count > 9:
            bar = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
        return tree

    def _on_map(self, event: tk.Event) -> None:
        if event.widget is self.top:
            set_title_bar(self.top, dark=self.dark)

    def run(self) -> str:
        """Show the dialog and wait until it closes. Return the answer."""
        self.top.deiconify()
        self.focus.focus_set()
        with contextlib.suppress(tk.TclError):  # X11 refuses a grab of a hidden window
            self.top.grab_set()
        self.top.wait_window()
        return self.answer

    def close(self, answer: str) -> None:
        """Close the dialog with ``answer``."""
        self.answer = answer
        with contextlib.suppress(tk.TclError):
            self.top.grab_release()
            self.top.destroy()


class App:
    """The main window. Make it on a Tk root, then call ``root.mainloop()``.

    ``dnd_type`` is the result of :func:`enable_dnd`. When it is None, the window has no
    drop zone, and the user must use Add files or Add folder.

    ``follower`` is the result of :func:`follow_theme`. With a follower in the Sun Valley
    theme, the header row shows the theme toggle. A choice in the toggle goes to the
    settings file ``settings``. Without ``settings``, the app does not save the choice.

    Without sv-ttk, the window has no toggle. The standard ttk theme has only one look, so
    the choices cannot change the window. A toggle that does nothing only confuses.

    The window has two states. With no image or one image, it shows the drop zone. From
    the second image, it shows the list, until the user clicks Clear list. The app keeps
    the counts of the row states in :class:`Tally` objects. Thus a message of the worker
    changes only one row and some counts, also in a list of many rows.
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
        self.jobs = JobQueue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.options: Options | None = None
        """The options of the run. A drop during the run uses them too."""
        self.closing = False
        self._dialogs = 0
        """The number of open modal dialogs. A modal dialog runs a nested event loop."""
        self._error_open = False
        """True while the dialog of an unexpected error is open."""
        self._poll_id: str | None = None
        self._note_id: str | None = None
        self._min_width_id: str | None = None
        self._min_size = (MIN_WIDTH, MIN_HEIGHT)
        """The minimum size that fit_min_size() measured."""
        self.note_action: Callable[[], None] | None = None

        self.rows: dict[int, Row] = {}
        """The rows of the list, in the order of the list."""
        self._last_id = 0
        self.tally = Tally()
        """The counts of the states of all rows."""
        self.run_ids: set[int] = set()
        self.run_tally = Tally()
        """The counts of the states of the rows of the current run, or of the last run."""
        self.jobs_by_id: dict[int, Job] = {}
        """The jobs that the worker did not finish."""
        self.current: int | None = None
        """The id of the row that converts now."""
        self._current_text = ""
        self._stopping_text = ""
        self._run_started = 0.0
        self._run_seconds = 0.0
        self._run_processed = 0
        self.done_ids: dict[int, None] = {}
        """The ids of the done rows. A dict keeps the order."""
        self.done_folders: dict[str, list[Any]] = {}
        """For each output folder of the done rows: [folder, number of done rows]."""
        self.out_dir: Path | None = None
        """The "Save to" folder, or None for the folder of each image."""
        self.list_mode = False
        self.follow = True
        """True when the list keeps the converting row in view."""
        self.failed_only = False
        self.labels: dict[str, str] = {}
        self._status_failed = False
        self._status_fit: _StatusFit | None = None
        """The font, the width and the cells of the Status column during a batch of rows."""

        # The dialogs and the system. The tests replace them, because a modal dialog
        # stops the test.
        self.show_error: Callable[..., object] = messagebox.showerror
        self.ask_files: Callable[..., Any] = filedialog.askopenfilenames
        self.ask_folder: Callable[..., Any] = filedialog.askdirectory
        self.ask_replace: Callable[[ReplaceQuestion], str] = self._ask_replace

        self._build()
        self.has_icon = set_icon(root)
        self.dnd = dnd_type is not None and self._register_drop(dnd_type)
        if not self.dnd:
            self.zone_label.configure(text="Choose images to convert")
            self.or_label.grid_remove()  # the buttons are the only way
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._report_exception
        self._apply_colors()
        self._refresh_all()
        self.fit_min_size()

    # Layout

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        sun_valley = theme_name(root) in SUN_VALLEY
        self._scale = root.winfo_fpixels("1i") / 96
        if sun_valley:
            # The fonts of sv-ttk. Their size follows the screen resolution.
            self._big_font = tkfont.nametofont("SunValleySubtitleFont", root=root)
            self._strong_font = tkfont.nametofont("SunValleyBodyStrongFont", root=root)
        else:
            self._big_font = tkfont.nametofont("TkDefaultFont", root=root).copy()
            size = self._big_font.cget("size")
            self._big_font.configure(size=round(size * 1.6))
            self._strong_font = tkfont.nametofont("TkDefaultFont", root=root).copy()
            self._strong_font.configure(weight="bold")

        body = ttk.Frame(root, padding=(BODY_PADDING, 12, BODY_PADDING, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.body = body

        # Row 0: "Save to" at the left, the theme toggle at the right.
        header = ttk.Frame(body, padding=(0, 0, 0, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.header = header
        save = ttk.Frame(header)
        save.grid(row=0, column=0, sticky="w")
        ttk.Label(save, text="Save to:").grid(row=0, column=0, padx=(0, 6))
        self.save_var = tk.StringVar(root, value=NEXT_TO_IMAGE)
        self.save_box = ttk.Combobox(
            save,
            textvariable=self.save_var,
            state="readonly",
            width=30,
            values=self._save_values(),
        )
        self.save_box.grid(row=0, column=1)
        self.save_box.bind("<<ComboboxSelected>>", self._on_save_to)
        self._build_theme_toggle(header, sun_valley)

        # Row 1: the conversion options.
        options = ttk.Frame(body, padding=(0, 0, 0, 10))
        options.grid(row=1, column=0, sticky="ew")
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
        for box in (self.save_box, self.max_side_box, self.background_box):
            box.bind("<<ThemeChanged>>", self._use_style_foreground, add="+")

        # Row 2: the drop zone or the list. Both use the same cell.
        area = ttk.Frame(body)
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.area = area
        self._build_zone(area, sun_valley)
        self._build_list(area)

        # Row 3: a note that the user can close, with an optional action.
        self._build_note(body)

        # Rows 4 and 5: the progress of the run.
        self.progress_var = tk.DoubleVar(root, value=0.0)
        self.progressbar = ttk.Progressbar(
            body, mode="determinate", maximum=1.0, variable=self.progress_var
        )
        self.progressbar.grid(row=4, column=0, sticky="ew", pady=(12, 4))
        line = ttk.Frame(body)
        line.grid(row=5, column=0, sticky="ew")
        line.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(root, value="")
        self.title_label = ttk.Label(line, textvariable=self.title_var)
        self.title_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.title_var.trace_add("write", lambda *args: self._fit_min_width())
        self.eta_var = tk.StringVar(root, value="")
        self.eta_label = ttk.Label(line, textvariable=self.eta_var)
        self.eta_label.grid(row=0, column=1, sticky="e")
        self.failed_only_var = tk.BooleanVar(root, value=False)
        self.failed_only_button = ttk.Checkbutton(
            line,
            variable=self.failed_only_var,
            style="Toggle.TButton",
            command=lambda: self._set_failed_only(self.failed_only_var.get()),
        )
        self.failed_only_button.grid(row=0, column=1, sticky="e")
        self.failed_only_button.grid_remove()

        # Rows 6 and 7: the details of the selected row in the list, on two lines: the
        # files, then the result. With one image, row 7 is the status of the image.
        self.path_var = tk.StringVar(root, value="")
        # A width of one character: a long text must not make the window wider.
        self.path_label = ttk.Label(body, textvariable=self.path_var, width=1)
        self.path_label.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self._line_width = 600
        """The width of one line of the details, in pixels."""
        self.status_var = tk.StringVar(root, value="")
        self.status_label = ttk.Label(body, textvariable=self.status_var, justify="left")
        self.status_label.grid(row=7, column=0, sticky="ew")
        body.bind("<Configure>", self._wrap_status, add="+")

        # Row 8: the buttons.
        self._build_buttons(body)
        root.bind("<Control-o>", lambda event: self.browse())
        root.bind("<<ThemeChanged>>", self._on_theme_changed, add="+")

    def _build_zone(self, area: ttk.Frame, sun_valley: bool) -> None:
        if sun_valley:
            zone = ttk.Frame(area, style="Card.TFrame", padding=24)
        else:
            zone = ttk.Frame(area, padding=24, borderwidth=2, relief="groove")
        zone.grid(row=0, column=0, sticky="nsew")
        zone.columnconfigure(0, weight=1)
        zone.rowconfigure(0, weight=1)
        zone.rowconfigure(5, weight=1)
        self.zone = zone
        self.zone_label = ttk.Label(zone, text=DROP_TEXT, font=self._big_font, anchor="center")
        self.zone_label.grid(row=1, column=0, pady=(0, 8))
        self.or_label = ttk.Label(zone, text="or")
        self.or_label.grid(row=2, column=0, pady=(0, 8))
        buttons = ttk.Frame(zone)
        buttons.grid(row=3, column=0)
        # Other themes show the style Accent.TButton as a normal button.
        self.browse_button = ttk.Button(
            buttons, text="Add files...", style="Accent.TButton", command=self.browse
        )
        self.browse_button.grid(row=0, column=0, padx=(0, 8))
        self.folder_button = ttk.Button(buttons, text="Add folder...", command=self.browse_folder)
        self.folder_button.grid(row=0, column=1)
        self.hint_label = ttk.Label(zone, text=FOLDER_HINT)
        self.hint_label.grid(row=4, column=0, pady=(12, 0))

    def _build_list(self, area: ttk.Frame) -> None:
        frame = ttk.Frame(area)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.list_frame = frame
        head = ttk.Frame(frame, padding=(0, 0, 0, 6))
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.count_var = tk.StringVar(self.root, value="")
        # A width of one character: _fit_count_line() makes the text fit the free width.
        self.count_label = ttk.Label(
            head, textvariable=self.count_var, font=self._strong_font, width=1
        )
        self.count_label.grid(row=0, column=0, sticky="ew", padx=(0, COUNT_GAP))
        self._count_line: tuple[str, Path | None] = ("", None)
        """The count line, and the folder at its end that can get "…"."""
        for sequence in ("<Configure>", "<Map>"):
            head.bind(sequence, self._fit_count_line, add="+")
        self.list_browse_button = ttk.Button(head, text="Add files...", command=self.browse)
        self.list_browse_button.grid(row=0, column=1, padx=(0, 6))
        self.list_folder_button = ttk.Button(head, text="Add folder...", command=self.browse_folder)
        self.list_folder_button.grid(row=0, column=2)
        tree = ttk.Treeview(
            frame,
            columns=("name", "folder", "status"),
            show="headings",
            selectmode="extended",
            # The minimum size of the window keeps 5 rows, also with a note and the details.
            height=5,
        )
        tree.heading("name", text="Name", anchor="w")
        tree.heading("folder", text="Folder", anchor="w")
        tree.heading("status", text="Status", anchor="w")
        width = round(STATUS_WIDTH * self._scale)
        # Name and Folder share the free width. These widths only set the minimum size.
        narrow = round(150 * self._scale)
        tree.column("name", width=narrow, minwidth=round(80 * self._scale), stretch=True)
        tree.column("folder", width=narrow, minwidth=round(80 * self._scale), stretch=True)
        tree.column("status", width=width, minwidth=width, stretch=False)
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.grid(row=1, column=0, sticky="nsew")
        bar.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        self.tree = tree
        self._columns: tuple[str, ...] = ("name", "folder", "status")
        self._heading = "Folder"
        """The full heading of the Folder column. _fit_heading() shortens the folder in it."""
        self.scrollbar = bar
        # The tree sets the widths of the columns when it draws, after this event.
        for sequence in ("<Configure>", "<Map>"):
            tree.bind(sequence, lambda event: self.root.after_idle(self._fit_heading), add="+")
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Return>", lambda event: self.activate())
        tree.bind("<Delete>", lambda event: self.remove_selected())
        tree.bind("<Control-a>", self._select_all)
        tree.bind("<Escape>", lambda event: self.follow_again())
        tree.bind("<Button-3>", self._on_right_click)
        tree.bind("<Shift-F10>", self._on_menu_key)
        for key in ("<App>", "<Menu>"):  # the Menu key on Windows and on X11
            with contextlib.suppress(tk.TclError):
                tree.bind(key, self._on_menu_key)
        # One context menu for the life of the window. context_menu() fills it again.
        self._context_menu = tk.Menu(self.root, tearoff=False)
        # A click, a scroll or a move of the selection stops the follow of the current row.
        for sequence in (
            "<Button-1>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<Up>",
            "<Down>",
            "<Prior>",
            "<Next>",
            "<Home>",
            "<End>",
        ):
            tree.bind(sequence, self._stop_follow, add="+")
        bar.bind("<Button-1>", self._stop_follow, add="+")
        frame.grid_remove()

    def _build_note(self, body: ttk.Frame) -> None:
        note = ttk.Frame(body, style="Note.TFrame", padding=(10, 4))
        note.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        note.columnconfigure(0, weight=1)
        self.note_var = tk.StringVar(self.root, value="")
        # A classic label: a ttk label of sv-ttk draws the theme color behind the text.
        self.note_label = tk.Label(
            note, textvariable=self.note_var, justify="left", anchor="w", bd=0
        )
        if theme_name(self.root) in SUN_VALLEY:
            self.note_label.configure(font="SunValleyBodyFont")
        self.note_label.grid(row=0, column=0, sticky="w")
        self.note_button = ttk.Button(note, text="", command=self.run_note_action)
        self.note_button.grid(row=0, column=1, padx=(8, 0))
        self.note_close = ttk.Button(
            note, text="✕", width=2, style="Toolbutton", command=lambda: self.set_note("")
        )
        self.note_close.grid(row=0, column=2, padx=(4, 0))
        note.bind("<Configure>", self._wrap_note, add="+")
        self.note = note
        note.grid_remove()

    def _build_buttons(self, body: ttk.Frame) -> None:
        bar = ttk.Frame(body, padding=(0, 12, 0, 0))
        bar.grid(row=8, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)
        self.buttons = bar
        self.stop_button = ttk.Button(bar, text="Stop", command=self._left_action)
        self.stop_button.grid(row=0, column=0, padx=(0, 6))
        self.retry_button = ttk.Button(bar, text="Retry failed", command=self.retry_failed)
        self.retry_button.grid(row=0, column=1, padx=(0, 6))
        self.clear_button = ttk.Button(bar, text="Clear list", command=self.clear_list)
        self.clear_button.grid(row=0, column=2, padx=(0, 6))
        self.folders_button = ttk.Menubutton(bar, text="Show folders")
        self.folders_menu = tk.Menu(
            self.folders_button, tearoff=False, postcommand=self._fill_folders_menu
        )
        self.folders_button.configure(menu=self.folders_menu)
        self.folders_button.grid(row=0, column=4, padx=(6, 0))
        self.folder_open_button = ttk.Button(bar, text="Show folder", command=self.show_folder)
        self.folder_open_button.grid(row=0, column=5, padx=(6, 0))
        self.show_button = ttk.Button(bar, text="Show in folder", command=self.show_output)
        self.show_button.grid(row=0, column=6, padx=(6, 0))
        self.open_button = ttk.Button(bar, text="Open", command=self.open_output)
        self.open_button.grid(row=0, column=7, padx=(6, 0))
        self.open_button.bind("<Return>", lambda event: self.open_button.invoke())
        for button in (self.stop_button, self.retry_button, self.clear_button):
            button.grid_remove()
        self.folders_button.grid_remove()
        self.folder_open_button.grid_remove()

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
        toggle.grid(row=0, column=1, sticky="e")
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
        """Let the style set the text color of "Save to" and of the option boxes.

        On each theme change, sv-ttk calls tk_setPalette. tk_setPalette gives each widget
        a fixed text color. Then the text does not change to gray when the widget is
        disabled, and the user cannot see that "Save to" and the options are locked
        during a run.
        ttk::ThemeChanged sends the event to the root first. Thus tk_setPalette is complete
        when a widget gets the event.
        """
        event.widget.configure(foreground="")

    def _on_theme_changed(self, event: tk.Event) -> None:
        # The root gets the event for each widget. sv-ttk runs tk_setPalette after this
        # handler, and tk_setPalette resets the colors of the classic note label.
        if event.widget is self.root:
            self.root.after_idle(self._apply_colors)

    def mode(self) -> str:
        """Return "dark" for the dark Sun Valley theme. Else return "light"."""
        return "dark" if theme_name(self.root) == SUN_VALLEY[1] else "light"

    def _apply_colors(self) -> None:
        """Set the colors that the theme does not set: the rows, the note and the errors."""
        with contextlib.suppress(tk.TclError):  # the window can be gone
            mode = self.mode()
            colors = ROW_COLORS[mode]
            for tag in ("failed", "converting", "muted"):
                self.tree.tag_configure(tag, foreground=colors[tag])
            background, foreground = NOTE_COLORS[mode]
            _configure_style(self.root, "Note.TFrame", "background", background)
            self.note_label.configure(bg=background, fg=foreground)
            if theme_name(self.root) in SUN_VALLEY:
                # sv-ttk sets the row height from the font size before the scale.
                font = tkfont.nametofont("SunValleyBodyFont", root=self.root)
                height = font.metrics("linespace") + 8
                _configure_style(self.root, "Treeview", "rowheight", height)
            self.status_label.configure(foreground=colors["failed"] if self._status_failed else "")

    def fit_min_size(self) -> None:
        """Make the minimum size of the window large enough for all the widgets.

        The size that the widgets need changes with the theme, the fonts, and the screen
        resolution. A smaller window cuts off a part of the options row. The size includes
        the list, the note bar and the buttons after a stopped run, so the list keeps some
        rows at the minimum size.

        The button row can show all its buttons at the same time, for example when the
        list has waiting rows, failed rows, two output folders and a selected row. Thus
        the size includes each button. The app shows "Show folder" only when it does not
        show the wider "Show folders (N)". Thus the size does not include "Show folder".

        The texts of the buttons and of the progress line have counts. The size uses
        samples with large counts. For larger counts, _fit_min_width() makes the minimum
        width larger when the texts change.
        """
        samples = {
            self.stop_button: "Convert 99,999",
            self.retry_button: "Retry failed (99,999)",
            self.folders_button: "Show folders (999)",
            self.failed_only_button: "Failed only (99,999)",
        }
        texts = {button: button.cget("text") for button in samples}
        for button, text in samples.items():
            button.configure(text=text)
        title = self.title_var.get()
        self.title_var.set(SUMMARY_SAMPLE)
        widgets = (
            self.list_frame,
            self.note,
            self.note_button,
            self.path_label,
            self.status_label,
            *samples,
            self.clear_button,
            self.show_button,
            self.open_button,
        )
        hidden = [widget for widget in widgets if not widget.winfo_manager()]
        for widget in hidden:
            widget.grid()
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        for widget in hidden:
            widget.grid_remove()
        for button, text in texts.items():
            button.configure(text=text)
        self.title_var.set(title)
        self._min_size = (max(MIN_WIDTH, width), max(MIN_HEIGHT, height))
        self.root.minsize(*self._min_size)

    def _fit_min_width(self) -> None:
        """Make the minimum width larger when the buttons or the progress line need it.

        Counts larger than the samples of fit_min_size() make these texts wider. The app
        calls this after the texts change. It waits until Tk measured the new texts.

        During a run, the progress line can name the file, for example "File 1 of 1:
        <a long name>". A long name must not make the window wider, so the app uses the
        progress line only after the run, for the summary.
        """
        if self._min_width_id is None:
            self._min_width_id = self.root.after_idle(self._check_min_width)

    def _check_min_width(self) -> None:
        self._min_width_id = None
        with contextlib.suppress(tk.TclError):  # the window can be gone
            rows = [self.buttons]
            if not self.running:
                rows.append(self.title_label.master)
            need = max(row.winfo_reqwidth() for row in rows) + 2 * BODY_PADDING
            width = max(self._min_size[0], need)
            if self.root.minsize()[0] != width:
                self.root.minsize(width, self._min_size[1])

    def _label_font(self) -> Any:
        return style_font(self.root, "TLabel")

    def _wrap_status(self, event: tk.Event) -> None:
        width = max(event.width - 2 * BODY_PADDING, 100)
        self.status_label.configure(wraplength=width)
        if width != self._line_width:
            self._line_width = width
            if self.list_mode:
                self._refresh_details()

    def _wrap_note(self, event: tk.Event) -> None:
        extra = self.note_button.winfo_reqwidth() if self.note_button.winfo_manager() else 0
        self.note_label.configure(wraplength=max(event.width - extra - 80, 100))

    def _register_drop(self, dnd_type: str) -> bool:
        # On Windows, each child window needs its own registration.
        try:
            for widget in (self.body, self.zone, self.list_frame, self.tree, self.note):
                widget.drop_target_register(dnd_type)
                widget.dnd_bind("<<Drop>>", self.on_drop)
        except tk.TclError:
            return False
        return True

    # State

    @property
    def running(self) -> bool:
        """True from the start of the worker until the window reads its last message."""
        return self.worker is not None

    @property
    def busy(self) -> bool:
        """True while a run is active or a modal dialog is open."""
        return self.worker is not None or self._dialogs > 0

    def _modal[T](self, dialog: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Show a modal dialog. The app refuses drops until the dialog closes.

        A dialog runs a nested event loop, so events still come in. The app refuses a
        drop in that time, so that a second drop cannot change the list in the middle of
        the code that opened the dialog.

        The dialog waits for the user. Thus the window shows the normal cursor, also when
        the code that opens the dialog shows the "watch" cursor. After the dialog, the
        cursor that was there before comes back.
        """
        cursor = ""
        with contextlib.suppress(tk.TclError):
            cursor = self.root.cget("cursor")
            self.root.configure(cursor="")
        self._dialogs += 1
        try:
            return dialog(*args, **kwargs)
        finally:
            self._dialogs -= 1
            with contextlib.suppress(tk.TclError):  # the window can be gone
                self.root.configure(cursor=cursor)

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

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _selected_rows(self) -> list[Row]:
        rows = []
        for iid in self.tree.selection():
            row = self.rows.get(int(iid))
            if row is not None:
                rows.append(row)
        return rows

    # Rows

    def _render(self, row: Row) -> None:
        self.tree.item(
            str(row.id),
            values=(
                row.src.name,
                self.labels.get(path_key(row.src.parent), ""),
                self._status_cell(row, status_text(row)),
            ),
            tags=(STATE_TAGS[row.state],),
        )

    def _status_cell(self, row: Row, text: str) -> str:
        """Return ``text``, the status of ``row``, shortened to fit the Status cell.

        A cell cuts a long text without "…". A waiting row with a tagged output gets "…"
        in the middle of the output name, so that the end, for example "png).xlsx", tells
        two rows apart. The details line shows the full name.
        """
        tagged = row.state == WAITING and text == f"{WAITING_AS}{row.dst.name}"
        fit = self._status_fit or self._new_status_fit()
        key = (text, tagged)
        cell = fit.cells.get(key)
        if cell is None:
            if tagged:
                cell = fit_text(fit.font, fit.room, row.dst.name, middle_cut, prefix=WAITING_AS)
            else:
                cell = fit_text(fit.font, fit.room, text, end_ellipsis)
            fit.cells[key] = cell
        return cell

    def _new_status_fit(self) -> _StatusFit:
        font = style_font(self.root, "Treeview")
        room = int(self.tree.column("status", "width")) - round(STATUS_PADDING * self._scale)
        return _StatusFit(font, room)

    @contextlib.contextmanager
    def _status_batch(self) -> Iterator[None]:
        """Fit the Status cells of a batch of rows with one font and one width.

        Each measure of a text takes about 20 us. Most rows of a batch have the same
        status, for example "Waiting". Thus in a batch, the app measures each text only
        one time. The code in a batch must not show a dialog. During a dialog, the user
        can change the width of the column.
        """
        if self._status_fit is not None:  # a batch in a batch
            yield
            return
        self._status_fit = self._new_status_fit()
        try:
            yield
        finally:
            self._status_fit = None

    def _count(self, row: Row, old: str | None, new: str | None) -> None:
        """Count a change of the state of ``row`` in the tallies and the done folders."""
        if old == new:
            return
        self.tally.add(old, new)
        if row.id in self.run_ids:
            self.run_tally.add(old, new)
        if old == DONE:
            self.done_ids.pop(row.id, None)
            key = path_key(row.dst.parent)
            entry = self.done_folders.get(key)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self.done_folders[key]
        if new == DONE:
            self.done_ids[row.id] = None
            entry = self.done_folders.setdefault(path_key(row.dst.parent), [row.dst.parent, 0])
            entry[1] += 1

    def _insert_row(self, row: Row) -> None:
        self.rows[row.id] = row
        self._count(row, None, row.state)
        self.tree.insert("", "end", iid=str(row.id))
        self._render(row)

    def _update_row(self, new: Row, *, to_end: bool = False) -> None:
        """Put ``new`` in the place of the row with its id, and show it."""
        old = self.rows.pop(new.id) if to_end else self.rows[new.id]
        # Count the old state with the old output folder, and the new with the new.
        self._count(old, old.state, None)
        self.rows[new.id] = new
        self._count(new, None, new.state)
        if to_end:
            self.tree.move(str(new.id), "", "end")
        if new.src != old.src:
            self._render(new)
            return
        # Tk draws the whole list again after each change, and that is slow. Thus write
        # only the Status cell and the tag, and only when they change.
        iid = str(new.id)
        status = status_text(new)
        if status != status_text(old):
            self.tree.set(iid, "status", self._status_cell(new, status))
        tag = STATE_TAGS[new.state]
        if tag != STATE_TAGS[old.state]:
            self.tree.item(iid, tags=(tag,))

    def _remove_row(self, row: Row) -> None:
        del self.rows[row.id]
        self._count(row, row.state, None)
        self.run_ids.discard(row.id)
        self.jobs_by_id.pop(row.id, None)

    # Drops and dialogs

    def on_drop(self, event: Any) -> str:
        """Handle a drop of files and folders. Return the drop action for tkdnd."""
        if self.closing:
            return "refuse_drop"
        if self._dialogs:
            self.set_note(DIALOG_DROP_TEXT)
            return "refuse_drop"
        paths = [Path(item) for item in self.root.tk.splitlist(event.data)]
        # Return to tkdnd at once: the file manager waits until the drop handler ends.
        self.root.after_idle(self.add_paths, paths)
        return "copy"

    def browse(self) -> None:
        """Let the user choose images, then add them to the list."""
        if self.closing or self._dialogs:
            return
        names = self._modal(
            self.ask_files, parent=self.root, title="Add images", filetypes=image_types()
        )
        if self._close_if_asked():
            return
        if names:
            self.add_paths([Path(name) for name in names])

    def browse_folder(self) -> None:
        """Let the user choose a folder, then add the images in it to the list."""
        if self.closing or self._dialogs:
            return
        name = self._modal(
            self.ask_folder, parent=self.root, title="Add a folder of images", mustexist=True
        )
        if self._close_if_asked():
            return
        if name:
            self.add_paths([Path(name)])

    def _ask_replace(self, question: ReplaceQuestion) -> str:
        """Show the replace dialog. Return "replace", "skip" or "cancel"."""
        dialog = ReplaceDialog(
            self.root,
            question,
            heading_font=self._big_font,
            strong_font=self._strong_font,
            dark=self.mode() == "dark",
        )
        return dialog.run()

    @contextlib.contextmanager
    def _watch_cursor(self) -> Iterator[None]:
        """Show the "watch" cursor while the app reads the disk on the window thread."""
        try:
            self.root.configure(cursor="watch")
        except tk.TclError:
            yield
            return
        try:
            yield
        finally:
            with contextlib.suppress(tk.TclError):
                self.root.configure(cursor="")

    def _run_options(self) -> Options | None:
        """Return the options for new rows, or None after an error dialog.

        During a run, the options are locked, and new rows use the options of the run.
        """
        if self.running and self.options is not None:
            return self.options
        try:
            return parse_options(
                self.max_side_var.get(), self.background_var.get(), self.compact_var.get()
            )
        except ValueError as e:
            self._modal(self.show_error, TITLE, str(e), parent=self.root)
            self._close_if_asked()
            return None

    def add_paths(
        self, paths: Sequence[Path], *, recursive: bool = False, include: bool = False
    ) -> None:
        """Add the images in the dropped ``paths`` to the list, and convert them.

        ``paths`` holds files and folders. With ``recursive``, the app adds the images of
        the subfolders too. ``include`` is for the Include subfolders action: the images
        that are in the list already do not count as duplicates, and they do not convert
        again.
        """
        if self.closing:
            return
        if self._dialogs:
            self.set_note(DIALOG_DROP_TEXT)
            return
        # A path from the command line can be relative. The rows keep the full path.
        paths = [absolute(path) for path in paths]
        with self._watch_cursor():
            if include:
                known = {path_key(row.src) for row in self.rows.values()}
            else:
                known = {
                    path_key(row.src)
                    for row in self.rows.values()
                    if row.state not in FINISHED_STATES
                }
            result = expand_drop(paths, known=known, recursive=recursive)
            if include:
                # The note of the first drop named the files in the folders already.
                folders = {path_key(path) for path in paths}
                result.duplicates = 0
                result.ignored = [p for p in result.ignored if path_key(p.parent) not in folders]
            notes = [drop_note(result)]
            if not result.images:
                self._show_drop_notes(notes, result)
                return
            during_run = self.running
            stopping = self._stopping()
            added = self._add_images(result.images)
        if added is None:
            return
        if added and self.list_mode:
            self.tree.focus_set()
        if added and stopping:
            wait = "It waits" if added == 1 else "They wait"
            notes.insert(
                0, f"Added {_plural(added, 'image')} to the list. {wait}, because the run stops."
            )
        elif added and during_run and not include:
            notes.insert(0, f"Added {_plural(added, 'image')} to the end of the list.")
        self._show_drop_notes(notes, result)

    def _show_drop_notes(self, notes: list[str], result: DropResult) -> None:
        subfolders = subfolder_note(result)
        text = " ".join(note for note in [*notes, subfolders] if note)
        if subfolders:
            folders = list(result.skipped_subfolders)
            self.set_note(
                text,
                "Include subfolders",
                lambda: self.add_paths(folders, recursive=True, include=True),
            )
        elif text:
            self.set_note(text)

    def _add_images(self, images: Sequence[Path]) -> int | None:
        """Add rows for ``images``, ask about outputs that exist, and queue the rows.

        An image that is in a finished row goes back to the end of the queue, without a
        second row. It gets an output with the current "Save to", as with Retry. Return
        the number of rows that the app added or queued again. Return None when the user
        clicked Cancel, or when the options have an error.
        """
        by_key = {path_key(row.src): row for row in self.rows.values()}
        fresh: list[Path] = []
        again: list[Row] = []
        for path in images:
            row = by_key.get(path_key(path))
            if row is None:
                fresh.append(path)
            elif row.state in FINISHED_STATES:
                again.append(row)
        again = self._current_outputs(again)
        again_ids = {row.id for row in again}
        taken = {output_key(row.dst) for row in self.rows.values() if row.id not in again_ids}
        taken |= {output_key(row.dst) for row in again}
        outputs = assign_outputs(fresh, self.out_dir, taken)
        new_rows = [Row(self._next_id(), src, dst) for src, dst in zip(fresh, outputs, strict=True)]
        # The worker can end while the question is open. The rows wait all the same.
        wait = self._stopping()
        earlier = self._waiting_rows()
        decision = self._decide([*earlier, *new_rows, *again])
        if decision is None:
            return None
        options = self._run_options()
        if options is None:
            return None
        if self.failed_only:
            self._set_failed_only(False)
        with self._status_batch():
            queued = self._apply(earlier, decision)
            for row in new_rows:
                new = decision(row)
                self._insert_row(new)
                queued.append(new)
            queued += self._apply(again, decision, to_end=True)
        self._refresh_list()
        self._enqueue(queued, options, wait=wait)
        return len(queued)

    def _decide(
        self,
        rows: Sequence[Row],
        *,
        replace_ids: Collection[int] = (),
        cancel_note: str = CANCELLED_TEXT,
    ) -> Callable[[Row], Row] | None:
        """Find the rows that cannot run, and ask about the outputs that exist.

        Return a function that gives the new state of each row. Return None when the
        user clicked Cancel, and show ``cancel_note``. A row whose output path is too long
        fails at once. For the rows in ``replace_ids``, the app replaces the outputs that
        exist without a question.
        """
        problems: dict[int, str] = {}
        for row in rows:
            problem = path_problem(row.dst)
            if problem is None and _same_file(row.src, row.dst):
                problem = "The output file is the input file."
            if problem is not None:
                problems[row.id] = problem
        existing = [
            row
            for row in rows
            if row.id not in problems and row.id not in replace_ids and row.dst.exists()
        ]
        skip: set[int] = set()
        if existing:
            # The rows with a problem fail at once. They are not images that convert.
            others = len(rows) - len(existing) - len(problems)
            question = ReplaceQuestion([row.dst for row in existing], other_count=others)
            answer = self._modal(self.ask_replace, question)
            if self._close_if_asked() or self.closing:
                return None
            if answer == "cancel":
                self.set_note(cancel_note)
                return None
            if answer == "skip":
                skip = {row.id for row in existing}

        def decide(row: Row) -> Row:
            fresh = replace(row, percent=0, size=0, error="", kind="", result=None, failed_dst=None)
            if row.id in problems:
                return replace(fresh, state=FAILED, error=problems[row.id], kind="long")
            if row.id in skip:
                return replace(fresh, state=SKIPPED)
            return replace(fresh, state=WAITING)

        return decide

    def _current_outputs(self, rows: Sequence[Row]) -> list[Row]:
        """Return copies of ``rows`` with outputs for the current "Save to" folder.

        A row keeps its output when the output is in that folder already. Thus a tag such
        as "photo (png).xlsx" stays. The other rows get new outputs. The outputs of the
        other rows in the list stay with those rows.
        """
        ids = {row.id for row in rows}
        taken = {output_key(row.dst) for row in self.rows.values() if row.id not in ids}
        result = list(rows)
        move: list[int] = []
        for index, row in enumerate(rows):
            folder = self.out_dir or row.src.parent
            key = output_key(row.dst)
            if path_key(row.dst.parent) == path_key(folder) and key not in taken:
                taken.add(key)
            else:
                move.append(index)
        outputs = assign_outputs([rows[i].src for i in move], self.out_dir, taken)
        for index, dst in zip(move, outputs, strict=True):
            result[index] = replace(rows[index], dst=dst)
        return result

    def _waiting_rows(self) -> list[Row]:
        """Return the rows that wait, when no run is active. A new run takes them too.

        These rows come from a run that the user stopped. Their outputs can have
        changed with "Save to", and new files can exist. Thus the app checks them with
        :meth:`_decide` again before the new run.
        """
        if self.running:
            return []
        return [row for row in self.rows.values() if row.state == WAITING]

    def _apply(
        self, rows: Iterable[Row], decision: Callable[[Row], Row], *, to_end: bool = False
    ) -> list[Row]:
        """Give each row of ``rows`` its new state from ``decision``. Return the new rows.

        A row keeps the output of ``rows``. The function ignores a row that the user
        removed in the meantime.
        """
        result = []
        with self._status_batch():
            for row in rows:
                current = self.rows.get(row.id)
                if current is None:
                    continue
                new = decision(replace(current, dst=row.dst))
                self._update_row(new, to_end=to_end)
                result.append(new)
        return result

    # "Save to"

    def _save_values(self) -> list[str]:
        values = [NEXT_TO_IMAGE]
        if self.out_dir is not None:
            values.append(f"In {middle_ellipsis(str(self.out_dir), 40)}")
        values.append(CHOOSE_FOLDER)
        return values

    def _on_save_to(self, event: Any = None) -> None:
        value = self.save_var.get()
        if value == CHOOSE_FOLDER:
            name = self._modal(
                self.ask_folder, parent=self.root, title="Save the .xlsx files in", mustexist=True
            )
            if self._close_if_asked():
                return
            self.set_out_dir(Path(name) if name else self.out_dir)
        elif value == NEXT_TO_IMAGE:
            self.set_out_dir(None)

    def set_out_dir(self, folder: Path | None) -> None:
        """Save the outputs in ``folder``, or next to each image for None.

        The rows that wait, failed or stopped get new outputs. The done rows and the
        skipped rows keep their outputs.
        """
        self.out_dir = folder
        values = self._save_values()
        self.save_box.configure(values=values)
        self.save_var.set(values[1] if folder is not None else values[0])
        targets = [row for row in self.rows.values() if row.state in (WAITING, FAILED, STOPPED)]
        ids = {row.id for row in targets}
        taken = {output_key(row.dst) for row in self.rows.values() if row.id not in ids}
        outputs = assign_outputs([row.src for row in targets], folder, taken)
        with self._status_batch():
            for row, dst in zip(targets, outputs, strict=True):
                if dst != row.dst:
                    self._update_row(replace(row, dst=dst))
        self._refresh_details()

    # The run

    def _stopping(self) -> bool:
        """Return True after Stop, until the worker ends."""
        return self.running and self.cancel_event.is_set()

    def _enqueue(self, rows: Sequence[Row], options: Options, *, wait: bool = False) -> None:
        """Put the waiting rows of ``rows`` into the queue. Start a run if none is active.

        A drop during a run adds its rows to that run. The caller checks each row with
        :meth:`_decide` first. For a new run, ``rows`` must include the rows that wait
        from an earlier run. See :meth:`_waiting_rows`. After Stop, the rows only wait.
        The next run takes them. With ``wait``, the rows also only wait. The caller sets
        it when the user clicked Stop before the question of :meth:`_decide`. The worker
        can end while the question is open, but the user asked the run to stop.
        """
        if wait or self._stopping():
            self._refresh_buttons()
            return
        if not self.running:
            self.options = options
            self.run_ids = set()
            self.run_tally = Tally()
            self._run_started = time.monotonic()
            self._run_seconds = 0.0
            self._run_processed = 0
            self.follow = True
        for row in rows:
            if row.id not in self.run_ids:
                self.run_ids.add(row.id)
                self.run_tally.add(None, row.state)
        jobs = [Job(row.id, row.src, row.dst) for row in rows if row.state == WAITING]
        for job in jobs:
            self.jobs_by_id[job.id] = job
        self.jobs.put(jobs)
        if self.running:
            self._refresh_progress()
            self._refresh_buttons()
        elif jobs:
            self._start_worker()
        else:
            self._end_run(stopped=False)

    def _start_worker(self) -> None:
        # A new message queue for each worker, so that no message of an earlier worker
        # can arrive.
        self.queue = queue.SimpleQueue()
        self.cancel_event = threading.Event()
        self._stopping_text = ""
        self.worker = threading.Thread(
            target=run_queue,
            args=(self.jobs, self.options, self.queue.put, self.cancel_event),
            name="pixheet-convert",
            daemon=True,
        )
        if self.failed_only:
            self._set_failed_only(False)
        self._set_running(True)
        self.worker.start()
        if self._poll_id is None:
            self._poll_id = self.root.after(POLL_MS, self._poll)
        self._refresh_progress()
        self._refresh_details()

    def _left_action(self) -> None:
        if self.running:
            self.cancel()
        else:
            self.convert_waiting()

    def cancel(self) -> None:
        """Stop the run. The current file stops at its next row. The other rows wait."""
        if self.worker is None:
            return
        self.cancel_event.set()
        self.stop_button.state(["disabled"])
        self._stopping_text = "Stopping."
        self._refresh_details()

    def convert_waiting(self) -> None:
        """Start a run with the rows that wait, for example after Stop."""
        if self.running or self.closing or self._dialogs:
            return
        rows = self._waiting_rows()
        if not rows:
            return
        decision = self._decide(rows, cancel_note=NOT_STARTED_TEXT)
        if decision is None:
            return
        options = self._run_options()
        if options is None:
            return
        self._enqueue(self._apply(rows, decision), options)

    def skip_current(self) -> None:
        """Stop the file that converts now. The queue continues with the next file."""
        job = self.jobs_by_id.get(self.current) if self.current is not None else None
        if job is not None:
            job.skip.set()

    def retry_failed(self) -> None:
        """Convert the failed rows and the stopped rows again."""
        self.retry([row for row in self.rows.values() if row.state in (FAILED, STOPPED)])

    def retry(self, rows: Sequence[Row], *, ask: bool = True) -> None:
        """Convert ``rows`` again with the current "Save to" folder.

        The rows get new outputs. With ``ask``, the app asks before it replaces an output
        that exists. Without ``ask``, the rows keep their outputs, and the app replaces
        them. The user chose these rows, for example with Convert again.
        """
        if self.closing or self._dialogs:
            return
        rows = [self.rows[row.id] for row in rows if row.id in self.rows]
        rows = [row for row in rows if row.state in FINISHED_STATES]
        if not rows:
            return
        if ask:
            rows = self._current_outputs(rows)
        # The worker can end while the question is open. The rows wait all the same.
        wait = self._stopping()
        earlier = self._waiting_rows()
        decision = self._decide(
            [*earlier, *rows],
            replace_ids=() if ask else {row.id for row in rows},
            cancel_note=NOT_STARTED_TEXT,
        )
        if decision is None:
            return
        options = self._run_options()
        if options is None:
            return
        if self.failed_only:
            self._set_failed_only(False)
        self._enqueue(
            [*self._apply(earlier, decision), *self._apply(rows, decision)], options, wait=wait
        )

    def retry_selected(self) -> None:
        self.retry([row for row in self._selected_rows() if row.state in (FAILED, STOPPED)])

    def convert_again(self) -> None:
        """Convert the selected skipped rows. Their outputs exist, and the app replaces them."""
        self.retry([row for row in self._selected_rows() if row.state == SKIPPED], ask=False)

    def remove_rows(self, rows: Iterable[Row]) -> None:
        """Remove ``rows`` from the list. The row that converts now stays."""
        removed = [row for row in rows if row.id in self.rows and row.id != self.current]
        if not removed:
            return
        ids = [row.id for row in removed]
        self.jobs.remove(ids)
        for row in removed:
            job = self.jobs_by_id.get(row.id)
            if job is not None:
                job.skip.set()  # the worker can have taken it already
            self._remove_row(row)
        self.tree.delete(*(str(i) for i in ids))
        self._refresh_list()
        if self.running:
            self._refresh_progress()
        elif self.failed_only or self.failed_only_button.winfo_manager():
            # The count on the toggle follows the failed rows. Without failed rows, the
            # filter ends, and the list shows all rows again.
            if self.tally.failed:
                self._show_failed_toggle()
            else:
                self._set_failed_only(False)
        self._refresh_buttons()
        self._refresh_details()

    def remove_selected(self) -> None:
        self.remove_rows(self._selected_rows())

    def remove_done(self) -> None:
        """Remove the done rows from the list."""
        self.remove_rows([self.rows[i] for i in list(self.done_ids)])

    def clear_list(self) -> None:
        """Remove all rows when no run is active. Show the drop zone again."""
        if self.running:
            return
        self.tree.delete(*(str(row_id) for row_id in self.rows))  # also the detached rows
        self.rows.clear()
        self.tally = Tally()
        self.run_ids = set()
        self.run_tally = Tally()
        self.jobs.clear()
        self.jobs_by_id.clear()
        self.done_ids.clear()
        self.done_folders.clear()
        self.failed_only = False
        self.failed_only_var.set(False)
        self.set_note("")
        self.title_var.set("")
        self.progress_var.set(0.0)
        self._refresh_all()

    def close(self) -> None:
        """Close the window. During a run, stop it first and close when the worker ends.

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
        self._stopping_text = "Stopping. The window closes when the current file stops."
        self._refresh_details()

    def dispose(self) -> None:
        """Cancel the timers of the window."""
        for name in ("_poll_id", "_note_id", "_min_width_id"):
            after_id = getattr(self, name)
            if after_id is not None:
                with contextlib.suppress(tk.TclError):
                    self.root.after_cancel(after_id)
                setattr(self, name, None)

    def _set_running(self, running: bool) -> None:
        state = ["disabled"] if running else ["!disabled"]
        for widget in (self.max_side_box, self.background_box, self.compact_check):
            widget.state(state)
        self.save_box.state(["disabled"] if running else ["!disabled", "readonly"])
        if running:
            self.stop_button.state(["!disabled"])
            self.failed_only_button.grid_remove()
            self.eta_label.grid()
        self._refresh_buttons()

    # Messages from the worker

    def _poll(self) -> None:
        """Read the messages of the worker.

        An error in a handler goes to the error dialog. The next poll comes anyway, so
        the app reads Finished, and the window can close. The message that failed is
        already out of the queue, thus the same error does not come again.

        After a poll that read messages, the next poll comes after BUSY_POLL_MS. Thus Tk
        draws the list at most about 4 times each second, also for many small files.
        """
        self._poll_id = None
        messages = self.queue
        read = False
        try:
            while True:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    break
                read = True
                self._handle(message)
                if self.worker is None or self.queue is not messages:
                    break  # the run ended, a new worker started, or the window is gone
        finally:
            if self.worker is not None and self._poll_id is None:
                delay = BUSY_POLL_MS if read else POLL_MS
                self._poll_id = self.root.after(delay, self._poll)

    def _handle(self, message: Message) -> None:
        if isinstance(message, Finished):
            self._finish(message.stopped)
            return
        row = self.rows.get(message.job_id)
        if row is None:
            return  # the user removed the row
        old_state = row.state
        new = apply_message(row, message)
        match message:
            case Started():
                self.current = row.id
                self._current_text = "Reading the image."
            case Progress(_, done, total):
                self._current_text = f"Writing row {done:,} of {total:,}."
            case Converted() | Failed() | Stopped():
                if self.current == row.id:
                    self.current = None
                self.jobs_by_id.pop(row.id, None)
                self._run_processed += 1
                self._run_seconds = time.monotonic() - self._run_started
        self._update_row(new)
        if isinstance(message, Started):
            self._follow_row(row.id)
        self._refresh_progress()
        if new.state != old_state:
            self._refresh_buttons()
        selection = self.tree.selection() if self.list_mode else ()
        if not selection or str(row.id) in selection:
            self._refresh_details()

    def _finish(self, stopped: bool) -> None:
        if self.worker is not None:
            self.worker.join()
            self.worker = None
        if self.closing:
            if not self._dialogs:  # else the window closes after the dialog
                self.dispose()
                with contextlib.suppress(tk.TclError):
                    self.root.destroy()
            return
        # The user can click Stop after the worker sent Finished(False), and a drop can
        # add jobs in that time. Then the run stops too, as the user asked.
        stopped = stopped or (self.cancel_event.is_set() and len(self.jobs) > 0)
        if stopped:
            self.jobs.clear()  # the rows that wait stay in the list
        elif len(self.jobs):
            # The window added jobs while the worker ended. The run continues.
            self._start_worker()
            return
        self.jobs_by_id.clear()
        self.current = None
        self._end_run(stopped)

    def _end_run(self, stopped: bool) -> None:
        self._stopping_text = ""
        tally = self.run_tally
        total = tally.total
        self.title_var.set(summary_line(tally, total, stopped) if total else "")
        self.progress_var.set(tally.finished / total if total and not stopped else 0.0)
        self.eta_var.set("")
        self.root.title(TITLE)
        self._set_running(False)
        if self.list_mode and self.tally.failed:
            self._show_failed_toggle()
            first = next(row for row in self.rows.values() if row.state == FAILED)
            self.tree.selection_set(str(first.id))
            self.tree.see(str(first.id))
            self.tree.focus(str(first.id))
        elif not self.list_mode and self.tally.done:
            self.open_button.focus_set()
        self._refresh_details()

    def _show_failed_toggle(self) -> None:
        self.eta_label.grid_remove()
        self.failed_only_button.configure(text=f"Failed only ({self.tally.failed:,})")
        self.failed_only_button.grid()
        self._fit_min_width()

    def _set_failed_only(self, on: bool) -> None:
        """Show only the failed rows, or all rows in their order."""
        self.failed_only = on
        self.failed_only_var.set(on)
        if on:
            for row in self.rows.values():
                if row.state != FAILED:
                    self.tree.detach(str(row.id))
        else:
            for index, row_id in enumerate(self.rows):
                self.tree.move(str(row_id), "", index)
            if not self.running and not self.tally.failed:
                self.failed_only_button.grid_remove()
                self.eta_label.grid()

    # Views

    def _refresh_all(self) -> None:
        self._refresh_list()
        self._refresh_progress()
        self._refresh_buttons()
        self._refresh_details()

    def _refresh_list(self) -> None:
        """Show the zone or the list, the count line and the Folder column.

        The app calls it when rows come or go, not for each message of the worker.
        """
        count = len(self.rows)
        if count >= 2 and not self.list_mode:
            self.list_mode = True
            self.zone.grid_remove()
            self.list_frame.grid()
        elif count == 0 and self.list_mode:
            self.list_mode = False
            self.list_frame.grid_remove()
            self.zone.grid()
        heading, labels = folder_labels([row.src.parent for row in self.rows.values()])
        if len(labels) == 1:
            folder = next(iter(self.rows.values())).src.parent
            self._count_line = (f"{_plural(count, 'image')} in ", folder)
            columns: tuple[str, ...] = ("name", "status")
        else:
            self._count_line = (f"{_plural(count, 'image')} from {len(labels):,} folders", None)
            columns = ("name", "folder", "status")
        self._fit_count_line()
        self._heading = heading
        if tuple(self.tree.cget("displaycolumns")) != columns:
            self.tree.configure(displaycolumns=columns)
            self._fit_columns(columns)
        self._fit_heading()
        if labels != self.labels:
            self.labels = labels
            for row in self.rows.values():
                self.tree.set(str(row.id), "folder", labels.get(path_key(row.src.parent), ""))

    def _fit_count_line(self, event: Any = None) -> None:
        """Show the count line. A folder in it gets "…" so that it ends before the buttons."""
        text, folder = self._count_line
        if folder is not None:
            head = self.count_label.master
            if head.winfo_width() > 1:
                buttons = self.list_browse_button, self.list_folder_button
                room = head.winfo_width() - sum(b.winfo_reqwidth() for b in buttons)
                room -= COUNT_GAP + 6  # the space after the label and between the buttons
                text = fit_text(self._strong_font, room, str(folder), prefix=text)
            else:  # the list does not show yet
                text += middle_ellipsis(str(folder), 48)
        _set_if_changed(self.count_var, text)

    def _fit_heading(self, event: Any = None) -> None:
        """Show the heading of the Folder column. The folder in it fits the column."""
        heading = self._heading
        with contextlib.suppress(tk.TclError):  # after a resize, the window can be gone
            if heading.startswith(FOLDER_IN) and heading.endswith(")"):
                where = heading[len(FOLDER_IN) : -1]
                if self.tree.winfo_ismapped():
                    font = style_font(self.root, "Treeview.Heading")
                    room = int(self.tree.column("folder", "width"))
                    room -= round(STATUS_PADDING * self._scale)
                    heading = fit_text(font, room, where, prefix=FOLDER_IN, suffix=")")
                else:  # the list does not show yet
                    heading = f"{FOLDER_IN}{middle_ellipsis(where, 40)})"
            if self.tree.heading("folder", "text") != heading:
                self.tree.heading("folder", text=heading)

    def _fit_columns(self, columns: Sequence[str]) -> None:
        """Give the width of a hidden Folder column to the Name column, and back.

        The sum of the widths stays the same. Thus the window does not get wider.
        """
        total = sum(int(self.tree.column(name, "width")) for name in self._columns)
        self._columns = tuple(columns)
        status = int(self.tree.column("status", "width"))
        free = max(total - status, 2 * round(80 * self._scale))
        if "folder" in columns:
            self.tree.column("name", width=free // 2)
            self.tree.column("folder", width=free - free // 2)
        else:
            self.tree.column("name", width=free)

    def _refresh_progress(self) -> None:
        """Show the progress of the run: the bar, the progress line and the window title."""
        if not self.running:
            return
        tally = self.run_tally
        total = max(tally.total, 1)
        current = self.rows.get(self.current) if self.current is not None else None
        fraction = current.percent / 100 if current is not None else 0.0
        index = min(tally.finished + 1, total)
        name = current.src.name if current is not None else ""
        _set_if_changed(self.title_var, progress_text(index, total, name, tally))
        # Each move of the bar makes Tk draw it again. Thus move it by BAR_STEP or more.
        bar = min(1.0, (tally.finished + fraction) / total)
        shown = self.progress_var.get()
        if abs(bar - shown) >= BAR_STEP or (bar != shown and bar in (0.0, 1.0)):
            self.progress_var.set(bar)
        left = tally.waiting + (1 - fraction if current is not None else 0)
        _set_if_changed(self.eta_var, eta_text(self._run_seconds, self._run_processed, left))
        title = f"{TITLE} - {index:,} of {total:,}"
        if self.root.title() != title:
            self.root.title(title)

    def _open_target(self) -> Row | None:
        """Return the done row that Open and Show in folder use, or None."""
        if self.list_mode:
            rows = self._selected_rows()
            if rows:
                return rows[0] if len(rows) == 1 and rows[0].state == DONE else None
        if len(self.done_ids) == 1:
            return self.rows[next(iter(self.done_ids))]
        return None

    def _refresh_buttons(self) -> None:
        """Show the buttons that fit the state. The app uses only the counts here."""
        running = self.running
        tally = self.tally
        if running:
            self.stop_button.configure(text="Stop", style="TButton")
            self.stop_button.grid()
        elif tally.waiting:
            self.stop_button.configure(text=f"Convert {tally.waiting:,}", style="Accent.TButton")
            self.stop_button.state(["!disabled"])
            self.stop_button.grid()
        else:
            self.stop_button.grid_remove()
        retry = tally.failed + tally.stopped
        if retry and not running:
            self.retry_button.configure(text=f"Retry failed ({retry:,})")
            self.retry_button.grid()
        else:
            self.retry_button.grid_remove()
        if self.list_mode and not running:
            self.clear_button.grid()
        else:
            self.clear_button.grid_remove()
        folders = len(self.done_folders)
        if self.list_mode and folders >= 2:
            self.folders_button.configure(text=f"Show folders ({folders:,})")
            self.folders_button.grid()
        else:
            self.folders_button.grid_remove()
        if self.list_mode and folders == 1:
            self.folder_open_button.grid()
        else:
            self.folder_open_button.grid_remove()
        target = self._open_target()
        state = ["!disabled"] if target is not None else ["disabled"]
        self.show_button.state(state)
        self.open_button.state(state)
        accent = target is not None and not running
        self.open_button.configure(style="Accent.TButton" if accent else "TButton")
        # With one image, the zone shows Stop only during the run, and the two buttons
        # after a done run. The list always shows Open, and Show in folder for a target.
        if self.list_mode:
            show, open_ = target is not None, True
        else:
            show = open_ = not running and tally.done > 0
        for button, visible in ((self.show_button, show), (self.open_button, open_)):
            if visible:
                button.grid()
            else:
                button.grid_remove()
        self._fit_min_width()

    def _path_line(self, src: Path, dst: Path | None = None, extra: str = "") -> str:
        """Return line 1 of the details: the image and its output, on one line.

        The output shows as a name when it is in the folder of the image. A long path
        gets "…" in the middle, so that the line fits the width of the window.
        """
        target = None
        if dst is not None:
            target = dst.name if path_key(dst.parent) == path_key(src.parent) else str(dst)
        font = self._label_font()
        full = len(str(src)) + len(extra) + (len(ARROW) + len(target) if target else 0)
        budget = full
        while True:
            line = self._fit_path_line(str(src), target, extra, budget)
            width = font.measure(line)
            if width <= self._line_width or budget <= 24:
                return line
            # Remove the characters that do not fit, and some more for wide letters.
            over = int((width - self._line_width) / max(width / len(line), 1)) + 2
            budget = max(24, min(budget, len(line)) - over)

    @staticmethod
    def _fit_path_line(source: str, target: str | None, extra: str, budget: int) -> str:
        """Return the line with ``budget`` characters or fewer for the two paths."""
        budget = max(24, budget - len(extra))
        if target is None:
            return middle_ellipsis(source, budget) + extra
        if len(source) + len(ARROW) + len(target) > budget:
            target = middle_ellipsis(target, max(12, budget // 2 - len(ARROW)))
            source = middle_ellipsis(source, max(12, budget - len(ARROW) - len(target)))
        return f"{source}{ARROW}{target}{extra}"

    def _row_details(self, row: Row) -> tuple[str, str, bool]:
        """Return the two lines of the details of ``row``, and True for a failed row."""
        path = self._path_line(row.src, row.dst)
        if row.state == DONE and row.result is not None:
            return path, result_text(row.result), False
        if row.state == FAILED:
            return path, self._fit_lines(lambda limit: self._failed_text(row, limit)), True
        if row.state == CONVERTING:
            return path, self._current_text, False
        return path, "", False

    @staticmethod
    def _failed_text(row: Row, limit: int) -> str:
        """Return line 2 of a failed row, with its paths shortened to ``limit`` characters.

        The paths are in line 1 already. Short paths keep the reason short.
        """

        def short(path: Path) -> Path:
            return Path(middle_ellipsis(str(path.parent), limit)) / path.name

        failed = row.failed_output
        error = row.error
        for path in {row.src, failed}:
            error = error.replace(str(path), str(short(path)))
        return f"Failed: {failure_detail(row.kind, error, short(failed))}"

    def _fit_lines(self, make: Callable[[int], str]) -> str:
        """Return the text of ``make(limit)`` on two lines at most of the details.

        ``make`` gives the text with its paths shortened to ``limit`` characters. The
        result uses the largest limit that fits. When no limit fits, the end of line 2 gets
        "…". "Copy error" gives the full text.
        """
        font, width = self._label_font(), self._line_width
        first = rest = ""
        for limit in LINE_PATH_LIMITS:
            first, rest = take_line(font, width, make(limit))
            second, after = take_line(font, width, rest)
            if not after:
                return f"{first}\n{second}" if second else first
        return f"{first}\n{fit_text(font, width, rest, end_ellipsis)}"

    def _zone_details(self, row: Row | None) -> tuple[str, bool]:
        if row is None:
            return ("" if self.dnd else NO_DND_TEXT), False
        match row.state:
            case "converting":
                return self._current_text, False
            case "done":
                return (summary(row.dst, row.result) if row.result else str(row.dst)), False
            case "failed":
                detail = as_sentence(failure_detail(row.kind, row.error, row.failed_output))
                return f"The app did not convert {row.src.name}.\n{detail}", True
            case "stopped":
                return "The app did not write the current file.", False
            case "skipped":
                return f"{row.dst.name} exists. The app did not replace it.", False
        return "", False

    def _refresh_details(self) -> None:
        """Show the status line of one image, or the two details lines of the list.

        Line 1 names the files. Line 2 tells the result, in red for an error.
        """
        failed = False
        path = text = ""
        if self._stopping_text:
            text = self._stopping_text
        elif not self.list_mode:
            text, failed = self._zone_details(next(iter(self.rows.values()), None))
        else:
            rows = self._selected_rows()
            current = self.rows.get(self.current) if self.current is not None else None
            if len(rows) == 1:
                path, text, failed = self._row_details(rows[0])
            elif rows:
                path = f"{len(rows):,} files selected."
            elif current is not None:
                path = self._path_line(current.src, extra=f"{SEP}{self._current_text.rstrip('.')}")
        self.path_var.set(path)
        self.status_var.set(text)
        for label, value in ((self.path_label, path), (self.status_label, text)):
            if value:
                label.grid()
            else:
                label.grid_remove()
        if failed != self._status_failed:
            self._status_failed = failed
            colors = ROW_COLORS[self.mode()]
            self.status_label.configure(foreground=colors["failed"] if failed else "")

    # Notes

    def set_note(
        self, text: str, action_text: str = "", action: Callable[[], None] | None = None
    ) -> None:
        """Show ``text`` in the note bar. An empty text closes the note bar.

        A note with an action has a button, and it stays until the user clicks the button
        or the close button. A note without an action closes after 10 seconds.
        """
        if self._note_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._note_id)
            self._note_id = None
        self.note_var.set(text)
        self.note_action = action
        if action is not None:
            self.note_button.configure(text=action_text)
            self.note_button.grid()
        else:
            self.note_button.grid_remove()
        if not text:
            self.note.grid_remove()
            return
        self.note.grid()
        if action is None:
            self._note_id = self.root.after(NOTE_MS, self._close_note)

    def _close_note(self) -> None:
        self._note_id = None
        self.set_note("")

    def run_note_action(self) -> None:
        action = self.note_action
        self.set_note("")
        if action is not None:
            action()

    # The list: follow, selection, keys and menus

    def _stop_follow(self, event: Any = None) -> None:
        self.follow = False

    def follow_again(self) -> str:
        """Clear the selection and keep the converting row in view again (Esc)."""
        selection = self.tree.selection()
        if selection:
            self.tree.selection_remove(*selection)
        self.follow = True
        if self.current is not None:
            self._follow_row(self.current)
        return "break"

    def _follow_row(self, row_id: int) -> None:
        """Scroll the list to the converting row, with up to 3 finished rows above it."""
        if not self.follow or not self.list_mode or self.failed_only:
            return
        iid = str(row_id)
        try:
            index = self.tree.index(iid)
        except tk.TclError:
            return
        height = int(ttk.Style(self.root).lookup("Treeview", "rowheight") or 20)
        visible = max(1, self.tree.winfo_height() // height - 1)  # minus the heading
        above = max(0, min(FOLLOW_ABOVE, visible - 2))
        self.tree.yview_moveto(max(0, index - above) / max(len(self.rows), 1))

    def _on_select(self, event: Any = None) -> None:
        if self.tree.selection():
            self.follow = False
        self._refresh_buttons()
        self._refresh_details()

    def _select_all(self, event: Any = None) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def activate(self) -> str:
        """Open the selected done row (Enter). For a failed row, the details show the error."""
        rows = self._selected_rows()
        if len(rows) == 1 and rows[0].state == DONE:
            self.open_output()
        return "break"

    def _on_double_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.activate()

    def _on_right_click(self, event: tk.Event) -> str:
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._popup(self.context_menu(), event.x_root, event.y_root)
        return "break"

    def _on_menu_key(self, event: Any = None) -> str:
        focus = self.tree.focus()
        box = self.tree.bbox(focus) if focus else ""
        x = self.tree.winfo_rootx() + (box[0] + 20 if box else 20)
        y = self.tree.winfo_rooty() + (box[1] + box[3] if box else 20)
        self._popup(self.context_menu(), x, y)
        return "break"

    def _popup(self, menu: tk.Menu, x: int, y: int) -> None:
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def context_menu(self) -> tk.Menu:
        """Return the context menu for the selected rows. The items fit the selection."""
        rows = self._selected_rows()
        states = {row.state for row in rows}
        one_done = len(rows) == 1 and rows[0].state == DONE
        # Fill the same menu each time. A new menu for each click stays in memory, with
        # its Tcl commands. Do not destroy the menu after tk_popup: on X11, tk_popup
        # returns while the menu is still open.
        menu = self._context_menu
        menu.delete(0, "end")

        def add(label: str, command: Callable[[], object], enabled: bool, key: str = "") -> None:
            menu.add_command(
                label=label,
                command=command,
                accelerator=key,
                state="normal" if enabled else "disabled",
            )

        add("Open", self.open_output, one_done, "Enter")
        add("Show in folder", self.show_output, one_done)
        add("Copy full path", self.copy_paths, bool(rows))
        add("Copy error", self.copy_errors, FAILED in states)
        menu.add_separator()
        add("Retry", self.retry_selected, bool(states & {FAILED, STOPPED}))
        add("Convert again", self.convert_again, SKIPPED in states)
        add("Skip this file", self.skip_current, CONVERTING in states)
        add("Remove from list", self.remove_selected, bool(states - {CONVERTING}), "Delete")
        menu.add_separator()
        add("Select failed files", self.select_failed, self.tally.failed > 0)
        add("Remove converted files", self.remove_done, self.tally.done > 0)
        return menu

    def _copy(self, lines: Iterable[str]) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))

    def copy_paths(self) -> None:
        """Copy the full paths of the selected images to the clipboard."""
        self._copy(str(row.src) for row in self._selected_rows())

    def copy_errors(self) -> None:
        """Copy the errors of the selected failed rows to the clipboard."""
        self._copy(
            f"{row.src}: {failure_detail(row.kind, row.error, row.failed_output)}"
            for row in self._selected_rows()
            if row.state == FAILED
        )

    def select_failed(self) -> None:
        failed = [str(row.id) for row in self.rows.values() if row.state == FAILED]
        self.tree.selection_set(failed)
        if failed:
            self.tree.see(failed[0])

    # Open and show

    def open_output(self) -> None:
        """Open the .xlsx file of the selected done row, or of the only done row."""
        row = self._open_target()
        if row is None:
            return
        try:
            open_path(row.dst)
        except OSError as e:
            self._modal(
                self.show_error, TITLE, f"Cannot open the file: {_describe(e)}", parent=self.root
            )
            self._close_if_asked()

    def show_output(self) -> None:
        """Show the .xlsx file of the selected done row in its folder."""
        row = self._open_target()
        if row is not None:
            self._show(lambda: open_folder(row.dst, select=True))

    def show_folder(self, folder: Path | None = None) -> None:
        """Open an output folder. Without ``folder``, open the only output folder."""
        if folder is None:
            if len(self.done_folders) != 1:
                return
            folder = next(iter(self.done_folders.values()))[0]
        self._show(lambda: open_path(folder))

    def _show(self, action: Callable[[], None]) -> None:
        try:
            action()
        except OSError as e:
            self._modal(
                self.show_error, TITLE, f"Cannot open the folder: {_describe(e)}", parent=self.root
            )
            self._close_if_asked()

    def _fill_folders_menu(self) -> None:
        """Make one menu item for each output folder, with its number of files."""
        self.folders_menu.delete(0, "end")
        for folder, count in self.done_folders.values():
            self.folders_menu.add_command(
                label=f"{middle_ellipsis(str(folder), 60)} ({count:,})",
                command=lambda folder=folder: self.show_folder(folder),
            )

    def _report_exception(self, exc: type[BaseException], value: BaseException, tb) -> None:
        """Show an error in a Tk callback in a dialog. A windowed app has no console.

        Only one error dialog opens at a time. The dialog runs a nested event loop, and
        the same error can come again in it, for example in each poll. While the dialog
        is open, the app only writes the next errors to stderr.
        """
        if sys.stderr is not None:
            import traceback

            traceback.print_exception(exc, value, tb)
        if self._error_open:
            return
        self._error_open = True
        try:
            self._modal(
                self.show_error,
                TITLE,
                f"An unexpected error occurred:\n\n{value!r}",
                parent=self.root,
            )
        finally:
            self._error_open = False
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


HIDDEN_ENV = "PIXHEET_HIDDEN_WINDOW"
"""The environment variable of the hidden mode. It is for the build checks and the tests.

With the value "1", the window opens off the screen and does not take the focus. Then
``packaging/check_exe.py`` can start the built app many times, and no window comes up on
the desktop of the person who runs the build. The app works as usual in the hidden mode.
"""
OFF_SCREEN = "+-32000+-32000"
"""The position of a hidden window. It is far from each monitor."""
LSFW_LOCK, LSFW_UNLOCK = 1, 2


def hidden_window(environ: Mapping[str, str] | None = None) -> bool:
    """Return True if the environment variable ``PIXHEET_HIDDEN_WINDOW`` is "1"."""
    return (os.environ if environ is None else environ).get(HIDDEN_ENV, "").strip() == "1"


def _lock_foreground(lock: bool) -> bool:
    """On Windows, stop or allow the changes of the foreground window. Return True if done.

    Tk makes its window the foreground window when the window shows for the first time.
    While the lock is on, Windows refuses this change. The lock stops when the user
    presses Alt or clicks a window, or when the app removes it.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    with contextlib.suppress(AttributeError, OSError):
        user32 = ctypes.windll.user32
        return bool(user32.LockSetForegroundWindow(LSFW_LOCK if lock else LSFW_UNLOCK))
    return False


def hide_window(root: tk.Tk, platform: str | None = None) -> bool:
    """Prepare ``root`` for the hidden mode, before the window shows for the first time.

    The window goes off the screen. The app keeps the transparency that it uses for the
    theme. On Windows, the window also gets no taskbar button, and the app stops the
    change of the foreground window. Return True if the app must remove that lock at the
    end, with ``_lock_foreground(False)``.
    """
    root.wm_geometry(OFF_SCREEN)
    if (platform or sys.platform) != "win32":
        return False
    with contextlib.suppress(tk.TclError):
        root.attributes("-toolwindow", True)
    return _lock_foreground(True)


X11_DIALOGS = {
    "::tk::dialog::file::": "the file dialog (tkfbox.tcl)",
    "::tk::dialog::file::chooseDir::": "the folder dialog (choosedir.tcl)",
    "::tk::MessageBox": "the message box (msgbox.tcl)",
}
"""The Tcl commands of the dialogs that Tk on X11 makes with Tcl scripts."""


def missing_x11_dialogs(call: Callable[..., Any]) -> list[str]:
    """Return a problem for each X11 dialog whose Tcl script Tk cannot find.

    ``call`` is ``root.tk.call``. A trim of the Tcl and Tk files in the built app can
    remove a script by mistake. The smoke test opens no dialog, so this check finds it.
    """
    return [
        f"Tk cannot load {what}."
        for command, what in X11_DIALOGS.items()
        if str(call("auto_load", command)) != "1"
    ]


def smoke_test(app: App) -> list[str]:
    """Prepare ``app`` for a smoke test, and return the list that collects the problems.

    The window closes 200 ms after the start, or later when a run is active, after the
    run. The app does not show dialogs. It records each error message and each failed
    row as a problem, and it skips the images whose output exists. A built app must also
    have the window icon and the Sun Valley theme. It must have drag and drop when tkdnd
    can load (see :func:`pixheet._desktop.dnd_allowed`). On X11, Tk must find the scripts
    of its dialogs.
    """
    problems: list[str] = []
    if getattr(sys, "frozen", False):
        if not app.has_icon:
            problems.append("The window icon is missing.")
        if not app.dnd and dnd_allowed():
            problems.append("Drag and drop is not available.")
        if theme_name(app.root) not in SUN_VALLEY:
            problems.append("The Sun Valley theme is not available.")
        if app.root.tk.call("tk", "windowingsystem") == "x11":
            problems += missing_x11_dialogs(app.root.tk.call)

    def record(title: str, message: str, **kwargs: Any) -> None:
        problems.append(message)

    app.show_error = record
    app.ask_replace = lambda question: "skip"

    def close_when_idle() -> None:
        if app.busy:
            app.root.after(POLL_MS, close_when_idle)
            return
        problems.extend(
            f"{row.src.name}: {failure_detail(row.kind, row.error, row.failed_output)}"
            for row in app.rows.values()
            if row.state == FAILED
        )
        app.close()

    app.root.after(200, close_when_idle)
    return problems


def _run(root: tk.Tk, smoke: bool, files: list[Path]) -> list[str]:
    """Make the app in ``root`` and run it until the window closes. Return the problems."""
    # Load tkdnd into the plain tk.Tk root first, then the theme. The tests use this order.
    dnd_type = enable_dnd(root)
    settings = settings_path()
    follower = follow_theme(root, settings)
    app = App(root, dnd_type=dnd_type, follower=follower, settings=settings)
    problems = smoke_test(app) if smoke else []
    if files:
        root.after_idle(app.add_paths, files)
    if smoke and sys.stdout is not None:
        print(f"pixheet-gui: drag and drop {'on' if app.dnd else 'off'}", flush=True)
        print(f"pixheet-gui: theme {theme_name(root)}", flush=True)
    root.mainloop()
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Open the window. Convert the images and folders in ``argv``, if there are some.

    With ``--smoke``, the window closes when it has nothing to do. Then the exit status is
    1 if the app found a problem. See :func:`smoke_test`. The window uses the theme choice
    in the settings file. Without a choice, it follows the system light or dark mode. The
    environment variable ``PIXHEET_THEME`` can set "system", "light" or "dark" at the
    start. It wins over the settings file. The environment variable
    ``PIXHEET_HIDDEN_WINDOW=1`` keeps the window off the screen, for the build checks and
    the tests. See :func:`hide_window`.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = "--smoke" in args
    files = [Path(arg) for arg in args if arg != "--smoke"]
    if tk is None:
        _error("tkinter is not available. Install a Python that includes Tk.")
        return 1
    _set_dpi_awareness()
    try:
        # On X11, the class name is the WM_CLASS of the window. Desktops use it to group
        # the windows of an app.
        root = tk.Tk(className=TITLE)
    except tk.TclError as e:
        _error(f"cannot open a window: {e}")
        return 1
    # Tk shows the window at the first idle time. Thus hide it at once.
    locked = hidden_window() and hide_window(root)
    try:
        problems = _run(root, smoke, files)
    finally:
        if locked:
            _lock_foreground(False)
    for problem in problems:
        _error(problem.replace("\n", " "))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
