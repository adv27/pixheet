"""The ``progress`` callback of ``convert``."""

import io
import zipfile
from collections.abc import Callable

import pytest
from sheets import Unseekable

from pixheet import convert


class Stop(Exception):
    pass


def recorder() -> tuple[list[tuple[int, int]], Callable[[int, int], None]]:
    calls: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        calls.append((done, total))

    return calls, progress


def stop_at(row: int) -> Callable[[int, int], None]:
    def progress(done: int, total: int) -> None:
        if done == row:
            raise Stop

    return progress


@pytest.mark.parametrize("compact", [False, True], ids=["full", "compact"])
@pytest.mark.parametrize("to_path", [False, True], ids=["stream", "path"])
def test_call_sequence(tmp_path, rgb_gradient, compact, to_path):
    h = rgb_gradient.height
    calls, progress = recorder()
    dst = tmp_path / "out.xlsx" if to_path else io.BytesIO()
    convert(rgb_gradient, dst, compact=compact, progress=progress)
    assert calls == [(i, h) for i in range(h + 1)]


def test_call_sequence_zip64_unseekable(rgb_gradient, monkeypatch):
    # A tiny sheet now counts as "over 2 GiB". An unseekable stream then makes the
    # writer go through a temporary file, and the callback must still get each row.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 50_000)
    h = rgb_gradient.height
    calls, progress = recorder()
    out = Unseekable()
    convert(rgb_gradient, out, progress=progress)
    assert calls == [(i, h) for i in range(h + 1)]
    assert zipfile.ZipFile(io.BytesIO(bytes(out.data))).testzip() is None


def test_one_row(rgb_one_pixel):
    calls, progress = recorder()
    convert(rgb_one_pixel, io.BytesIO(), progress=progress)
    assert calls == [(0, 1), (1, 1)]


@pytest.mark.parametrize("row", [0, 3, 48], ids=["first", "middle", "last"])
def test_raising_callback_leaves_no_files(tmp_path, rgb_gradient, row):
    with pytest.raises(Stop):
        convert(rgb_gradient, tmp_path / "out.xlsx", progress=stop_at(row))
    assert list(tmp_path.iterdir()) == []


def test_raising_callback_keeps_existing_output(tmp_path, rgb_gradient):
    dst = tmp_path / "out.xlsx"
    dst.write_bytes(b"previous")
    with pytest.raises(Stop):
        convert(rgb_gradient, dst, progress=stop_at(3))
    assert dst.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["out.xlsx"]


def test_raising_callback_with_stream(rgb_gradient):
    with pytest.raises(Stop):
        convert(rgb_gradient, io.BytesIO(), progress=stop_at(3))


@pytest.mark.parametrize("compact", [False, True], ids=["full", "compact"])
def test_output_does_not_change(rgb_noise, compact):
    plain, tracked = io.BytesIO(), io.BytesIO()
    result = convert(rgb_noise, plain, compact=compact)
    tracked_result = convert(rgb_noise, tracked, compact=compact, progress=lambda d, t: None)
    assert tracked_result.colors == result.colors
    # The zip entries carry the time of writing, so compare the parts, not the files.
    a, b = zipfile.ZipFile(plain), zipfile.ZipFile(tracked)
    assert a.namelist() == b.namelist()
    for name in a.namelist():
        assert a.read(name) == b.read(name), name
