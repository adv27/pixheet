"""Tests of the desktop app. The widget tests skip when no display is available."""

import gc
import importlib.util
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

    A Tk variable deletes itself in Tcl when Python collects it. The cyclic garbage
    collector can run on the worker thread of a later test. Then the call comes from the
    wrong thread. Tk waits for one second and then raises "main thread is not in main loop".
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
            cancel.set()  # the user clicks Cancel while convert writes the sheet
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


@pytest.mark.parametrize(("platform", "opener"), [("linux", "xdg-open"), ("darwin", "open")])
def test_open_folder_does_not_wait_for_the_opener(tmp_path, monkeypatch, platform, opener):
    started = []
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        started.append((command, kwargs))
        # An opener that runs for some time, as xdg-open can.
        return real_popen([sys.executable, "-c", "import time; time.sleep(1)"], **kwargs)

    monkeypatch.setattr(gui.subprocess, "Popen", popen)
    t0 = time.monotonic()
    gui.open_folder(tmp_path / "a.xlsx", platform=platform)
    assert time.monotonic() - t0 < 0.5

    [(command, kwargs)] = started
    assert command == [opener, str(tmp_path)]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == kwargs["stderr"] == kwargs["stdin"] == subprocess.DEVNULL
    waiter = next(t for t in threading.enumerate() if t.name == "pixheet-opener")
    waiter.join(10)
    assert not waiter.is_alive()


def test_open_folder_without_an_opener_raises(tmp_path, monkeypatch):
    def popen(command, **kwargs):
        raise FileNotFoundError(2, "No such file", command[0])

    monkeypatch.setattr(gui.subprocess, "Popen", popen)
    with pytest.raises(FileNotFoundError, match="xdg-open"):  # App shows it in a dialog
        gui.open_folder(tmp_path / "a.xlsx", platform="linux")


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

    def __init__(self, answer=True):
        self.root = new_root()
        self.app = gui.App(self.root, dnd_type=None)
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
    assert h.app.note_var.get() == gui.BUSY_DROP_TEXT
    h.app._handle(Progress(1, 3))  # a worker message does not replace the note
    assert h.app.note_var.get() == gui.BUSY_DROP_TEXT

    gate.set()
    h.pump(lambda: not h.app.busy)
    assert (tmp_path / "first.xlsx").exists()
    assert not (tmp_path / "second.xlsx").exists()
    assert h.app.cancel_button.winfo_manager() == ""
    assert h.app.note_var.get() == gui.BUSY_DROP_TEXT  # it stays true after the batch

    h.run_batch(second)  # the next batch removes the note
    assert h.app.note_var.get() == ""
    assert (tmp_path / "second.xlsx").exists()


@pytest.mark.parametrize("stop", ["cancel", "close"])
def test_stopping_text_stays_until_the_worker_stops(harness, tmp_path, monkeypatch, stop):
    gate = gate_convert(monkeypatch)
    h = harness()
    h.drop(image(tmp_path / "a.png"))
    h.root.update()  # runs start(); the window reads the Started message later
    getattr(h.app, stop)()
    text = h.app.status_var.get()
    assert text.startswith("Stopping.")

    h.pump(lambda: h.app.title_var.get().startswith("File 1 of 1"))
    assert h.app.status_var.get() == text

    gate.set()
    h.pump(lambda: not h.app.busy)


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


def convert_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "pixheet-convert"]


def drop_inside(h, path, seen):
    """Return a dialog that drops ``path`` and runs the event loop, as a modal dialog does."""

    def dialog(*args, **kwargs):
        seen.append(h.drop(path))
        h.root.update()  # runs a start() that the drop scheduled
        seen.append(len(convert_threads()))
        return kwargs.pop("result", True)

    return dialog


def test_drop_during_the_replace_prompt_is_refused(harness, tmp_path):
    h = harness()
    a = image(tmp_path / "a.png")
    a.with_suffix(".xlsx").write_bytes(b"old")
    b = image(tmp_path / "b.png")
    seen = []
    h.app.ask_yes_no = drop_inside(h, b, seen)

    h.run_batch(a)

    assert seen == ["refuse_drop", 0]  # no worker started inside the prompt
    assert h.app.note_var.get() == gui.BUSY_DROP_TEXT
    assert h.app.title_var.get() == "Converted 1 file."
    assert a.with_suffix(".xlsx").read_bytes() != b"old"
    assert not (tmp_path / "b.xlsx").exists()
    assert convert_threads() == []


def test_drop_during_the_file_dialog_is_refused(harness, tmp_path):
    h = harness()
    a = image(tmp_path / "a.png")
    b = image(tmp_path / "b.png")
    seen = []
    pick = drop_inside(h, b, seen)
    h.app.ask_files = lambda **kw: pick(**kw, result=(str(a),))

    h.app.browse()
    h.pump(lambda: not h.app.busy)

    assert seen == ["refuse_drop", 0]
    assert (tmp_path / "a.xlsx").exists()
    assert not (tmp_path / "b.xlsx").exists()


def test_drop_during_the_error_dialog_is_refused(harness, tmp_path):
    h = harness()
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    good = image(tmp_path / "good.png")
    seen = []
    h.app.show_error = drop_inside(h, good, seen)

    h.run_batch(bad)

    assert seen == ["refuse_drop", 0]
    assert not (tmp_path / "good.xlsx").exists()
    # No second batch and no second timer that reads the queue.
    assert h.app.worker is None
    assert h.app._poll_id is None
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        h.root.update()
    assert h.app._poll_id is None


def test_second_start_before_idle_is_refused_with_a_note(harness, tmp_path, monkeypatch):
    gate = gate_convert(monkeypatch)
    h = harness()
    a = image(tmp_path / "a.png")
    b = image(tmp_path / "b.png")
    assert h.drop(a) == "copy"
    assert h.drop(b) == "copy"  # the first start() did not run yet
    h.root.update()
    assert len(convert_threads()) == 1
    assert h.app.note_var.get() == gui.BUSY_DROP_TEXT

    gate.set()
    h.pump(lambda: not h.app.busy)
    assert (tmp_path / "a.xlsx").exists()
    assert not (tmp_path / "b.xlsx").exists()


def test_close_during_a_dialog_closes_after_it(harness, tmp_path):
    h = harness()
    a = image(tmp_path / "a.png")
    a.with_suffix(".xlsx").write_bytes(b"old")
    alive = []

    def ask(*args, **kwargs):
        h.app.close()
        alive.append(h.root.winfo_exists())  # the window waits for the dialog
        return True

    h.app.ask_yes_no = ask
    h.app.start([a])

    assert alive == [1]
    assert destroyed(h.root)
    assert h.app.worker is None
    assert a.with_suffix(".xlsx").read_bytes() == b"old"


def test_old_messages_do_not_reach_a_new_batch(harness, tmp_path):
    h = harness()
    h.app.queue.put(Finished(0, cancelled=True))  # a message that no batch reads

    h.run_batch(image(tmp_path / "a.png"))

    assert h.app.title_var.get() == "Converted 1 file."
    assert (tmp_path / "a.xlsx").exists()


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
    h = harness()
    assert not h.app.dnd
    assert h.app.status_var.get() == gui.NO_DND_TEXT
    assert h.app.zone_label.cget("text") == "Choose images to convert"
    assert h.app.or_label.winfo_manager() == ""  # no "or" above Browse


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


def sheet_size(path: Path) -> tuple[int, int]:
    """Return the number of columns and rows of the sheet in ``path``."""
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    try:
        return workbook.active.max_column, workbook.active.max_row
    finally:
        workbook.close()


def test_main_smoke_waits_for_the_batch(tmp_path):
    new_root().destroy()
    src = image(tmp_path / "pic.png", 40, 30)
    assert gui.main(["--smoke", str(src)]) == 0
    assert sheet_size(default_output(src)) == (40, 30)
    assert no_temp_files(tmp_path)


def test_main_smoke_fails_when_a_file_fails(tmp_path, capsys):
    new_root().destroy()
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    good = image(tmp_path / "good.png")
    assert gui.main(["--smoke", str(bad), str(good)]) == 1
    assert "bad.png" in capsys.readouterr().err
    assert default_output(good).is_file()


def test_main_smoke_does_not_replace_files(tmp_path):
    new_root().destroy()
    src = image(tmp_path / "pic.png")
    default_output(src).write_bytes(b"old")
    assert gui.main(["--smoke", str(src)]) == 0
    assert default_output(src).read_bytes() == b"old"


def test_main_smoke_of_a_built_app_needs_the_icon_and_dnd(tmp_path, monkeypatch, capsys):
    new_root().destroy()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # no icon here
    monkeypatch.setattr(gui, "enable_dnd", lambda root: None)
    assert gui.main(["--smoke"]) == 1
    err = capsys.readouterr().err
    assert "The window icon is missing." in err
    assert "Drag and drop is not available." in err


def test_main_smoke_of_a_built_app_needs_the_theme(monkeypatch, capsys):
    new_root().destroy()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui, "_load_sv_ttk", no_sv_ttk)
    assert gui.main(["--smoke"]) == 1
    assert "The Sun Valley theme is not available." in capsys.readouterr().err


# --- The theme -----------------------------------------------------------------------


def no_sv_ttk():
    raise ImportError("No module named 'sv_ttk'")


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_winreg(value=None, error=None):
    """Return a fake winreg module. It has the value of AppsUseLightTheme, or raises."""

    def open_key(root, path):
        assert root == "HKCU"
        assert path == gui.PERSONALIZE_KEY
        if error is not None:
            raise error
        return FakeKey()

    def query(key, name):
        assert name == "AppsUseLightTheme"
        return value, 4

    return types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", OpenKey=open_key, QueryValueEx=query)


@pytest.mark.parametrize(
    ("winreg", "theme"),
    [
        (fake_winreg(1), "light"),
        (fake_winreg(0), "dark"),
        (fake_winreg(error=FileNotFoundError(2, "The key does not exist")), "light"),
        (fake_winreg(error=PermissionError(5, "Access is denied")), "light"),
    ],
)
def test_system_theme_reads_the_windows_setting(monkeypatch, winreg, theme):
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    assert gui.system_theme(platform="win32") == theme


def test_system_theme_without_winreg(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)  # the import raises ImportError
    assert gui.system_theme(platform="win32") == "light"


def test_system_theme_on_other_systems_is_light(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(0))
    assert gui.system_theme(platform="linux") == "light"
    assert gui.system_theme(platform="darwin") == "light"


@pytest.mark.parametrize(
    ("value", "theme"),
    [("light", "light"), (" Dark ", "dark"), ("system", None), ("", None), ("blue", None)],
)
def test_theme_override(value, theme):
    assert gui.theme_override({gui.THEME_ENV: value}) == theme
    assert gui.theme_override({}) is None


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_apply_theme_uses_sun_valley(mode):
    pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        assert gui.apply_theme(root, mode) == f"sun-valley-{mode}"
        assert gui.theme_name(root) == f"sun-valley-{mode}"
    finally:
        root.destroy()


def test_disabled_accent_button_is_readable_in_the_light_theme():
    pytest.importorskip("sv_ttk")
    from tkinter import ttk

    root = new_root()
    try:
        gui.apply_theme(root, "dark")  # the change stays when the theme changes later
        gui.apply_theme(root, "light")
        colors = [(str(s), str(c)) for s, c in ttk.Style(root).map("Accent.TButton", "foreground")]
        assert ("disabled", gui.LIGHT_DISABLED_ACCENT_FG) in colors
    finally:
        root.destroy()


def test_apply_theme_scales_the_fonts_for_the_screen():
    pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        root.tk.call("tk", "scaling", 2.0)  # 144 dpi: 150 %
        gui.apply_theme(root, "light")
        assert root.tk.call("font", "configure", "SunValleyBodyFont", "-size") == -21
        gui.apply_theme(root, "dark")  # only once
        assert root.tk.call("font", "configure", "SunValleyBodyFont", "-size") == -21
    finally:
        root.destroy()


@pytest.mark.parametrize("error", [ImportError, RuntimeError, OSError])
def test_apply_theme_keeps_the_ttk_theme_when_sv_ttk_fails(monkeypatch, error):
    def fail():
        raise error("no sv_ttk")

    monkeypatch.setattr(gui, "_load_sv_ttk", fail)
    root = new_root()
    try:
        before = gui.theme_name(root)
        assert gui.apply_theme(root, "dark") is None
        assert gui.theme_name(root) == before
    finally:
        root.destroy()


def test_window_without_sv_ttk_has_the_old_look(monkeypatch, harness):
    monkeypatch.setattr(gui, "_load_sv_ttk", no_sv_ttk)
    h = harness()
    follower = gui.ThemeFollower(h.root, lambda: "dark")
    assert follower.theme is None
    assert follower._after_id is None
    assert str(h.app.zone.cget("relief")) == "groove"
    assert h.app.zone.cget("style") == ""


def test_main_without_sv_ttk_starts(monkeypatch, capsys):
    new_root().destroy()
    monkeypatch.setattr(gui, "_load_sv_ttk", no_sv_ttk)
    assert gui.main(["--smoke"]) == 0
    out = capsys.readouterr().out
    assert "pixheet-gui: theme " in out
    assert "sun-valley" not in out


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_main_smoke_shows_the_theme(monkeypatch, capsys, mode):
    pytest.importorskip("sv_ttk")
    new_root().destroy()
    monkeypatch.setenv(gui.THEME_ENV, mode)
    assert gui.main(["--smoke"]) == 0
    assert f"pixheet-gui: theme sun-valley-{mode}\n" in capsys.readouterr().out


def test_window_in_sun_valley_uses_the_card_and_the_accent_button():
    pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        gui.apply_theme(root, "light")
        app = gui.App(root, dnd_type=None)
        assert app.zone.cget("style") == "Card.TFrame"
        assert app.browse_button.cget("style") == "Accent.TButton"
        assert str(app.zone_label.cget("font")) == "SunValleySubtitleFont"
        app.dispose()
    finally:
        root.destroy()


class ThemeSource:
    """A fake system setting. It counts its reads."""

    def __init__(self, mode):
        self.mode = mode
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.mode


def test_follower_changes_the_theme_when_the_system_changes(monkeypatch):
    pytest.importorskip("sv_ttk")
    bars = []
    monkeypatch.setattr(gui, "set_title_bar", lambda root, dark: bars.append(dark))
    applied = []
    real_apply = gui.apply_theme
    monkeypatch.setattr(
        gui, "apply_theme", lambda root, mode: applied.append(mode) or real_apply(root, mode)
    )
    root = new_root()
    try:
        source = ThemeSource("light")
        follower = gui.ThemeFollower(root, source, interval=10_000)
        assert follower.theme == "sun-valley-light"
        follower.poll()  # no change: no new theme and no new title bar
        assert applied == ["light"]
        assert bars == []
        source.mode = "dark"
        follower.poll()
        assert applied == ["light", "dark"]
        assert bars == [True]
        assert follower.mode == "dark"
        assert follower.theme == "sun-valley-dark"
        assert gui.theme_name(root) == "sun-valley-dark"
        assert source.reads == 3
    finally:
        root.destroy()


def test_follower_reads_the_source_on_a_timer():
    pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        source = ThemeSource("light")
        follower = gui.ThemeFollower(root, source, interval=1)
        deadline = time.monotonic() + 10
        while source.reads < 3:
            assert time.monotonic() < deadline, "timeout"
            root.update()
            time.sleep(0.005)
        follower.stop()
        reads = source.reads
        for _ in range(3):
            time.sleep(0.01)
            root.update()
        assert source.reads == reads
    finally:
        root.destroy()


def test_follower_sets_the_title_bar_when_the_window_shows(monkeypatch):
    pytest.importorskip("sv_ttk")
    bars = []
    monkeypatch.setattr(gui, "set_title_bar", lambda root, dark: bars.append(dark))
    root = new_root()
    try:
        gui.ThemeFollower(root, ThemeSource("dark"), follow=False)
        frame = tk.Frame(root)
        frame.pack()
        root.update()
        assert bars == []  # the window is hidden, and a child does not count
        root.deiconify()
        root.update()
        assert bars == [True]
    finally:
        root.destroy()


def test_follower_stops_when_the_window_closes():
    pytest.importorskip("sv_ttk")
    root = new_root()
    follower = gui.ThemeFollower(root, ThemeSource("dark"), interval=10_000)
    assert follower._after_id is not None
    root.destroy()
    assert follower._after_id is None


def test_follower_with_a_fixed_theme_does_not_read_again():
    pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        source = ThemeSource("dark")
        follower = gui.ThemeFollower(root, source, follow=False)
        assert follower.theme == "sun-valley-dark"
        assert follower._after_id is None
        assert source.reads == 1
    finally:
        root.destroy()


def test_follow_theme_uses_the_environment(monkeypatch):
    pytest.importorskip("sv_ttk")
    monkeypatch.setenv(gui.THEME_ENV, "light")
    monkeypatch.setattr(gui, "system_theme", lambda: pytest.fail("reads the system"))
    root = new_root()
    try:
        follower = gui.follow_theme(root)
        assert follower.theme == "sun-valley-light"
        assert follower._after_id is None
    finally:
        root.destroy()


def test_set_title_bar_does_nothing_on_other_systems():
    assert not gui.set_title_bar(object(), dark=True, platform="linux")


def test_set_title_bar_does_not_raise_without_a_window():
    class NoWindow:
        def winfo_id(self):
            raise tk.TclError("bad window path name")

    assert not gui.set_title_bar(NoWindow(), dark=True, platform="win32")


@pytest.mark.skipif(sys.platform != "win32", reason="the title bar color is a Windows feature")
def test_set_title_bar_on_a_real_window():
    root = new_root()
    try:
        assert not gui.set_title_bar(root, dark=True)  # Tk did not show the window yet
        root.deiconify()
        root.update()
        assert gui.set_title_bar(root, dark=True)
        assert gui.set_title_bar(root, dark=False)
    finally:
        root.destroy()


# --- The size of the window ----------------------------------------------------------


def test_minimum_size_is_at_least_the_requested_size(harness):
    root = harness().root
    width, height = root.minsize()
    assert width >= max(gui.MIN_WIDTH, root.winfo_reqwidth())
    assert height >= max(gui.MIN_HEIGHT, root.winfo_reqheight())


@pytest.mark.parametrize("percent", [100, 125, 150])
@pytest.mark.parametrize("themed", [True, False])
def test_nothing_is_cut_off_at_the_minimum_size(percent, themed):
    if themed:
        pytest.importorskip("sv_ttk")
    root = new_root()
    try:
        root.tk.call("tk", "scaling", 96 * percent / 100 / 72)
        if themed:
            assert gui.apply_theme(root, "light") is not None
        app = gui.App(root, dnd_type=None)
        app._set_busy(True)  # the Cancel button shows too
        width, height = root.minsize()
        root.geometry(f"{width}x{height}")
        root.deiconify()
        root.update()
        for widget in (
            app.zone_label,
            app.browse_button,
            app.max_side_box,
            app.background_box,
            app.compact_check,
            app.progressbar,
            app.cancel_button,
            app.open_button,
        ):
            parent = widget.nametowidget(widget.winfo_parent())
            assert widget.winfo_width() >= widget.winfo_reqwidth(), widget
            assert widget.winfo_height() >= widget.winfo_reqheight(), widget
            assert widget.winfo_x() + widget.winfo_width() <= parent.winfo_width(), widget
            assert widget.winfo_y() + widget.winfo_height() <= parent.winfo_height(), widget
        app._set_busy(False)
        app.dispose()
    finally:
        root.destroy()


# --- The window icon -----------------------------------------------------------------

REPO_ICON = Path(__file__).resolve().parents[1] / "packaging" / gui.ICON_NAME


def test_icon_path_from_a_checkout():
    assert gui.icon_path() == REPO_ICON
    assert REPO_ICON.is_file()


def test_icon_path_of_a_built_app(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert gui.icon_path() is None
    (tmp_path / gui.ICON_NAME).write_bytes(REPO_ICON.read_bytes())
    assert gui.icon_path() == tmp_path / gui.ICON_NAME


def test_icon_path_of_an_installed_package(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "__file__", str(tmp_path / "site-packages" / "pixheet" / "gui.py"))
    assert gui.icon_path() is None


class IconRoot:
    """A root that records the calls of iconbitmap."""

    def __init__(self):
        self.calls = []

    def iconbitmap(self, **kwargs):
        self.calls.append(kwargs)


def test_set_icon_sets_the_default_icon_on_windows():
    root = IconRoot()
    assert gui.set_icon(root, platform="win32")
    assert root.calls == [{"default": str(REPO_ICON)}]


def test_set_icon_does_nothing_on_other_systems_or_without_the_file(monkeypatch):
    root = IconRoot()
    assert not gui.set_icon(root, platform="linux")
    monkeypatch.setattr(gui, "icon_path", lambda: None)
    assert not gui.set_icon(root, platform="win32")
    assert root.calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Tk reads .ico files only on Windows")
def test_set_icon_on_a_real_window(tmp_path, monkeypatch):
    root = new_root()
    try:
        assert gui.set_icon(root)
        # The file goes away after the check: Tk raises an error, and the app continues.
        monkeypatch.setattr(gui, "icon_path", lambda: tmp_path / gui.ICON_NAME)
        assert not gui.set_icon(root)
    finally:
        root.destroy()


def test_window_has_the_icon_on_windows(harness):
    assert harness().app.has_icon == (sys.platform == "win32")


def has_tkinterdnd2() -> bool:
    return importlib.util.find_spec("tkinterdnd2") is not None


def run_python(*args: str) -> subprocess.CompletedProcess[str]:
    """Run Python in a new process. Tk loads tkdnd reliably only in a new interpreter."""
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, timeout=60, check=False
    )


def test_python_m_pixheet_gui_smoke():
    new_root().destroy()
    result = run_python("-m", "pixheet.gui", "--smoke")
    assert result.returncode == 0, result.stderr
    state = "on" if has_tkinterdnd2() else "off"
    assert f"pixheet-gui: drag and drop {state}" in result.stdout


REGISTER_DROP = """
import tkinter as tk
from pixheet import gui

root = tk.Tk()
root.withdraw()
app = gui.App(root, dnd_type=gui.enable_dnd(root))
print(app.dnd)
print(repr(app.status_var.get()))
print(app.or_label.winfo_manager())
for widget in (app.body, app.zone):
    print("on_drop" in root.tk.call("bind", widget, "<<Drop>>"))
root.destroy()
"""


def test_real_tkdnd_registers_the_drop_zone():
    if not has_tkinterdnd2():
        pytest.skip("tkinterdnd2 is not installed")
    new_root().destroy()
    result = run_python("-c", REGISTER_DROP)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "''", "grid", "True", "True"]
