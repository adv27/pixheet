"""Tests of the desktop app. The widget tests skip when no display is available."""

import gc
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest
from images import noise

from pixheet import gui
from pixheet.core import convert as real_convert
from pixheet.gui import (
    Converted,
    Failed,
    Finished,
    Job,
    Options,
    Progress,
    Started,
    Throttle,
    default_output,
    parse_drop,
    parse_options,
    plan_jobs,
    run_batch,
)

tk = pytest.importorskip("tkinter")


@pytest.fixture(autouse=True)
def collect_tk_garbage():
    """Collect the Tk objects of a test on the main thread, at the end of the test.

    A Tk variable deletes itself in Tcl when Python collects it. If the cyclic garbage
    collector runs on the worker thread of a later test, that call comes from the wrong
    thread: Tk waits for one second and then raises "main thread is not in main loop".
    """
    yield
    gc.collect()


@pytest.fixture
def splitlist():
    """The Tcl list parser of a Tcl interpreter. It does not need a display."""
    return tk.Tcl().splitlist


def image(path: Path, w: int = 4, h: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    noise(w, h, min(w * h, 10)).save(path)
    return path


def destroyed(root) -> bool:
    try:
        return not root.winfo_exists()
    except tk.TclError:
        return True


def no_temp_files(folder: Path) -> bool:
    return not list(folder.glob(".pixheet-*.tmp"))


# --- Helpers without a display -------------------------------------------------------


def test_default_output_is_next_to_the_input(tmp_path):
    assert default_output(tmp_path / "a b" / "pic.png") == tmp_path / "a b" / "pic.xlsx"
    assert default_output(Path("x.tar.png")) == Path("x.tar.xlsx")


def test_parse_drop_handles_braces_and_skips_folders_and_missing(tmp_path, splitlist):
    spaced = image(tmp_path / "my pics" / "one pic.png")
    plain = image(tmp_path / "two.png")
    data = (
        f"{{{spaced.as_posix()}}} {plain.as_posix()} {{{tmp_path.as_posix()}}} "
        f"{(tmp_path / 'missing.png').as_posix()} {plain.as_posix()}"
    )
    assert parse_drop(data, splitlist) == [spaced, plain]


def test_parse_drop_of_nothing(splitlist):
    assert parse_drop("", splitlist) == []


def test_throttle_limits_updates_but_lets_the_first_and_last_through():
    now = [0.0]
    throttle = Throttle(0.05, clock=lambda: now[0])
    assert throttle(0, 10)  # the first
    now[0] = 0.01
    assert not throttle(1, 10)
    now[0] = 0.06
    assert throttle(2, 10)
    now[0] = 0.07
    assert not throttle(3, 10)
    assert throttle(10, 10)  # the last, at once after the one before


def test_parse_options():
    assert parse_options("1000", "white", False) == Options(1000, (255, 255, 255), False)
    assert parse_options(" 0 ", "none", True) == Options(0, None, True)
    assert parse_options("50", "#102030", False).background == (16, 32, 48)


@pytest.mark.parametrize(
    ("side", "background", "match"),
    [
        ("abc", "white", "maximum side"),
        ("-1", "white", "maximum side"),
        ("", "white", "maximum side"),
        ("100", "nope", "background"),
    ],
)
def test_parse_options_refuses_bad_text(side, background, match):
    with pytest.raises(ValueError, match=match):
        parse_options(side, background, False)


def test_plan_jobs_finds_existing_outputs_and_conflicts(tmp_path):
    a_png = image(tmp_path / "a.png")
    a_jpg = tmp_path / "a.jpg"
    a_jpg.write_bytes(b"")
    b_png = image(tmp_path / "b.png")
    (tmp_path / "b.xlsx").write_bytes(b"old")
    sheet = tmp_path / "c.xlsx"
    sheet.write_bytes(b"old")

    plan = plan_jobs([a_png, a_jpg, b_png, sheet])

    assert plan.jobs == [Job(a_png, tmp_path / "a.xlsx"), Job(b_png, tmp_path / "b.xlsx")]
    assert plan.existing == [Job(b_png, tmp_path / "b.xlsx")]
    assert plan.errors == [
        "a.jpg: a.xlsx is also the output of a.png",
        "c.xlsx: the output file is the input file",
    ]


def run(jobs, cancel=None, interval=0.05):
    messages = []
    run_batch(jobs, Options(), messages.append, cancel or threading.Event(), interval)
    return messages


def test_run_batch_messages(tmp_path):
    src = image(tmp_path / "in.png", 6, 5)
    messages = run([Job(src, tmp_path / "out.xlsx")], interval=0)

    assert messages[0] == Started(1, 1, src)
    progress = [m for m in messages if isinstance(m, Progress)]
    assert [(m.done, m.total) for m in progress] == [(i, 5) for i in range(6)]
    assert isinstance(messages[-2], Converted)
    assert messages[-2].result.size == (6, 5)
    assert messages[-1] == Finished(1, cancelled=False)
    assert (tmp_path / "out.xlsx").exists()


def test_run_batch_continues_after_an_error(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    good = image(tmp_path / "good.png")
    messages = run([Job(bad, tmp_path / "bad.xlsx"), Job(good, tmp_path / "good.xlsx")])

    failed = [m for m in messages if isinstance(m, Failed)]
    assert len(failed) == 1
    assert failed[0].src == bad
    assert "cannot identify" in failed[0].error
    assert messages[-1] == Finished(1, cancelled=False)
    assert not (tmp_path / "bad.xlsx").exists()
    assert (tmp_path / "good.xlsx").exists()


def test_run_batch_cancelled_before_the_start(tmp_path):
    cancel = threading.Event()
    cancel.set()
    messages = run([Job(image(tmp_path / "in.png"), tmp_path / "out.xlsx")], cancel)
    assert messages == [Finished(0, cancelled=True)]
    assert not (tmp_path / "out.xlsx").exists()


def test_run_batch_cancelled_while_writing_keeps_the_old_file(tmp_path, monkeypatch):
    src = image(tmp_path / "in.png")
    dst = tmp_path / "out.xlsx"
    dst.write_bytes(b"old")
    cancel = threading.Event()

    def convert(*args, progress, **kwargs):
        def cancel_at_once(done, total):
            cancel.set()  # the user clicks Cancel while the sheet is written
            progress(done, total)

        return real_convert(*args, progress=cancel_at_once, **kwargs)

    monkeypatch.setattr(gui, "convert", convert)
    messages = run([Job(src, dst), Job(src, tmp_path / "second.xlsx")], cancel)

    assert messages == [Started(1, 2, src), Finished(0, cancelled=True)]
    assert dst.read_bytes() == b"old"
    assert not (tmp_path / "second.xlsx").exists()
    assert no_temp_files(tmp_path)


def test_main_without_tkinter(monkeypatch, capsys):
    monkeypatch.setattr(gui, "tk", None)
    assert gui.main([]) == 1
    assert "tkinter is not available" in capsys.readouterr().err


@pytest.mark.parametrize("error", [ImportError, RuntimeError])
def test_enable_dnd_falls_back(monkeypatch, error):
    def fail():
        raise error("no tkdnd")

    monkeypatch.setattr(gui, "_load_tkdnd", fail)
    assert gui.enable_dnd(object()) is None


# --- The window ----------------------------------------------------------------------


def new_root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    return root


class Harness:
    """An App on a hidden root, with dialogs that record their calls."""

    def __init__(self, real_dnd=False, answer=True):
        self.root = new_root()
        dnd_type = gui.enable_dnd(self.root) if real_dnd else None
        self.app = gui.App(self.root, dnd_type=dnd_type)
        self.errors = []
        self.questions = []
        self.app.show_error = lambda title, message, **kw: self.errors.append(message)
        self.app.ask_yes_no = lambda title, message, **kw: self.questions.append(message) or answer

    def drop(self, *paths):
        data = " ".join(f"{{{Path(p).as_posix()}}}" for p in paths)
        return self.app.on_drop(types.SimpleNamespace(data=data, action="copy"))

    def pump(self, until, timeout=20.0):
        deadline = time.monotonic() + timeout
        while not until():
            assert time.monotonic() < deadline, "timeout"
            self.root.update()
            time.sleep(0.005)

    def run_batch(self, *paths):
        """Drop the paths, and wait until the batch ends."""
        assert self.drop(*paths) == "copy"
        self.root.update()  # runs start()
        self.pump(lambda: not self.app.busy)

    def close(self):
        app = self.app
        app.cancel_event.set()
        if app.worker is not None:
            app.worker.join(10)
        try:
            app.dispose()
            self.root.destroy()
        except tk.TclError:  # the test destroyed the root
            pass


@pytest.fixture
def harness():
    made = []

    def make(**kwargs):
        made.append(Harness(**kwargs))
        return made[-1]

    yield make
    for h in made:
        h.close()


def test_drop_converts_a_file(harness, tmp_path):
    h = harness()
    src = image(tmp_path / "my pics" / "one pic.png", 8, 6)

    h.run_batch(src)

    dst = tmp_path / "my pics" / "one pic.xlsx"
    assert dst.exists()
    assert h.app.title_var.get() == "Converted 1 file."
    status = h.app.status_var.get()
    assert status.startswith(str(dst))
    assert "8x6" in status
    assert "10 colors" in status
    assert h.app.progress_var.get() == 1.0
    assert h.app.last_output == dst
    assert h.app.open_button.instate(["!disabled"])
    assert h.app.browse_button.instate(["!disabled"])
    assert h.errors == []
    assert no_temp_files(dst.parent)


def test_batch_with_an_error_continues_and_reports(harness, tmp_path):
    h = harness()
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    good = image(tmp_path / "good.png")

    h.run_batch(bad, good)

    assert (tmp_path / "good.xlsx").exists()
    assert h.app.title_var.get() == "Converted 1 of 2 files."
    assert h.app.status_var.get().endswith("1 file not converted.")
    assert len(h.errors) == 1
    assert "bad.png: cannot identify" in h.errors[0]


@pytest.mark.parametrize("answer", [True, False])
def test_existing_output_asks_once(harness, tmp_path, answer):
    h = harness(answer=answer)
    a = image(tmp_path / "a.png")
    b = image(tmp_path / "b.png")
    for src in (a, b):
        src.with_suffix(".xlsx").write_bytes(b"old")

    h.run_batch(a, b)

    assert len(h.questions) == 1
    assert "2 output files exist" in h.questions[0]
    if answer:
        assert h.app.title_var.get() == "Converted 2 files."
        assert a.with_suffix(".xlsx").read_bytes() != b"old"
    else:
        assert h.app.title_var.get() == "Converted 0 of 2 files. Skipped 2 files that exist."
        assert a.with_suffix(".xlsx").read_bytes() == b"old"
        assert b.with_suffix(".xlsx").read_bytes() == b"old"


def test_drop_of_folders_only(harness, tmp_path):
    h = harness()
    assert h.drop(tmp_path) == "copy"
    h.root.update()
    assert not h.app.busy
    assert "no files" in h.app.status_var.get()


def test_bad_options_show_an_error_and_do_not_start(harness, tmp_path):
    h = harness()
    h.app.max_side_var.set("abc")
    h.app.start([image(tmp_path / "a.png")])
    assert not h.app.busy
    assert len(h.errors) == 1
    assert "maximum side" in h.errors[0]


def test_options_go_to_convert(harness, tmp_path):
    h = harness()
    h.app.max_side_var.set("5")
    h.app.background_var.set("black")
    h.app.compact_var.set(True)
    src = image(tmp_path / "a.png", 20, 10)

    h.run_batch(src)

    assert "5x2" in h.app.status_var.get()
    with zipfile.ZipFile(tmp_path / "a.xlsx") as z:
        assert 'r="A1"' not in z.read("xl/worksheets/sheet1.xml").decode()


def gate_convert(monkeypatch):
    """Make convert wait at its first progress call until the test opens the gate."""
    gate = threading.Event()

    def convert(*args, progress, **kwargs):
        def wait_then_report(done, total):
            gate.wait(10)
            progress(done, total)

        return real_convert(*args, progress=wait_then_report, **kwargs)

    monkeypatch.setattr(gui, "convert", convert)
    return gate


def test_drop_while_busy_is_refused(harness, tmp_path, monkeypatch):
    gate = gate_convert(monkeypatch)
    h = harness()
    first = image(tmp_path / "first.png")
    second = image(tmp_path / "second.png")
    assert h.drop(first) == "copy"
    h.root.update()
    assert h.app.busy
    assert h.app.browse_button.instate(["disabled"])
    assert h.app.cancel_button.winfo_manager() == "grid"

    assert h.drop(second) == "refuse_drop"
    assert "in progress" in h.app.status_var.get()

    gate.set()
    h.pump(lambda: not h.app.busy)
    assert (tmp_path / "first.xlsx").exists()
    assert not (tmp_path / "second.xlsx").exists()
    assert h.app.cancel_button.winfo_manager() == ""


def test_cancel_stops_the_batch(harness, tmp_path, monkeypatch):
    gate = gate_convert(monkeypatch)
    h = harness()
    a = image(tmp_path / "a.png")
    b = image(tmp_path / "b.png")
    h.drop(a, b)
    h.root.update()
    h.pump(lambda: h.app.title_var.get().startswith("File 1 of 2"))

    h.app.cancel()
    gate.set()
    h.pump(lambda: not h.app.busy)

    assert h.app.title_var.get() == "Stopped. Converted 0 of 2 files."
    assert "did not write" in h.app.status_var.get()
    assert not (tmp_path / "a.xlsx").exists()
    assert not (tmp_path / "b.xlsx").exists()
    assert no_temp_files(tmp_path)
    assert h.errors == []


def test_close_during_a_batch_waits_for_the_worker(harness, tmp_path, monkeypatch):
    gate = gate_convert(monkeypatch)
    h = harness()
    src = image(tmp_path / "a.png")
    dst = tmp_path / "a.xlsx"
    dst.write_bytes(b"old")
    h.app.ask_yes_no = lambda *a, **k: True
    h.drop(src)
    h.root.update()
    worker = h.app.worker

    h.app.close()
    assert h.app.closing
    assert h.root.winfo_exists()  # the window waits for the worker
    gate.set()

    h.pump(lambda: h.app.worker is None)
    assert not worker.is_alive()
    assert destroyed(h.root)
    assert dst.read_bytes() == b"old"
    assert no_temp_files(tmp_path)


def test_close_when_idle_destroys_the_root(harness):
    h = harness()
    h.app.close()
    assert destroyed(h.root)


def test_browse_converts_the_chosen_files(harness, tmp_path):
    h = harness()
    src = image(tmp_path / "a.png")
    h.app.ask_files = lambda **kw: (str(src),)
    h.app.browse()
    h.pump(lambda: not h.app.busy)
    assert (tmp_path / "a.xlsx").exists()


def test_browse_cancelled_does_nothing(harness):
    h = harness()
    h.app.ask_files = lambda **kw: ""
    h.app.browse()
    assert not h.app.busy


def test_open_folder_button(harness, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(gui, "open_folder", opened.append)
    h = harness()
    h.run_batch(image(tmp_path / "a.png"))
    h.app.open_button.invoke()
    assert opened == [tmp_path / "a.xlsx"]


def test_open_folder_error_is_shown(harness, tmp_path, monkeypatch):
    def fail(path):
        raise OSError("no file manager")

    monkeypatch.setattr(gui, "open_folder", fail)
    h = harness()
    h.app.last_output = tmp_path / "a.xlsx"
    h.app.open_output()
    assert h.errors == ["Cannot open the folder: no file manager"]


def test_window_without_dnd_says_so(harness):
    h = harness(real_dnd=False)
    assert not h.app.dnd
    assert h.app.status_var.get() == gui.NO_DND_TEXT


def test_real_tkdnd_registers_the_drop_zone(harness, tmp_path):
    pytest.importorskip("tkinterdnd2")
    h = harness(real_dnd=True)
    assert h.app.dnd
    assert h.app.status_var.get() == ""
    for widget in (h.app.body, h.app.zone):
        assert "on_drop" in h.root.tk.call("bind", widget, "<<Drop>>")
    h.run_batch(image(tmp_path / "a.png"))  # the handler that tkdnd calls
    assert (tmp_path / "a.xlsx").exists()


def test_callback_error_is_shown_in_a_dialog(harness, capsys):
    h = harness()

    def boom():
        raise LookupError("boom")

    h.root.after_idle(boom)
    h.root.update()
    assert h.errors == ["An unexpected error occurred:\n\nLookupError('boom')"]
    assert "LookupError" in capsys.readouterr().err


def test_main_smoke(monkeypatch, capsys):
    new_root().destroy()  # skip when there is no display
    assert gui.main(["--smoke"]) == 0
    assert "pixheet-gui: drag and drop" in capsys.readouterr().out


def test_main_starts_the_files_in_argv(monkeypatch):
    new_root().destroy()
    started = []
    monkeypatch.setattr(gui.App, "start", lambda self, files: started.append(files))
    assert gui.main(["--smoke", "a b.png", "c.png"]) == 0
    assert started == [[Path("a b.png"), Path("c.png")]]


def test_python_m_pixheet_gui_smoke():
    new_root().destroy()
    result = subprocess.run(
        [sys.executable, "-m", "pixheet.gui", "--smoke"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "drag and drop" in result.stdout
