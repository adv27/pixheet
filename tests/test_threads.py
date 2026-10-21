"""Each threaded helper must give the same pixels, or the same decompressed bytes, as the
plain, one-thread call."""

import gc
import io
import random
import threading
import time
import weakref
import zipfile
import zlib
from concurrent.futures import Future

import pytest
from images import encoded, noise
from PIL import Image
from sheets import SHEET, Unseekable, write

from pixheet import _threads, prepare

LANCZOS = Image.Resampling.LANCZOS


def random_rgb(w: int, h: int, seed: int = 0) -> Image.Image:
    return Image.frombytes("RGB", (w, h), random.Random(seed).randbytes(w * h * 3))


@pytest.fixture(params=[2, 3, 8], ids=lambda n: f"{n}-workers")
def workers(request, monkeypatch):
    monkeypatch.setattr(_threads, "WORKERS", request.param)
    monkeypatch.setattr(_threads, "_pool", None)
    monkeypatch.setattr(_threads, "_serial", False)
    return request.param


@pytest.fixture
def jobs(monkeypatch):
    """Count the jobs that go to the pool, with 4 workers."""
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "_pool", None)
    monkeypatch.setattr(_threads, "_serial", False)
    count = []
    real = _threads._shared_pool

    class Counting:
        def submit(self, fn, *args):
            count.append(fn)
            return real().submit(fn, *args)

    monkeypatch.setattr(_threads, "_shared_pool", Counting)
    return count


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ((1201, 799), (1000, 665)),
        ((40, 30), (17, 13)),
        ((2000, 5), (1000, 2)),
        ((2000, 1), (1000, 1)),
        ((1, 2000), (1, 1000)),
        ((300, 120), (300, 60)),  # only the height changes
        ((300, 120), (150, 120)),  # only the width changes
        # Pillow's pass order: vertical first when the height loses more than two
        # times the pixels of the width, and horizontal first at the tie.
        ((5, 2000), (2, 1000)),
        ((300, 3000), (100, 1000)),
        ((300, 1201), (250, 1000)),
        ((100, 1000), (50, 900)),
        # Enlargements: the height does not shrink, so the horizontal pass goes first.
        ((10, 10), (40, 15)),
        ((100, 60), (300, 120)),
        # The width grows and the height shrinks: the vertical pass goes first.
        ((400, 2000), (800, 1000)),
        ((900, 900), (1800, 300)),
    ],
)
def test_resize_is_identical(workers, src, dst):
    im = random_rgb(*src)
    assert _threads.resize(im, dst).tobytes() == im.resize(dst, LANCZOS).tobytes()


def test_resize_is_identical_on_random_shapes(workers):
    rng = random.Random(workers)
    for _ in range(40):
        w, h = rng.randint(1, 300), rng.randint(1, 300)
        size = (rng.randint(1, w), rng.randint(1, h))
        im = random_rgb(w, h, seed=w * h)
        assert _threads.resize(im, size).tobytes() == im.resize(size, LANCZOS).tobytes(), (
            (w, h),
            size,
        )


def test_resize_keeps_info_and_other_modes(workers, monkeypatch):
    passes = []
    real = _threads._pass
    monkeypatch.setattr(_threads, "_pass", lambda *args: passes.append(args) or real(*args))
    size = (200, 150)
    im = random_rgb(400, 300)
    im.info["dpi"] = (300, 300)
    out = _threads.resize(im, size)
    assert passes  # the image is large enough for strips
    assert out.info == im.resize(size, LANCZOS).info
    assert out.info is not im.info  # a copy, as im.resize() gives
    # Not RGB: the plain call. Random alpha, so that strips would give other bytes.
    rgba = Image.frombytes("RGBA", im.size, random.Random(1).randbytes(400 * 300 * 4))
    assert _threads.resize(rgba, size).tobytes() == rgba.resize(size, LANCZOS).tobytes()


def test_resize_does_not_copy_the_input(workers, monkeypatch):
    im = random_rgb(400, 300)
    cropped = []
    real = Image.Image.crop

    def spy(self, *args, **kwargs):
        cropped.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "crop", spy)
    assert _threads.resize(im, (200, 150)).tobytes() == im.resize((200, 150), LANCZOS).tobytes()
    assert all(source is not im for source in cropped)


def test_resize_under_a_low_decompression_bomb_limit(workers, monkeypatch):
    # crop() checks Pillow's pixel limit and resize() does not. The strips must not
    # turn a working resize into a warning or an error (pytest makes warnings errors).
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    im = random_rgb(300, 200)
    assert _threads.resize(im, (150, 100)).tobytes() == im.resize((150, 100), LANCZOS).tobytes()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "BMP", "TIFF"])
def test_resize_loads_a_file_image_before_the_threads_use_it(workers, fmt):
    # Image.open() does not decode the pixels. The strip threads must not decode them.
    data = encoded(random_rgb(1200, 900), fmt).getvalue()
    out = _threads.resize(Image.open(io.BytesIO(data)), (500, 375))
    expected = Image.open(io.BytesIO(data)).resize((500, 375), LANCZOS)
    assert out.tobytes() == expected.tobytes()


@pytest.mark.parametrize("fmt", ["JPEG", "BMP", "TIFF"])
def test_prepare_on_an_image_that_is_not_loaded(workers, fmt):
    # Not PNG: normalize() reads its EXIF, which loads a PNG.
    data = encoded(random_rgb(1600, 1200), fmt).getvalue()
    expected = Image.open(io.BytesIO(data))
    expected.load()
    assert prepare(Image.open(io.BytesIO(data))).tobytes() == prepare(expected).tobytes()


@pytest.mark.parametrize(
    ("size", "side"),
    [
        ((1000, 667), 701),
        ((667, 1000), 999),
        ((1000, 667), 1000),  # no change
        ((300, 300), 400),  # larger than the image: no change
        ((1000, 667), 250),  # the width shrinks 4 times: thumbnail() reduces by (2, 1)
        ((667, 1000), 250),  # the height shrinks 4 times: thumbnail() reduces by (1, 2)
        ((1000, 667), 251),  # a little less than 4 times: no reduce
        ((16384, 33), 3000),
        ((33, 16384), 3000),
        ((5, 1999), 1000),
    ],
)
def test_thumbnail_is_identical(workers, size, side):
    im = random_rgb(*size)
    expected = im.copy()
    expected.thumbnail((side, side), LANCZOS)
    out = _threads.thumbnail(im, side)
    assert out is not im
    assert (out.size, out.tobytes()) == (expected.size, expected.tobytes())


def test_thumbnail_size_matches_pillow():
    rng = random.Random(1)
    for _ in range(2000):
        w, h = rng.randint(1, 3000), rng.randint(1, 3000)
        side = rng.randint(1, max(w, h) - 1) if max(w, h) > 1 else 1
        expected = Image.new("1", (w, h))
        expected.thumbnail((side, side))
        if expected.size != (w, h):
            assert _threads._thumbnail_size(w, h, side) == expected.size, (w, h, side)


def test_resize_and_thumbnail_use_the_pool(jobs):
    im = random_rgb(400, 300)
    _threads.resize(im, (200, 150))
    assert len(jobs) == 8  # 4 strips in each of the 2 passes
    jobs.clear()
    _threads.thumbnail(im, 199)
    assert len(jobs) == 8


def test_first_column_pass_uses_more_strips_on_a_large_input(jobs, monkeypatch):
    # A tall image: the vertical pass goes first and copies its strips. On a large input,
    # more strips than workers keep less of the input copied at one time.
    monkeypatch.setattr(_threads, "_LARGE", 1_000_000)
    im = random_rgb(400, 3000)
    assert _threads.resize(im, (100, 750)).tobytes() == im.resize((100, 750), LANCZOS).tobytes()
    assert len(jobs) == 8 + 4  # min(2 * 4, 400 // 32) = 8 column strips, then 4 row strips
    jobs.clear()
    monkeypatch.setattr(_threads, "_LARGE", 2_000_000)  # now the input is not large
    _threads.resize(im, (100, 750))
    assert len(jobs) == 4 + 4


@pytest.mark.parametrize("size", [0, 1, 999, 1000, 1001, 4000, 30_000])
def test_parallel_deflate_round_trip(workers, monkeypatch, size):
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    rng = random.Random(size)
    data = b"".join(f'<c r="A{rng.randrange(99)}"/>'.encode() for _ in range(size))[:size]
    deflate = _threads._ParallelDeflate()
    out = b"".join(deflate.compress(data[i : i + 777]) for i in range(0, size, 777))
    out += deflate.flush()
    d = zlib.decompressobj(-15)
    assert d.decompress(out) == data
    assert d.eof
    assert not d.unused_data


def test_one_large_write_is_cut_into_chunks(jobs, monkeypatch):
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    data = bytes(range(256)) * 40  # 10,240 bytes in one write, as styles.xml comes
    deflate = _threads._ParallelDeflate()
    out = deflate.compress(data) + deflate.flush()
    assert zlib.decompress(out, -15) == data
    assert len(jobs) == 10


def test_small_member_has_the_bytes_of_zipfile(workers):
    data = b"<row/>" * 1000  # less than one chunk
    deflate = _threads._ParallelDeflate()
    out = deflate.compress(data) + deflate.flush()
    c = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    assert out == c.compress(data) + c.flush()


def test_size_is_close_to_one_stream(monkeypatch):
    # The preset dictionary keeps the size close: without it, this is about 1 % larger.
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "_pool", None)
    rng = random.Random(3)
    data = b"".join(
        f'<c r="A{rng.randrange(999)}" s="{rng.randrange(50)}"/>'.encode() for _ in range(60_000)
    )  # about 1.2 MB: more than 4 chunks
    deflate = _threads._ParallelDeflate()
    out = b"".join(deflate.compress(data[i : i + 65536]) for i in range(0, len(data), 65536))
    out += deflate.flush()
    c = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    assert zlib.decompress(out, -15) == data
    assert len(out) <= len(c.compress(data) + c.flush()) * 1.005


def test_parallel_deflate_keeps_two_chunks_for_each_worker(workers, monkeypatch):
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    real = _threads._deflate

    def slow(data, zdict, last):  # the writer is faster than the pool
        time.sleep(0.005)
        return real(data, zdict, last)

    monkeypatch.setattr(_threads, "_deflate", slow)
    data = bytes(range(256)) * 160  # 41 chunks: 40 full chunks and one of 960 bytes
    deflate = _threads._ParallelDeflate()
    early = b""
    for i in range(0, len(data), 1000):
        early += deflate.compress(data[i : i + 1000])
        assert len(deflate._jobs) <= 2 * workers
    assert early  # compress() gives bytes before flush()
    assert zlib.decompress(early + deflate.flush(), -15) == data


@pytest.mark.parametrize("total", [3000, 9000])  # under one chunk, and several chunks
def test_parallel_deflate_copies_a_buffer_that_the_caller_changes(workers, monkeypatch, total):
    monkeypatch.setattr(_threads, "CHUNK", 4096)
    buf = bytearray(1500)
    written_data = b""
    deflate = _threads._ParallelDeflate()
    out = b""
    for i in range(total // 1500):
        buf[:] = bytes([i]) * 1500  # the caller writes new data into the same buffer
        written_data += bytes(buf)
        out += deflate.compress(memoryview(buf))
    del buf[:10]  # the caller can resize its buffer: no export holds it
    assert zlib.decompress(out + deflate.flush(), -15) == written_data


def test_work_runs_on_this_thread_when_the_pool_takes_none(monkeypatch):
    # At interpreter shutdown, thread pools refuse new work. Each part below starts
    # with the pool in use, so each part gets a refusal.
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "CHUNK", 1000)

    class Closed:
        def submit(self, fn, *args):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(_threads, "_shared_pool", Closed)
    # First direct jobs, with a timeout: a lost job fails the test and does not hang it.
    monkeypatch.setattr(_threads, "_serial", False)
    failed = _threads._submit(lambda: 1 / 0)
    assert isinstance(failed, Future)
    with pytest.raises(ZeroDivisionError):
        failed.result(timeout=5)
    assert _threads._serial  # from now on, the helpers do the plain calls
    failed = _threads._submit(lambda: 1 / 0)  # a plain call: the error goes to the future
    with pytest.raises(ZeroDivisionError):
        failed.result(timeout=5)

    monkeypatch.setattr(_threads, "_serial", False)
    im = random_rgb(400, 300)
    assert _threads.resize(im, (200, 150)).tobytes() == im.resize((200, 150), LANCZOS).tobytes()

    monkeypatch.setattr(_threads, "_serial", False)
    data = bytes(range(256)) * 40
    deflate = _threads._ParallelDeflate()
    assert zlib.decompress(deflate.compress(data) + deflate.flush(), -15) == data


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("dst_type", [io.BytesIO, Unseekable])
@pytest.mark.parametrize("zip64", [False, True], ids=["normal", "zip64"])
def test_threaded_writer_gives_the_same_parts(monkeypatch, compact, dst_type, zip64):
    monkeypatch.setattr(_threads, "CHUNK", 4096)  # chunk a small image's parts too
    if zip64:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 50_000)
    im = noise(120, 80, 3000, seed=7)
    monkeypatch.setattr(_threads, "WORKERS", 1)
    plain = write(im, dst_type(), compact=compact)
    monkeypatch.setattr(_threads, "WORKERS", 8)
    monkeypatch.setattr(_threads, "_pool", None)
    chunks = []
    real = _threads._deflate

    def counted(data, zdict, last):
        chunks.append(len(data))
        return real(data, zdict, last)

    monkeypatch.setattr(_threads, "_deflate", counted)
    threaded = write(im, dst_type(), compact=compact)
    assert len(chunks) > 2  # the sheet and styles parts went through the pool
    for name in plain.namelist():
        assert threaded.read(name) == plain.read(name), name


def test_one_worker_uses_no_pool(monkeypatch):
    monkeypatch.setattr(_threads, "WORKERS", 1)
    monkeypatch.setattr(_threads, "CHUNK", 4096)

    def no_pool():
        raise AssertionError("the helpers must not use the pool with one worker")

    monkeypatch.setattr(_threads, "_shared_pool", no_pool)
    im = random_rgb(400, 300)
    _threads.resize(im, (200, 150))
    _threads.thumbnail(im, 199)
    write(noise(120, 80, 3000))


def test_after_a_refusal_no_job_goes_to_the_pool(monkeypatch):
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "CHUNK", 4096)
    monkeypatch.setattr(_threads, "_serial", True)

    def no_pool(*args):
        raise AssertionError("no job must go to the pool after it refused work")

    monkeypatch.setattr(_threads, "_shared_pool", no_pool)
    assert _threads._submit(pow, 2, 3).result(timeout=5) == 8  # it runs on this thread
    monkeypatch.setattr(_threads, "_submit", no_pool)  # the helpers make no jobs
    im = random_rgb(400, 300)
    assert _threads.resize(im, (200, 150)).tobytes() == im.resize((200, 150), LANCZOS).tobytes()
    _threads.thumbnail(im, 199)
    write(noise(120, 80, 3000))


def test_new_pool_after_fork(monkeypatch):
    monkeypatch.setattr(_threads, "WORKERS", 2)
    monkeypatch.setattr(_threads, "_pool", None)
    first = _threads._shared_pool()
    assert _threads._shared_pool() is first
    monkeypatch.setattr(_threads.os, "getpid", lambda: -1)  # as in a forked child
    assert _threads._shared_pool() is not first


def test_unknown_zipfile_internals_fall_back_to_zipfile(monkeypatch):
    # If a later Python changes its compressor, pixheet must leave it alone.
    monkeypatch.setattr(_threads, "_COMPRESSOR", type(None))
    monkeypatch.setattr(_threads, "CHUNK", 4096)
    monkeypatch.setattr(_threads, "WORKERS", 8)  # also on a host with one CPU
    monkeypatch.setattr(_threads, "_pool", None)
    im = noise(120, 80, 3000, seed=7)
    a = write(im)
    monkeypatch.setattr(_threads, "WORKERS", 1)
    b = write(im)
    assert a.getinfo(SHEET).compress_size == b.getinfo(SHEET).compress_size


def test_missing_compressor_attribute_falls_back(monkeypatch):
    # If a later Python renames _compressor, pixheet must leave the handle alone.
    monkeypatch.setattr(_threads, "WORKERS", 2)
    handle = io.BytesIO()  # a write handle without a _compressor attribute
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, *args, **kwargs: handle)
    with _threads.ZipFile(io.BytesIO(), "w", zipfile.ZIP_DEFLATED) as zf:
        assert zf.open("a.bin", "w") is handle
    assert not hasattr(handle, "_compressor")


def test_thread_start_failure_runs_each_job_once(monkeypatch):
    # At a thread limit, submit() queues the job and then fails to start a thread.
    # Each part below starts with the pool in use, so one job stays in the queue of
    # the pool. That job must not run, and it must keep nothing alive.
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "_pool", None)
    real_start = threading.Thread.start

    def start(self):
        if self.name.startswith("pixheet"):
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    # First direct jobs, with a timeout: a lost job fails the test and does not hang it.
    monkeypatch.setattr(_threads, "_serial", False)
    result = _threads._submit(lambda: Image.new("RGB", (8, 8))).result(timeout=5)
    assert _threads._serial  # no more jobs go to the pool that cannot start threads
    assert _threads._submit(pow, 2, 3).result(timeout=5) == 8  # a plain call
    result_ref = weakref.ref(result)
    del result
    gc.collect()
    assert result_ref() is None  # the job in the queue keeps no result alive

    calls = []
    real = _threads._deflate

    def counted(data, zdict, last):
        calls.append(1)
        return real(data, zdict, last)

    monkeypatch.setattr(_threads, "_deflate", counted)
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    monkeypatch.setattr(_threads, "_serial", False)
    data = bytes(range(256)) * 40
    deflate = _threads._ParallelDeflate()
    assert zlib.decompress(deflate.compress(data) + deflate.flush(), -15) == data
    assert len(calls) == 10  # each chunk once

    monkeypatch.setattr(_threads, "_serial", False)
    im = random_rgb(400, 300)
    ref = weakref.ref(im)
    assert _threads.resize(im, (200, 150)).tobytes() == im.resize((200, 150), LANCZOS).tobytes()
    del im
    gc.collect()
    assert ref() is None  # and no image


def test_member_with_its_own_level_keeps_zipfile_compression(monkeypatch):
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    monkeypatch.setattr(_threads, "_deflate", None)  # the pool must not compress
    data = bytes(range(256)) * 400
    for level in (1, 9):
        # A level for one member: writestr(..., compresslevel=level).
        info = zipfile.ZipInfo("a.xml", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        expected, got = io.BytesIO(), io.BytesIO()
        with zipfile.ZipFile(expected, "w") as zf:
            zf.writestr(info, data, compresslevel=level)
        with _threads.ZipFile(got, "w") as zf:
            zf.writestr(info, data, compresslevel=level)
        assert got.getvalue() == expected.getvalue()
        # A level for the archive, and a member opened by name.
        sizes = []
        for cls in (zipfile.ZipFile, _threads.ZipFile):
            buf = io.BytesIO()
            with cls(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as zf:
                with zf.open("a.xml", "w") as f:
                    f.write(data)
                sizes.append(zf.getinfo("a.xml").compress_size)
        assert sizes[0] == sizes[1]


def test_thumbnail_does_not_copy_the_source_when_it_reduces(workers, monkeypatch):
    im = random_rgb(1000, 667)
    expected = im.copy()
    expected.thumbnail((250, 250), LANCZOS)  # shrinks 4 times: reduce() first
    copied = []
    real = Image.Image.copy

    def spy(self):
        copied.append(self)
        return real(self)

    monkeypatch.setattr(Image.Image, "copy", spy)
    out = _threads.thumbnail(im, 250)
    assert out.tobytes() == expected.tobytes()
    assert im not in copied


def test_job_runs_once_when_only_some_threads_start(monkeypatch):
    # The first worker thread starts, and later ones fail. A job can then wait in the
    # queue of a live worker and also go to the calling thread. Only one of them must
    # run it.
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "_pool", None)
    monkeypatch.setattr(_threads, "_serial", False)
    monkeypatch.setattr(_threads, "CHUNK", 1000)
    real_start = threading.Thread.start
    started = []

    def start(self):
        if self.name.startswith("pixheet"):
            if started:
                raise RuntimeError("can't start new thread")
            started.append(self)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    calls = []
    real = _threads._deflate

    def slow(data, zdict, last):
        calls.append(1)
        time.sleep(0.01)  # keep the one worker busy, so that jobs wait in its queue
        return real(data, zdict, last)

    monkeypatch.setattr(_threads, "_deflate", slow)
    data = bytes(range(256)) * 40  # 10 chunks
    deflate = _threads._ParallelDeflate()
    assert zlib.decompress(deflate.compress(data) + deflate.flush(), -15) == data
    time.sleep(0.3)  # let the worker take the jobs left in its queue
    assert len(calls) == 10


class _Sized:
    def __len__(self):
        return 3


def test_job_in_a_live_queue_holds_no_data(monkeypatch):
    # Only the first worker starts. It waits on a gate, so a job stays in its queue.
    monkeypatch.setattr(_threads, "WORKERS", 2)
    monkeypatch.setattr(_threads, "_pool", None)
    monkeypatch.setattr(_threads, "_serial", False)
    real_start = threading.Thread.start
    started = []

    def start(self):
        if self.name.startswith("pixheet"):
            if started:
                raise RuntimeError("can't start new thread")
            started.append(self)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    gate = threading.Event()
    try:
        blocker = _threads._submit(gate.wait)
        obj = _Sized()
        ref = weakref.ref(obj)
        assert _threads._submit(len, obj).result(timeout=5) == 3  # it runs here
        del obj
        gc.collect()
        assert ref() is None  # the job in the live queue holds no data
    finally:
        gate.set()
    blocker.result(timeout=5)


def test_many_callers_while_the_pool_refuses(monkeypatch):
    # Callers on many threads must get their results, also when the pool starts to
    # refuse work in the middle of their calls.
    monkeypatch.setattr(_threads, "WORKERS", 4)
    monkeypatch.setattr(_threads, "_pool", None)
    monkeypatch.setattr(_threads, "_serial", False)
    real = _threads._shared_pool
    count = [0]

    class SometimesClosed:
        def submit(self, fn, *args):
            count[0] += 1
            if count[0] > 50:
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")
            return real().submit(fn, *args)

    monkeypatch.setattr(_threads, "_shared_pool", SometimesClosed)
    errors, results = [], []

    def caller(k):
        try:
            results.extend(_threads._submit(pow, k, 2).result(timeout=10) for _ in range(50))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=caller, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert sorted(results) == sorted(k * k for k in range(8) for _ in range(50))
