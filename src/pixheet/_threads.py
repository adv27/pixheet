"""Work on several threads: a shared pool, a LANCZOS resize, and DEFLATE for zip members.

Each helper gives the same data as the plain, one-thread call:

- ``resize`` and ``thumbnail`` return the same pixels as ``Image.resize`` and
  ``Image.thumbnail`` with LANCZOS. Pillow resizes in two passes with an 8-bit image
  between them. The horizontal pass makes each row from the same input row, and the
  vertical pass makes each column from the same input column. So threads do each pass
  on strips at whole pixels, in the order that Pillow uses. Pillow releases the GIL
  while it resamples.
- ``ZipFile`` compresses large DEFLATE members in chunks on the pool (the pigz method).
  The members decompress to the same bytes, but the compressed bytes differ. For a
  photo, the file size changes by less than 1 %. A small file of flat colors can
  change by a few percent.

These helpers copy rules from Pillow 13 and use one private zipfile attribute. The tests
in tests/test_threads.py compare each helper with the plain call.
"""

import math
import os
import zipfile
import zlib
from collections import deque
from collections.abc import Callable
lazy from concurrent.futures import Future, ThreadPoolExecutor

from PIL import Image

# The number of worker threads: the CPUs that this process can use, at most 8. With 1,
# every helper does the plain call on the calling thread.
WORKERS = max(1, min(8, os.process_cpu_count() or 1))

_LANCZOS = Image.Resampling.LANCZOS
_MIN_STRIP = 32  # pixels: a pass with thinner strips runs as one plain call
_LARGE = 16_000_000  # pixels: a first pass that copies a larger input uses more strips
_pool: ThreadPoolExecutor | None = None
_pool_pid = 0
# True after the pool refused work (interpreter shutdown, or no thread can start). Then
# every helper does the plain call on the calling thread, as with one worker.
_serial = False


def _threaded() -> bool:
    return WORKERS > 1 and not _serial


def _shared_pool() -> ThreadPoolExecutor:
    """The process-wide pool, made on first use, and made again in a forked child."""
    global _pool, _pool_pid
    pool = _pool
    if pool is None or _pool_pid != os.getpid():
        pool = _pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="pixheet")
        _pool_pid = os.getpid()
    return pool


def _submit[T](fn: Callable[..., T], *args) -> Future[T]:
    """Run ``fn(*args)`` on the pool, or on this thread when the pool takes no new work.

    The pool refuses new work when the interpreter shuts down. submit() also fails when
    the system cannot start a thread, but then the job is already in the queue. In both
    cases this thread runs the job, unless a live worker takes it first. After a refusal,
    the helpers do not use the pool again. The first side that takes a job runs it. The
    job, its data and its result go with it, so a job that stays in the queue holds nothing.
    """
    global _serial
    future: Future[T] = Future()
    job = [(fn, args, future)]

    def run() -> None:
        try:
            f, a, fut = job.pop()  # atomic: only one side gets the job
        except IndexError:
            return  # the other side has the job
        try:
            fut.set_result(f(*a))
        except BaseException as e:
            fut.set_exception(e)

    if _serial:
        run()
        return future
    try:
        _shared_pool().submit(run)
    except RuntimeError:  # shutdown, or "can't start new thread"
        _serial = True
        run()
    return future


def _strips(n: int, parts: int) -> list[tuple[int, int]]:
    """Cut ``range(n)`` into at most ``parts`` strips that are not empty."""
    count = min(parts, n)
    return [(n * i // count, n * (i + 1) // count) for i in range(count)]


def _pass(
    im: Image.Image, size: tuple[int, int], vertical: bool, copy: bool, parts: int
) -> Image.Image:
    """One resize pass, horizontal or vertical, on ``parts`` strips that the pool runs.

    A strip that is a copy (``crop``) resizes fastest. A strip that ``box`` selects
    needs no copy, but Pillow then adds a pass that keeps its size, which takes time.
    """
    w, h = size

    def strip(span: tuple[int, int]) -> Image.Image:
        a, b = span
        box = (a, 0, b, im.height) if vertical else (0, a, im.width, b)
        part = (b - a, h) if vertical else (w, b - a)
        if copy:
            return im.crop(box).resize(part, _LANCZOS)
        return im.resize(part, _LANCZOS, box=box)

    spans = _strips(im.width if vertical else im.height, parts)
    jobs = [_submit(strip, span) for span in spans]
    out = Image.new(im.mode, size)
    for (a, _), job in zip(spans, jobs, strict=True):
        out.paste(job.result(), (a, 0) if vertical else (0, a))
    return out


def resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Return ``im.resize(size, LANCZOS)``, byte for byte, made on the pool."""
    if not _threaded() or im.mode != "RGB" or size == im.size:
        return im.resize(size, _LANCZOS)
    w, h = size
    im.load()
    # Pillow (Resample.c) does the vertical pass first when the height loses more than
    # two times as many pixels as the width. Pillow skips a pass that keeps its side.
    vertical_first = im.height - h > max(0, 2 * (im.width - w))
    out = im
    for vertical in (vertical_first, not vertical_first):
        if (h if vertical else w) == (out.height if vertical else out.width):
            continue
        target = (out.width, h) if vertical else (w, out.height)
        # A box strip reads the input in place, with no copy, but Pillow adds a pass
        # that keeps the size. For a strip of rows, that pass is small. For a strip of
        # columns, it is a full vertical pass, which is slower than one plain pass. So
        # only a first pass over rows uses box strips. That pass does not copy the input.
        # Other passes copy their strips (crop), unless a strip is above Pillow's limit
        # for decompression bombs: crop() checks that limit, and resize() does not.
        cut, other = (out.width, out.height) if vertical else (out.height, out.width)
        largest = math.ceil(cut / min(WORKERS, cut)) * other  # pixels in the largest strip
        limit = Image.MAX_IMAGE_PIXELS
        fits = limit is None or largest <= limit
        # Very thin strips cost more to cut and join than the threads save.
        if (vertical and not fits) or cut < _MIN_STRIP * min(WORKERS, cut):
            out = out.resize(target, _LANCZOS)
            continue
        copy = fits and (vertical or out is not im)
        parts = WORKERS
        if copy and out is im and im.width * im.height > _LARGE:
            # Only the running strips have a copy. With 2 strips for each worker, about
            # half of the input has a copy at one time. A narrow input gets fewer strips,
            # so more of it has a copy. Each strip adds time, so a smaller input does not
            # use more strips, and the count stays at 2 for each worker.
            parts = max(WORKERS, min(2 * WORKERS, cut // _MIN_STRIP))
        out = _pass(out, target, vertical, copy, parts)
    out.info = im.info.copy()  # as im.resize() does
    return out


def _thumbnail_size(w: int, h: int, side: int) -> tuple[int, int]:
    """The size that ``thumbnail((side, side))`` gives a w x h image if side < max(w, h).

    This is Pillow 13's rule. For a larger side, thumbnail() keeps the size.
    """
    aspect = w / h
    if aspect <= 1:  # the height is the longest side
        x = max(
            min(
                math.floor(side * aspect),
                math.ceil(side * aspect),
                key=lambda n: abs(aspect - n / side),
            ),
            1,
        )
        return x, side
    y = max(
        min(
            math.floor(side / aspect),
            math.ceil(side / aspect),
            key=lambda n: 0 if n == 0 else abs(aspect - side / n),
        ),
        1,
    )
    return side, y


def thumbnail(im: Image.Image, side: int) -> Image.Image:
    """Return a copy of ``im`` after ``thumbnail((side, side), LANCZOS)``, byte for byte."""
    w, h = im.size
    size = _thumbnail_size(w, h, side) if side < max(w, h) else im.size
    if size == im.size:
        return im.copy()
    # On a copy, thumbnail() calls resize() with reducing_gap=2.0 (its draft() changes
    # no image in memory). That calls reduce() first when a side shrinks 4 times or more.
    # Then, and with one worker, do that call.
    if not _threaded() or w // size[0] >= 4 or h // size[1] >= 4:
        return im.resize(size, _LANCZOS, reducing_gap=2.0)
    return resize(im, size)


CHUNK = 256 * 1024
_WINDOW = 32 * 1024  # the DEFLATE window: the preset dictionary of the next chunk
_EMPTY_FINAL_BLOCK = b"\x03\x00"  # BFINAL=1, fixed Huffman codes, end of block
_DEFLATE = (zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)  # what zipfile uses
_COMPRESSOR = type(zlib.compressobj())


def _deflate(data: memoryview, zdict: bytes, last: bool) -> bytes:
    c = zlib.compressobj(*_DEFLATE, zdict=zdict) if zdict else zlib.compressobj(*_DEFLATE)
    return c.compress(data) + c.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)


class _ParallelDeflate:
    """Takes the place of zipfile's compressor for one member: compress() and flush().

    Each chunk of about CHUNK bytes has its own compressor. The 32 KiB before the chunk
    is the preset dictionary of that compressor, so a match can refer back across the
    cut. A sync flush ends each chunk on a byte boundary. Usually the last chunk ends
    the stream. If no data waits at flush(), an empty final block ends the stream.
    Together the chunks are one usual raw DEFLATE stream.
    """

    def __init__(self) -> None:
        self._pending: list[bytes] = []
        self._pending_size = 0
        self._zdict = b""  # empty until the first chunk goes to the pool
        self._jobs: deque[Future[bytes]] = deque()

    def compress(self, data: bytes) -> bytes:
        # Keep a copy of a buffer that the caller can change or use again.
        data = data if type(data) is bytes else bytes(data)
        self._pending.append(data)
        self._pending_size += len(data)
        if self._pending_size < CHUNK:
            return b""
        self._submit(last=False)
        return self._collect(wait=False)

    def flush(self) -> bytes:
        if not self._zdict:  # less than one chunk: compress here, as zipfile does
            c = zlib.compressobj(*_DEFLATE)
            return b"".join([c.compress(data) for data in self._pending]) + c.flush()
        end = b"" if self._pending_size else _EMPTY_FINAL_BLOCK
        if self._pending_size:
            self._submit(last=True)
        return self._collect(wait=True) + end

    def _submit(self, last: bool) -> None:
        data = b"".join(self._pending)
        self._pending.clear()
        self._pending_size = 0
        # styles.xml comes in one large write: cut it into chunks of about CHUNK bytes.
        step = -(-len(data) // max(1, len(data) // CHUNK))
        view = memoryview(data)
        for start in range(0, len(data), step):
            chunk = view[start : start + step]
            final = last and start + step >= len(data)
            self._jobs.append(_submit(_deflate, chunk, self._zdict, final))
            self._zdict = bytes(chunk[-_WINDOW:])

    def _collect(self, wait: bool) -> bytes:
        jobs, done = self._jobs, []
        # Keep at most two chunks for each worker in memory.
        while jobs and (wait or jobs[0].done() or len(jobs) > 2 * WORKERS):
            done.append(jobs.popleft().result())
        return b"".join(done)


class ZipFile(zipfile.ZipFile):
    """A ``zipfile.ZipFile`` that compresses its DEFLATE members on the pool.

    A member with its own compression level keeps zipfile's compressor.
    """

    def open(self, name, mode="r", pwd=None, *, force_zip64=False):
        handle = super().open(name, mode, pwd, force_zip64=force_zip64)
        level = name.compress_level if isinstance(name, zipfile.ZipInfo) else self.compresslevel
        # _compressor is private zipfile API. If a later Python renames it, the members
        # stay correct: zipfile compresses them on this thread, as before.
        if (
            _threaded()
            and mode == "w"
            and level is None
            and isinstance(getattr(handle, "_compressor", None), _COMPRESSOR)
        ):
            handle._compressor = _ParallelDeflate()
        return handle
