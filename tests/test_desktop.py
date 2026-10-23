"""Tests of the desktop services without Tk: pixheet._desktop."""

import sys
import types

import pytest

from pixheet import _desktop

# --- Drag and drop -------------------------------------------------------------------


def test_x11_supports_tkdnd_needs_libx11_1_7():
    new = types.SimpleNamespace(XSetIOErrorExitHandler=object())
    old = types.SimpleNamespace()
    names = []

    def load(library):
        names.append(library)
        return new

    assert _desktop.x11_supports_tkdnd(load)
    assert names == ["libX11.so.6"]
    assert not _desktop.x11_supports_tkdnd(lambda library: old)


def test_x11_supports_tkdnd_without_libx11():
    def load(library):
        raise OSError(f"{library}: cannot open shared object file")

    assert not _desktop.x11_supports_tkdnd(load)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs libX11")
def test_x11_supports_tkdnd_with_the_real_libx11():
    assert isinstance(_desktop.x11_supports_tkdnd(), bool)


def test_dnd_allowed():
    old = lambda library: types.SimpleNamespace()  # noqa: E731
    assert _desktop.dnd_allowed("win32", {}, old)
    assert not _desktop.dnd_allowed("win32", {"PIXHEET_DND": "0"}, old)
    assert _desktop.dnd_allowed("win32", {"PIXHEET_DND": "1"}, old)
    assert not _desktop.dnd_allowed("linux", {}, old)
    new = lambda library: types.SimpleNamespace(XSetIOErrorExitHandler=1)  # noqa: E731
    assert _desktop.dnd_allowed("linux", {}, new)
    assert not _desktop.dnd_allowed("linux", {"PIXHEET_DND": " 0 "}, new)
