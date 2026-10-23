"""Private. The plan of a batch of images, without Tk.

The desktop app uses these helpers to:

- expand a drop of files and folders into a list of images,
- give each image a unique output path,
- find problems with an output path before the run,
- keep the state of each row of the list, and the counts of the states,
- make the texts of the list, the progress line, the notes and the replace dialog.

The functions do not use Tk. Thus the tests can call them without a window.
"""

import errno
import os
import re
import sys
import threading
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import UnidentifiedImageError

from pixheet.core import ConvertResult

IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".avif", ".ico", ".ppm", ".pgm", ".pbm", ".pnm", ".tga", ".psd",
})  # fmt: skip
"""The suffixes of the files that the app adds from a drop. Lower case, with the dot."""

QUIET_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})
"""Files in a dropped folder that the note about files that are not images does not name."""

SEP = " \u00b7 "
"""The separator between the parts of a status text, for example "Done · 2.0 MB"."""
ELLIPSIS = "\u2026"
DONE_MARK, FAILED_MARK, MUTED_MARK = "\u2713", "\u2715", "\u2013"

LONG_PATH = 259
"""The longest path that Windows opens without the long path setting."""
LONG_NAME = 255
"""The longest file name on Windows, macOS and Linux."""
TEMP_NAME_LENGTH = len(".pixheet-0123456789abcdef.tmp")
"""The length of the name of the temporary file that convert() writes next to the output."""
MAX_NAMED = 3
"""The maximum number of file names in a note."""
REASON_LENGTH = 24
"""The maximum length of the short reason in the Status cell of a failed row."""

WAITING, CONVERTING, DONE, FAILED, SKIPPED, STOPPED = (
    "waiting",
    "converting",
    "done",
    "failed",
    "skipped",
    "stopped",
)
STATES = (WAITING, CONVERTING, DONE, FAILED, SKIPPED, STOPPED)
FINISHED_STATES = frozenset({DONE, FAILED, SKIPPED, STOPPED})


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


# --- Paths ---------------------------------------------------------------------------


def absolute(path: Path | str) -> Path:
    """Return the absolute path, without "." and "..". The function does not read the disk.

    Path.resolve() reads the disk and follows links. That is slow on a network share.
    """
    return Path(os.path.abspath(path))  # noqa: PTH100


def path_key(path: Path | str) -> str:
    """Return a key that is the same for two spellings of one path.

    The key is the absolute path, without "." and "..". On Windows, the case of the
    letters and the type of the slash have no effect.
    """
    return os.path.normcase(absolute(path))


_DIGITS = re.compile(r"([0-9]+)")


def natural_key(name: str) -> tuple:
    """Return a sort key that puts names in the order of File Explorer.

    A run of digits sorts as a number, so "IMG_2" comes before "IMG_10". The case of the
    letters has no effect. Names that have the same key get the order of their text.
    """
    parts = _DIGITS.split(name.casefold())
    # The parts at odd indexes are digits. Thus two keys compare text with text and
    # numbers with numbers.
    key = tuple(int(part) if i % 2 else part for i, part in enumerate(parts))
    return key, name


def is_image(path: Path) -> bool:
    """Return True when the suffix of ``path`` is in :data:`IMAGE_SUFFIXES`."""
    return path.suffix.lower() in IMAGE_SUFFIXES


@dataclass(slots=True)
class DropResult:
    """The images of a drop, and the paths that the app did not add."""

    images: list[Path] = field(default_factory=list)
    """The new images, without duplicates."""
    ignored: list[Path] = field(default_factory=list)
    """The files that are not images, by their suffix."""
    duplicates: int = 0
    """The number of images whose key is in ``known``."""
    missing: int = 0
    """The number of paths that do not exist, and of folders that the app cannot read."""
    skipped_subfolders: dict[Path, int] = field(default_factory=dict)
    """For each dropped folder with subfolders, the number of subfolders not opened."""


def _scan_folder(folder: Path) -> tuple[list[Path], list[Path]]:
    """Return the files and the subfolders in ``folder``. Raise OSError."""
    files: list[Path] = []
    folders: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            (folders if is_dir else files).append(Path(entry.path))
    return files, folders


def expand_drop(
    paths: Sequence[Path],
    *,
    known: Collection[str] = (),
    recursive: bool = False,
    listdir: Callable[[Path], list[Path]] | None = None,
    isdir: Callable[[Path], bool] | None = None,
    isfile: Callable[[Path], bool] | None = None,
) -> DropResult:
    """Return the images of a drop of files and folders.

    - A dropped file with an image suffix is an image. Other dropped files go to
      ``ignored``.
    - A dropped folder gives the images directly in it, in the order of File Explorer.
      Its other files go to ``ignored``, but not the hidden files, the files in
      :data:`QUIET_NAMES` and the .xlsx files. Thus a second drop of a converted folder
      does not show a note about its outputs.
    - Without ``recursive``, the app does not open the subfolders. It counts them in
      ``skipped_subfolders``. With ``recursive``, it adds the images of all subfolders,
      after the images of the folder itself.
    - An image whose key (see :func:`path_key`) is in ``known`` is a duplicate. Give the
      keys of the rows that wait or convert. An image that comes two times in one drop
      is added one time, and it is not a duplicate.

    The dropped paths keep their order. ``listdir``, ``isdir`` and ``isfile`` let the
    tests use a fake disk. ``listdir`` returns the full paths of the items in a folder.
    """
    is_dir = isdir or os.path.isdir
    is_file = isfile or os.path.isfile
    if listdir is None and isdir is None:
        scan = _scan_folder
    else:
        list_items = listdir or (lambda folder: list(Path(folder).iterdir()))

        def scan(folder: Path) -> tuple[list[Path], list[Path]]:
            files: list[Path] = []
            folders: list[Path] = []
            for item in list_items(folder):
                (folders if is_dir(item) else files).append(item)
            return files, folders

    result = DropResult()
    known = set(known)
    seen: set[str] = set()
    visited: set[str] = set()  # the real paths of the folders, against a loop of links

    def add_image(path: Path) -> None:
        key = path_key(path)
        if key in seen:
            return
        seen.add(key)
        if key in known:
            result.duplicates += 1
        else:
            result.images.append(path)

    def add_folder(folder: Path) -> None:
        real = path_key(os.path.realpath(folder))
        if real in visited:
            return
        visited.add(real)
        try:
            files, folders = scan(folder)
        except OSError:
            result.missing += 1
            return
        for path in sorted(files, key=lambda p: natural_key(p.name)):
            if is_image(path):
                add_image(path)
            elif not _quiet(path):
                result.ignored.append(path)
        if not folders:
            return
        if not recursive:  # only a dropped folder comes here
            result.skipped_subfolders[folder] = len(folders)
            return
        for subfolder in sorted(folders, key=lambda p: natural_key(p.name)):
            add_folder(subfolder)

    for path in paths:
        if is_dir(path):
            add_folder(path)
        elif not is_file(path):
            result.missing += 1
        elif is_image(path):
            add_image(path)
        else:
            result.ignored.append(path)
    return result


def _quiet(path: Path) -> bool:
    name = path.name.lower()
    return name in QUIET_NAMES or name.startswith(".") or path.suffix.lower() == ".xlsx"


def _folder_tag(folder: Path) -> str:
    """Return the name of ``folder`` for a tag. A drive gives its letter, a share its name."""
    if folder.name:
        return folder.name
    drive = folder.drive.rstrip(":\\/")
    return drive.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "root"


def assign_outputs(
    sources: Sequence[Path], out_dir: Path | None = None, taken: Collection[str] = ()
) -> list[Path]:
    """Return one output path for each source. Two outputs are never the same.

    Each source wants ``<folder>/<stem>.xlsx``. The folder is ``out_dir``, or the folder
    of the source. When two or more sources want the same output, each of them gets a
    tag. No source keeps the plain name:

    - Same folder, other type: ``photo (png).xlsx`` and ``photo (jpg).xlsx``.
    - Other folders: the folder names, ``photo (b).xlsx`` and ``photo (c).xlsx``.
    - Both differ: ``photo (b, jpg).xlsx``.
    - The same tag again: a number, ``photo (b 2).xlsx``. The numbers follow the order of
      the keys of the source paths.

    ``taken`` holds the keys of the outputs of the other rows in the list. A source that
    wants a taken output gets a tag, and the other row keeps its name. The tag is the
    folder name when the output goes to another folder, else the type. An output is never
    the key of a source in this call, so the app does not write over an input.

    The result does not depend on the order of ``sources``.
    """
    wanted = [(out_dir or src.parent) / f"{src.stem}.xlsx" for src in sources]
    groups: dict[str, list[int]] = {}
    for index, dst in enumerate(wanted):
        groups.setdefault(path_key(dst), []).append(index)
    used = set(taken) | {path_key(src) for src in sources}
    result = list(wanted)
    colliding: list[tuple[str, list[int]]] = []
    for key, members in groups.items():
        if len(members) > 1 or key in used:
            colliding.append((key, members))
        else:
            used.add(key)
    for _key, members in sorted(colliding):
        members.sort(key=lambda i: path_key(sources[i]))
        srcs = [sources[i] for i in members]
        if len(srcs) > 1:
            use_folder = len({path_key(src.parent) for src in srcs}) > 1
            use_type = not use_folder or len({src.suffix.lower() for src in srcs}) > 1
        else:
            use_folder = out_dir is not None and path_key(out_dir) != path_key(srcs[0].parent)
            use_type = not use_folder
        for index, src in zip(members, srcs, strict=True):
            parts = []
            if use_folder:
                parts.append(_folder_tag(src.parent))
            if use_type:
                parts.append(src.suffix.lower().lstrip(".") or "file")
            tag = ", ".join(parts)
            folder = wanted[index].parent
            candidate = folder / f"{src.stem} ({tag}).xlsx"
            number = 2
            while path_key(candidate) in used:
                candidate = folder / f"{src.stem} ({tag} {number}).xlsx"
                number += 1
            used.add(path_key(candidate))
            result[index] = candidate
    return result


def path_problem(dst: Path, limit: int = LONG_PATH, *, platform: str | None = None) -> str | None:
    """Return the reason why the app cannot write ``dst``, or None.

    The function only looks at the length of the path. It does not write to the disk.
    A folder that the app cannot write gives an error in the row when the app writes.

    - On Windows, a path of more than ``limit`` characters fails. convert() first writes
      a temporary file with a name of 29 characters next to the output. Its path counts
      too.
    - On all systems, a file name of more than 255 characters fails.
    """
    name = dst.name
    if len(name) > LONG_NAME:
        return (
            f"The output file name has {len(name):,} characters. The limit is {LONG_NAME}. "
            "Rename the image, then click Retry failed."
        )
    if (platform or sys.platform) != "win32":
        return None
    full = absolute(dst)
    length = len(str(full))
    temp_length = len(str(full.parent / ("x" * TEMP_NAME_LENGTH)))
    if length > limit:
        what = f"The output path has {length:,} characters."
    elif temp_length > limit:
        what = f"The temporary file next to the output needs {temp_length:,} characters."
    else:
        return None
    return (
        f'{what} Windows allows {limit}. Set "Save to" to a short folder, then click Retry failed.'
    )


def common_folder(folders: Sequence[Path]) -> Path | None:
    """Return the deepest folder that holds all ``folders``.

    Return None for no folders, and when the folders are on different drives or shares.
    """
    if not folders:
        return None
    try:
        return Path(os.path.commonpath([absolute(folder) for folder in folders]))
    except ValueError:
        return None


def folder_labels(folders: Sequence[Path]) -> tuple[str, dict[str, str]]:
    r"""Return the heading of the Folder column, and a short label for each folder.

    The keys of the labels are :func:`path_key` of the folders. With a common folder, the
    heading names the folder above it, and each label starts with the name of the common
    folder. For example, ``D:\Photos\2023`` and ``D:\Photos\2024\Da Lat`` give the
    heading ``Folder (in D:\)`` and the labels ``Photos\2023`` and ``Photos\2024\Da Lat``.
    When the common folder is a drive or a share, the labels start below it. Without a
    common folder, the heading is ``Folder`` and the labels are the full paths.
    """
    unique: dict[str, Path] = {}
    for folder in folders:
        unique.setdefault(path_key(folder), absolute(folder))
    root = common_folder(list(unique.values()))
    if root is None:
        return "Folder", {key: str(folder) for key, folder in unique.items()}
    labels: dict[str, str] = {}
    if root == root.parent:  # a drive or a share
        where = str(root)
        for key, folder in unique.items():
            relative = os.path.relpath(folder, root)
            labels[key] = str(root) if relative == os.curdir else relative
    else:
        where = str(root.parent)
        for key, folder in unique.items():
            relative = os.path.relpath(folder, root)
            labels[key] = root.name if relative == os.curdir else str(root.name / Path(relative))
    return f"Folder (in {middle_ellipsis(where, 40)})", labels


def middle_ellipsis(text: str, limit: int) -> str:
    r"""Shorten a path to ``limit`` characters or fewer. Cut it in the middle.

    The result keeps the start, for example ``D:\`` or ``\\server\share\``, and as many
    of the last folders as it can, for example ``D:\…\2024\Da Lat``. When the last name
    alone is too long, the result is ``…`` and the end of the text.
    """
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    sep = "\\" if "\\" in text else "/"
    if text.startswith(sep * 2):  # \\server\share\
        parts = text.split(sep)
        head = sep.join(parts[:4]) + sep
        rest = parts[4:]
    else:
        first, _, after = text.partition(sep)
        head = first + sep
        rest = after.split(sep) if after else []
    tail: list[str] = []
    for part in reversed(rest):
        candidate = sep.join([part, *tail])
        if len(head) + len(ELLIPSIS) + len(sep) + len(candidate) > limit:
            break
        tail.insert(0, part)
    if tail and len(tail) < len(rest):
        return f"{head}{ELLIPSIS}{sep}{sep.join(tail)}"
    return ELLIPSIS + text[-(limit - 1) :]


def group_by_folder(paths: Iterable[Path]) -> list[tuple[Path, list[Path]]]:
    """Group ``paths`` by their folder. The groups keep the order of the first path of each."""
    groups: dict[str, tuple[Path, list[Path]]] = {}
    for path in paths:
        groups.setdefault(path_key(path.parent), (path.parent, []))[1].append(path)
    return list(groups.values())


# --- Errors --------------------------------------------------------------------------

NOT_AN_IMAGE = "the app cannot read this file as an image"


def _in_folder_of(filename: object, dst: Path) -> bool:
    if not isinstance(filename, (str, os.PathLike)):
        return False
    key = path_key(filename)
    folder = path_key(dst.parent)
    return key == folder or path_key(Path(key).parent) == folder


def failure_kind(error: BaseException, dst: Path) -> str:
    """Return the kind of a conversion error: "write", "missing", "image" or "other".

    - "image": Pillow cannot read the source.
    - "write": the system does not let the app write the output or its temporary file.
    - "missing": the folder of the output does not exist, for example a share that went
      away.
    - "other": all other errors, for example a source that does not exist.
    """
    if isinstance(error, UnidentifiedImageError):
        return "image"
    if not isinstance(error, OSError):
        return "other"
    filename = error.filename
    if isinstance(error, PermissionError) or error.errno == errno.EROFS:
        return "write" if filename is None or _in_folder_of(filename, dst) else "other"
    if isinstance(error, FileNotFoundError):
        if filename is None:
            return "other" if dst.parent.is_dir() else "missing"
        return "missing" if _in_folder_of(filename, dst) else "other"
    return "other"


SHORT_REASONS = {
    "write": "cannot write here",
    "missing": "cannot find the folder",
    "image": "not an image",
    "long": "path too long",
}

_ERRNO_PREFIX = re.compile(r"^\[(?:Errno|WinError) -?\d+\] ")


def short_reason(kind: str, text: str) -> str:
    """Return the reason in the Status cell of a failed row, 24 characters or fewer.

    ``kind`` is a result of :func:`failure_kind`, or "long" for a path that is too long.
    For "other", the reason is the start of ``text``, without "[Errno 2]".
    """
    if kind in SHORT_REASONS:
        return SHORT_REASONS[kind]
    text = _ERRNO_PREFIX.sub("", " ".join(text.split())) or "unknown error"
    if len(text) > REASON_LENGTH:
        text = text[: REASON_LENGTH - 1].rstrip() + ELLIPSIS
    return text


# --- Jobs and messages ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class Job:
    """One image to convert, and the file to write.

    ``id`` is the id of the row. Set ``skip`` to stop this job, but not the queue.
    """

    id: int
    src: Path
    dst: Path
    skip: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class Started:
    job_id: int


@dataclass(frozen=True, slots=True)
class Progress:
    job_id: int
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class Converted:
    job_id: int
    result: ConvertResult
    size: int = 0
    """The size of the output file in bytes. The worker reads it after the write."""


@dataclass(frozen=True, slots=True)
class Failed:
    job_id: int
    error: str
    kind: str = "other"
    """The result of :func:`failure_kind`, or "long"."""


@dataclass(frozen=True, slots=True)
class Stopped:
    job_id: int


@dataclass(frozen=True, slots=True)
class Finished:
    stopped: bool


type Message = Started | Progress | Converted | Failed | Stopped | Finished


# --- Rows ----------------------------------------------------------------------------


@dataclass(slots=True)
class Row:
    """One row of the list: an image, its output, and its state."""

    id: int
    src: Path
    dst: Path
    state: str = WAITING
    percent: int = 0
    size: int = 0
    """The size of the output file in bytes, after the conversion."""
    error: str = ""
    kind: str = ""
    result: ConvertResult | None = None
    """What convert() did, for the details line of a done row."""


def format_size(n: int) -> str:
    """Return a file size for people, for example "512 bytes" or "2.0 MB"."""
    if n < 1024:
        return _plural(n, "byte")
    size = n / 1024
    unit = "KB"
    for larger in ("MB", "GB"):
        if size < 1024:
            break
        size /= 1024
        unit = larger
    return f"{size:,.1f} {unit}"


def status_text(row: Row) -> str:
    """Return the text of the Status cell. Each state has a word, not only a color."""
    match row.state:
        case "waiting":
            if row.dst.name != f"{row.src.stem}.xlsx":
                return f"Waiting{SEP}as {row.dst.name}"
            return "Waiting"
        case "converting":
            return f"Converting {row.percent}%"
        case "done":
            return f"{DONE_MARK} Done{SEP}{format_size(row.size)}"
        case "failed":
            return f"{FAILED_MARK} Failed: {short_reason(row.kind, row.error)}"
        case "skipped":
            return f"{MUTED_MARK} Skipped: output exists"
        case "stopped":
            return f"{MUTED_MARK} Stopped"
    return row.state


def apply_message(row: Row, message: Message) -> Row:
    """Return the row after a message of the worker. Do not change ``row``.

    A message for another job, and :class:`Finished`, give the same row.
    """
    match message:
        case Started(job_id) if job_id == row.id:
            return replace(row, state=CONVERTING, percent=0, error="", kind="", result=None)
        case Progress(job_id, done, total) if job_id == row.id and row.state == CONVERTING:
            percent = min(100, done * 100 // total) if total > 0 else 0
            return replace(row, state=CONVERTING, percent=percent)
        case Converted(job_id, result, size) if job_id == row.id:
            return replace(row, state=DONE, percent=100, size=size, result=result)
        case Failed(job_id, error, kind) if job_id == row.id:
            return replace(row, state=FAILED, error=error, kind=kind)
        case Stopped(job_id) if job_id == row.id:
            return replace(row, state=STOPPED, percent=0)
    return row


@dataclass(slots=True)
class Tally:
    """The number of rows in each state. Update it with :meth:`add` for each change.

    Thus the app does not count all rows on each message.
    """

    waiting: int = 0
    converting: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: int = 0

    def add(self, old_state: str | None, new_state: str | None) -> None:
        """Count a row that changes from ``old_state`` to ``new_state``.

        None as ``old_state`` is a new row. None as ``new_state`` is a removed row. Raise
        ValueError for an unknown state and for a count that becomes less than 0.
        """
        if old_state == new_state:
            return
        for state in (old_state, new_state):
            if state is not None and state not in STATES:
                raise ValueError(f"unknown state: {state!r}")
        if old_state is not None:
            count = getattr(self, old_state)
            if count == 0:
                raise ValueError(f"no row is {old_state}")
            setattr(self, old_state, count - 1)
        if new_state is not None:
            setattr(self, new_state, getattr(self, new_state) + 1)

    @property
    def finished(self) -> int:
        """The number of rows that are done, failed, skipped or stopped."""
        return self.done + self.failed + self.skipped + self.stopped

    @property
    def total(self) -> int:
        return self.finished + self.waiting + self.converting


def _counts(tally: Tally, *, done: bool) -> list[str]:
    parts = [f"{tally.done:,} done"] if done else []
    for count, word in ((tally.failed, "failed"), (tally.skipped, "skipped")):
        if count:
            parts.append(f"{count:,} {word}")
    if tally.stopped:
        parts.append(f"{tally.stopped:,} stopped")
    return parts


def progress_text(index: int, total: int, current: str, tally: Tally) -> str:
    """Return the left part of the progress line during a run.

    For one file: "File 1 of 1: IMG_2041.jpg". For more files, the text does not name
    the file, so that the time left has space: "File 25 of 60 · 23 done · 1 failed". The
    text leaves out a count of 0, but not the count of done files.
    """
    if total == 1:
        return f"File {index:,} of 1: {current}" if current else f"File {index:,} of 1"
    return SEP.join([f"File {index:,} of {total:,}", *_counts(tally, done=True)])


def summary_line(tally: Tally, total: int, stopped: bool) -> str:
    """Return the progress line after a run of ``total`` rows.

    For example "Converted 57 of 60 files. 2 failed. 1 skipped.", "Converted 60 files.",
    or "Stopped. Converted 24 of 60 files." The text leaves out a count of 0.
    """
    if tally.done == total and not stopped:
        text = f"Converted {_plural(total, 'file')}."
    else:
        text = f"Converted {tally.done:,} of {_plural(total, 'file')}."
    if stopped:
        text = f"Stopped. {text}"
    rest = _counts(tally, done=False)
    return " ".join([text, *(f"{part}." for part in rest)])


def eta_text(seconds_done: float, files_done: int, files_left: float) -> str:
    """Return the time left, for example "about 40 s left", "about 2 min left".

    The time left is the mean time of the done files, times ``files_left``. The text is
    empty until 3 files are done, and when no file is left. Seconds round to 5 s,
    minutes to 1 min, hours to 1 h.
    """
    if files_done < 3 or files_left <= 0 or seconds_done <= 0:
        return ""
    left = seconds_done / files_done * files_left
    if left < 57.5:
        return f"about {max(5, int(left / 5 + 0.5) * 5)} s left"
    minutes = max(1, int(left / 60 + 0.5))
    if minutes < 60:
        return f"about {minutes} min left"
    return f"about {int(left / 3600 + 0.5):,} h left"


# --- Notes ---------------------------------------------------------------------------


def _names(paths: Sequence[Path]) -> str:
    names = ", ".join(path.name for path in paths[:MAX_NAMED])
    if len(paths) > MAX_NAMED:
        names += f" and {len(paths) - MAX_NAMED:,} more"
    return names


def drop_note(result: DropResult) -> str:
    """Return the note about a drop. Empty when the drop added all its items.

    The note does not tell about the subfolders. See :func:`subfolder_note`.
    """
    sentences = []
    if not result.images and not result.duplicates:
        sentences.append("The drop has no images.")
    if result.ignored:
        count = len(result.ignored)
        what = "file that is not an image" if count == 1 else "files that are not images"
        sentences.append(f"Ignored {count:,} {what}: {_names(result.ignored)}.")
    if result.duplicates:
        verb = "is" if result.duplicates == 1 else "are"
        sentences.append(f"{_plural(result.duplicates, 'image')} {verb} already in the list.")
    if result.missing:
        sentences.append(f"The app cannot find or read {_plural(result.missing, 'dropped item')}.")
    return " ".join(sentences)


def subfolder_note(result: DropResult) -> str:
    """Return the note about the subfolders that the app did not open. Empty for none.

    For example "The app did not open 2 subfolders of Beach."
    """
    folders = result.skipped_subfolders
    if not folders:
        return ""
    count = sum(folders.values())
    if len(folders) == 1:
        (folder,) = folders
        where = _folder_tag(folder)
    else:
        where = f"{len(folders):,} folders"
    return f"The app did not open {_plural(count, 'subfolder')} of {where}."


@dataclass(slots=True)
class ReplaceQuestion:
    """The input of the replace dialog: the outputs that exist, for one drop."""

    existing: list[Path]
    """The output files that exist."""
    other_count: int
    """The number of other images in the drop, which have no output yet."""

    @property
    def folders(self) -> list[tuple[Path, list[Path]]]:
        return group_by_folder(self.existing)

    @property
    def heading(self) -> str:
        if len(self.existing) == 1:
            return f"{self.existing[0].name} exists"
        return f"{len(self.existing):,} output files exist"

    @property
    def text(self) -> str:
        one = len(self.existing) == 1
        parts = []
        if not one:
            folders = len(self.folders)
            parts.append(
                "They are in one folder." if folders == 1 else f"They are in {folders:,} folders."
            )
        them = "it" if one else "them"
        if self.other_count:
            these = "this image" if one else "these images"
            other = "image" if self.other_count == 1 else f"{self.other_count:,}"
            parts.append(
                f"The app can replace {them}. Or it can skip {these} and convert the other {other}."
            )
        else:
            parts.append(f"The app can replace {them}, or stop.")
        return " ".join(parts)

    @property
    def replace_label(self) -> str:
        return "Replace" if len(self.existing) == 1 else "Replace all"

    @property
    def skip_label(self) -> str | None:
        """The text of the Skip button. None when the drop has no other images."""
        if not self.other_count:
            return None
        if len(self.existing) == 1:
            return "Skip this file"
        return f"Skip these {len(self.existing):,}"
