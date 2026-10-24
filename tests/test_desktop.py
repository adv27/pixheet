"""Tests of the desktop services without Tk: pixheet._desktop.

The tests run the code of each system with a fake subprocess, so that each system runs
all tests. No test starts a file manager or a D-Bus call.
"""

import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from pixheet import _desktop

# --- The environment of a child process ----------------------------------------------


def test_child_env_puts_back_the_library_path_of_the_user(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI1", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "A": "1"}
    assert _desktop.child_env(env) == {"LD_LIBRARY_PATH": "/opt/lib", "A": "1"}
    assert _desktop.child_env({"LD_LIBRARY_PATH": "/tmp/_MEI1", "A": "1"}) == {"A": "1"}


def test_child_env_keeps_the_library_path_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    env = {"LD_LIBRARY_PATH": "/opt/lib"}
    assert _desktop.child_env(env) == env
    assert _desktop.child_env() == dict(os.environ)


# --- Open and show files -------------------------------------------------------------


def test_file_uri_encodes_each_special_byte():
    uri = _desktop.file_uri("/home/me/a b/ảnh's,x~1.xlsx")
    assert uri == "file:///home/me/a%20b/%E1%BA%A3nh%27s%2Cx~1.xlsx"


class FakePopen:
    """Record the commands of subprocess.Popen, and end at once."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return types.SimpleNamespace(wait=lambda: 0)

    @property
    def commands(self):
        return [command for command, _kwargs in self.calls]


@pytest.fixture
def popen(monkeypatch):
    fake = FakePopen()
    monkeypatch.setattr(_desktop.subprocess, "Popen", fake)
    return fake


@pytest.mark.parametrize(("platform", "opener"), [("linux", "xdg-open"), ("darwin", "open")])
def test_open_path_uses_the_opener_of_the_system(tmp_path, popen, platform, opener):
    _desktop.open_path(tmp_path / "a.xlsx", platform=platform)
    [(command, kwargs)] = popen.calls
    assert command == [opener, str(tmp_path / "a.xlsx")]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "LD_LIBRARY_PATH_ORIG" not in kwargs["env"]


def test_open_path_on_windows_uses_the_file_association(tmp_path, monkeypatch, popen):
    opened = []
    monkeypatch.setattr(_desktop.os, "startfile", opened.append, raising=False)
    _desktop.open_path(tmp_path / "a.xlsx", platform="win32")
    assert opened == [tmp_path / "a.xlsx"]
    assert popen.calls == []


def test_open_folder_opens_the_parent_or_selects_the_file(tmp_path, popen):
    _desktop.open_folder(tmp_path / "a.xlsx", platform="darwin")
    _desktop.open_folder(tmp_path / "a.xlsx", platform="darwin", select=True)
    assert popen.commands == [
        ["open", str(tmp_path)],
        ["open", "-R", str(tmp_path / "a.xlsx")],
    ]


def test_show_in_folder_on_windows_selects_the_file(tmp_path, popen, monkeypatch):
    monkeypatch.setattr(_desktop, "windows_folder", lambda environ=None: r"C:\Windows")
    _desktop.show_in_folder(tmp_path / "a b.xlsx", platform="win32")
    # The full path: Windows does not look for an explorer.exe next to the app.
    explorer = r'"C:\Windows\explorer.exe"'
    assert popen.commands == [f'{explorer} /select,"{tmp_path / "a b.xlsx"}"']


def test_windows_folder_without_the_api_reads_the_environment(monkeypatch):
    monkeypatch.setattr(_desktop, "_system_windows_folder", lambda: None)
    assert _desktop.windows_folder({"SystemRoot": r"D:\Win"}) == r"D:\Win"
    assert _desktop.windows_folder({"WINDIR": r"E:\Win"}) == r"E:\Win"
    # A relative value can name a folder that an attacker controls.
    assert _desktop.windows_folder({"SystemRoot": "Windows"}) == r"C:\Windows"
    assert _desktop.windows_folder({}) == r"C:\Windows"


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_windows_folder_holds_explorer():
    folder = _desktop.windows_folder({})
    assert Path(folder, "explorer.exe").is_file()
    assert _desktop.windows_folder({"SystemRoot": r"D:\Win"}) == folder  # the API wins


def test_show_in_folder_on_linux_runs_on_a_worker_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(_desktop.shutil, "which", lambda name: f"/usr/bin/{name}")
    shown = []
    done = threading.Event()

    def show(path):
        shown.append((path, threading.current_thread().name))
        done.set()
        return "gdbus"

    monkeypatch.setattr(_desktop, "show_on_linux", show)
    _desktop.show_in_folder(tmp_path / "a.xlsx", platform="linux")
    assert done.wait(10)
    assert shown == [(str(tmp_path / "a.xlsx"), "pixheet-show")]


def test_show_in_folder_on_linux_without_a_program_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(_desktop.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="xdg-open"):  # the app shows it in a dialog
        _desktop.show_in_folder(tmp_path / "a.xlsx", platform="linux")


BUS = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}


class FakeRun:
    """Answer the commands of _output: an output for each program, or None."""

    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def __call__(self, command, timeout=_desktop.COMMAND_TIMEOUT):
        self.commands.append(command)
        return self.answers.get(command[0])


def test_show_on_linux_asks_the_file_manager_through_gdbus(monkeypatch, popen):
    run = FakeRun({"gdbus": "()\n"})
    monkeypatch.setattr(_desktop, "_output", run)
    assert _desktop.show_on_linux("/home/me/a b.xlsx", BUS) == "gdbus"
    [command] = run.commands
    assert command[:3] == ["gdbus", "call", "--session"]
    assert "org.freedesktop.FileManager1.ShowItems" in command
    assert command[-2:] == ["['file:///home/me/a%20b.xlsx']", ""]
    assert popen.calls == []


def test_show_on_linux_uses_dbus_send_without_gdbus(monkeypatch, popen):
    run = FakeRun({"dbus-send": "method return\n"})
    monkeypatch.setattr(_desktop, "_output", run)
    assert _desktop.show_on_linux("/home/me/a.xlsx", BUS) == "dbus-send"
    assert run.commands[-1][-2:] == ["array:string:file:///home/me/a.xlsx", "string:"]


def test_show_on_linux_opens_the_folder_when_dbus_fails(monkeypatch, popen):
    run = FakeRun({})
    monkeypatch.setattr(_desktop, "_output", run)
    assert _desktop.show_on_linux("/home/me/a.xlsx", BUS) == "xdg-open"
    assert len(run.commands) == 2
    assert popen.commands == [["xdg-open", "/home/me"]]


def test_show_on_linux_without_a_bus_opens_the_folder(monkeypatch, popen):
    monkeypatch.setattr(_desktop, "_output", lambda *args: pytest.fail("calls D-Bus"))
    assert _desktop.show_on_linux("/home/me/a.xlsx", {}) == "xdg-open"
    assert popen.commands == [["xdg-open", "/home/me"]]


def test_show_on_linux_without_xdg_open_does_not_raise(monkeypatch):
    def popen(command, **kwargs):
        raise FileNotFoundError(2, "No such file", command[0])

    monkeypatch.setattr(_desktop.subprocess, "Popen", popen)
    assert _desktop.show_on_linux("/home/me/a.xlsx", {}) == ""


def test_has_session_bus(tmp_path):
    assert _desktop.has_session_bus(BUS)
    assert not _desktop.has_session_bus({})
    assert not _desktop.has_session_bus({"XDG_RUNTIME_DIR": str(tmp_path)})
    (tmp_path / "bus").touch()
    assert _desktop.has_session_bus({"XDG_RUNTIME_DIR": str(tmp_path)})


def test_output_returns_none_for_each_failure():
    python = sys.executable
    assert _desktop._output([python, "-c", "print('prefer-dark')"]) == "prefer-dark\n"
    assert _desktop._output([python, "-c", "import sys; sys.exit(1)"]) is None
    assert _desktop._output([python, "-c", "import time; time.sleep(10)"], timeout=0.2) is None
    assert _desktop._output(["pixheet-no-such-program"]) is None


# --- The light or dark setting -------------------------------------------------------


class FakeDesktop:
    """Answer the commands of linux_theme. A key is the program and the last argument."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def __call__(self, command):
        key = f"{command[0]} {command[-1]}"
        self.asked.append(key)
        return self.answers.get(key)


@pytest.mark.parametrize(
    ("answers", "theme"),
    [
        # gdbus gives "(<<uint32 1>>,)" for Read, and "(<uint32 1>,)" for ReadOne.
        ({"gdbus color-scheme": "(<<uint32 1>>,)\n"}, "dark"),
        ({"gdbus color-scheme": "(<uint32 2>,)\n"}, "light"),
        ({"dbus-send string:color-scheme": "   variant       variant          uint32 1\n"}, "dark"),
        ({"gdbus color-scheme": "(<<uint32 0>>,)", "gsettings color-scheme": "'prefer-dark'"},
         "dark"),
        ({"gsettings color-scheme": "'prefer-light'\n"}, "light"),
        ({"gsettings color-scheme": "'default'", "gsettings gtk-theme": "'Yaru-dark'"}, "dark"),
        ({"gsettings gtk-theme": "'Adwaita'\n"}, "light"),
        ({"gdbus color-scheme": "(<<uint32 0>>,)"}, "light"),
        ({}, None),
    ],
)  # fmt: skip
def test_linux_theme_asks_the_portal_then_gnome(answers, theme):
    desktop = FakeDesktop(answers)
    assert _desktop.linux_theme(BUS, desktop) == theme


def test_linux_theme_without_a_bus_does_not_call_dbus():
    desktop = FakeDesktop({"gdbus color-scheme": "(<<uint32 1>>,)"})
    assert _desktop.linux_theme({}, desktop) is None
    assert desktop.asked == ["gsettings color-scheme", "gsettings gtk-theme"]


def test_linux_theme_asks_dbus_send_only_without_gdbus():
    desktop = FakeDesktop({"gdbus color-scheme": "(<<uint32 2>>,)"})
    assert _desktop.linux_theme(BUS, desktop) == "light"
    assert desktop.asked == ["gdbus color-scheme"]


def test_latest_value_reads_once_at_first_then_on_a_worker_thread():
    values = iter(["light", "dark"])
    threads = []
    gate = threading.Event()

    def read():
        threads.append(threading.current_thread().name)
        if len(threads) > 1:
            assert gate.wait(10)
        return next(values)

    latest = _desktop.LatestValue(read, name="pixheet-test")
    assert latest() == "light"
    assert threads == [threading.current_thread().name]
    assert latest() == "light"  # the last value, while the read on the worker waits
    assert latest() == "light"  # no second read while one runs
    gate.set()
    latest.join(10)
    assert threads[1:] == ["pixheet-test"]
    assert latest() == "dark"
    latest.join(10)  # the read raises StopIteration: keep the last value
    assert latest() == "dark"
    latest.join(10)


# --- Drag and drop -------------------------------------------------------------------


def fake_libraries(x11=True, xcb=True):
    """Return a fake ctypes.CDLL for libX11 and libxcb, and the list of the names it loads.

    ``x11`` and ``xcb`` are True for a new version, False for an old one, and None for a
    library that is not on the system.
    """
    exports = {
        "libX11.so.6": {"XSetIOErrorExitHandler": object()} if x11 else {},
        "libxcb.so.1": {"xcb_total_read": object()} if xcb else {},
    }
    found = {"libX11.so.6": x11 is not None, "libxcb.so.1": xcb is not None}
    names = []

    def load(library):
        names.append(library)
        if not found[library]:
            raise OSError(f"{library}: cannot open shared object file")
        return types.SimpleNamespace(**exports[library])

    return load, names


def test_x11_supports_tkdnd_needs_libx11_1_7_and_libxcb_1_14():
    load, names = fake_libraries()
    assert _desktop.x11_supports_tkdnd(load)
    assert names == ["libX11.so.6", "libxcb.so.1"]
    assert not _desktop.x11_supports_tkdnd(fake_libraries(x11=False)[0])
    # Ubuntu 18.04, Debian 10 and RHEL 8 have libX11 1.7 from the app, but libxcb 1.13.
    assert not _desktop.x11_supports_tkdnd(fake_libraries(xcb=False)[0])


@pytest.mark.parametrize(("x11", "xcb"), [(None, True), (True, None)])
def test_x11_supports_tkdnd_without_a_library(x11, xcb):
    assert not _desktop.x11_supports_tkdnd(fake_libraries(x11, xcb)[0])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs libX11")
def test_x11_supports_tkdnd_with_the_real_libx11():
    assert isinstance(_desktop.x11_supports_tkdnd(), bool)


def test_dnd_allowed():
    old, _names = fake_libraries(x11=False, xcb=False)
    assert _desktop.dnd_allowed("win32", {}, old)
    assert not _desktop.dnd_allowed("win32", {"PIXHEET_DND": "0"}, old)
    assert _desktop.dnd_allowed("win32", {"PIXHEET_DND": "1"}, old)
    assert not _desktop.dnd_allowed("linux", {}, old)
    assert not _desktop.dnd_allowed("linux", {}, fake_libraries(xcb=False)[0])
    new, _names = fake_libraries()
    assert _desktop.dnd_allowed("linux", {}, new)
    assert not _desktop.dnd_allowed("linux", {"PIXHEET_DND": " 0 "}, new)
