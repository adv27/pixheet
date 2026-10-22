"""Desktop app: drop images on the window to convert each one to an .xlsx file.

Start it with ``pixheet-gui`` or ``python -m pixheet.gui``. The app converts the image
paths that you give as arguments when it starts. The option ``--smoke`` opens the window
and closes it when the app has nothing to do. Use it to test a built app.

Each output file goes next to its image, with the suffix ``.xlsx``. A worker thread does
the conversion, so the window stays responsive. The worker sends messages to a queue,
and the window reads the queue each 50 ms.
"""

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # a Python build without Tk
    tk = None

from pixheet._image import RGB, check_max_side, parse_background
from pixheet.core import DEFAULT_BACKGROUND, DEFAULT_MAX_SIDE, ConvertResult, convert

TITLE = "Pixheet"
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


def _describe(error: Exception) -> str:
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


class App:
    """The main window. Make it on a Tk root, then call ``root.mainloop()``.

    ``dnd_type`` is the result of :func:`enable_dnd`. When it is None, the window has no
    drop zone, and the user must use Browse.
    """

    def __init__(self, root: tk.Tk, *, dnd_type: str | None) -> None:
        self.root = root
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

    # Layout

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.minsize(460, 400)
        self._big_font = tkfont.nametofont("TkDefaultFont").copy()
        size = self._big_font.cget("size")
        self._big_font.configure(size=round(size * 1.6))

        body = ttk.Frame(root, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.body = body

        zone = ttk.Frame(body, padding=24, borderwidth=2, relief="groove")
        zone.grid(row=0, column=0, sticky="nsew")
        zone.columnconfigure(0, weight=1)
        zone.rowconfigure(0, weight=1)
        zone.rowconfigure(3, weight=1)
        self.zone = zone
        self.zone_label = ttk.Label(zone, text=DROP_TEXT, font=self._big_font, anchor="center")
        self.zone_label.grid(row=1, column=0, pady=(0, 8))
        self.or_label = ttk.Label(zone, text="or")
        self.or_label.grid(row=2, column=0, pady=(0, 8))
        self.browse_button = ttk.Button(zone, text="Browse...", command=self.browse)
        self.browse_button.grid(row=3, column=0, sticky="n")

        options = ttk.Frame(body, padding=(0, 12, 0, 0))
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

        self.progress_var = tk.DoubleVar(root, value=0.0)
        self.progressbar = ttk.Progressbar(
            body, mode="determinate", maximum=1.0, variable=self.progress_var
        )
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(12, 4))
        self.title_var = tk.StringVar(root, value="")
        ttk.Label(body, textvariable=self.title_var).grid(row=3, column=0, sticky="w")
        self.status_var = tk.StringVar(root, value="")
        self.status_label = ttk.Label(body, textvariable=self.status_var, justify="left")
        self.status_label.grid(row=4, column=0, sticky="ew")
        body.bind("<Configure>", self._wrap_status, add="+")
        # A note that the worker messages do not replace, for example about a refused drop.
        self.note_var = tk.StringVar(root, value="")
        ttk.Label(body, textvariable=self.note_var).grid(row=5, column=0, sticky="w")

        buttons = ttk.Frame(body, padding=(0, 12, 0, 0))
        buttons.grid(row=6, column=0, sticky="e")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button.grid_remove()
        self.open_button = ttk.Button(buttons, text="Open folder", command=self.open_output)
        self.open_button.grid(row=0, column=1)
        self.open_button.state(["disabled"])

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
    window icon and drag and drop.
    """
    problems: list[str] = []
    if getattr(sys, "frozen", False):
        if not app.has_icon:
            problems.append("The window icon is missing.")
        if not app.dnd:
            problems.append("Drag and drop is not available.")

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
    1 if the app found a problem. See :func:`smoke_test`.
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
    app = App(root, dnd_type=enable_dnd(root))
    problems = smoke_test(app) if smoke else []
    if files:
        root.after_idle(app.start, files)
    if smoke and sys.stdout is not None:
        print(f"pixheet-gui: drag and drop {'on' if app.dnd else 'off'}", flush=True)
    root.mainloop()
    for problem in problems:
        _error(problem.replace("\n", " "))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
