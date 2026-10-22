"""Tests of the scripts in packaging/: the icon, the version resource, the spec, the Tcl and
Tk trim, and the runner hook."""

import ast
import importlib.util
import io
import os
import shutil
import struct
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import pytest
from PIL import Image

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
SPEC = PACKAGING / "pixheet.spec"
SOURCE = PACKAGING.parent / "src" / "pixheet"


def load(name: str):
    """Import a module from packaging/, which is not a package."""
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icon = load("make_icon")
version_info = load("version_info")
check_exe = load("check_exe")
trim_tcltk = load("trim_tcltk")


def frames(path: Path) -> dict[int, bytes]:
    """Return the RGBA pixels of each size in an .ico file."""
    with Image.open(path) as icon:
        result = {}
        for width, height in sorted(icon.info["sizes"]):
            icon.size = (width, height)
            icon.load()
            result[width] = icon.convert("RGBA").tobytes()
        return result


def test_committed_icon_is_the_output_of_make_icon(tmp_path):
    fresh = make_icon.write(tmp_path / "pixheet.ico")
    assert frames(make_icon.ICON) == frames(fresh)
    assert sorted(frames(fresh)) == list(make_icon.SIZES)


@pytest.mark.parametrize("size", make_icon.SIZES)
def test_icon_cells_have_the_picture_colors(size):
    image = make_icon.draw(size)
    cell = size // make_icon.GRID
    for row, letters in enumerate(make_icon.PICTURE):
        for column, letter in enumerate(letters):
            center = (column * cell + cell // 2, row * cell + cell // 2)
            assert image.getpixel(center) == (*make_icon.PALETTE[letter], 255)


def test_icon_has_grid_lines_only_from_48_pixels():
    line = (*make_icon.LINE, 255)
    small = make_icon.draw(32)
    assert line not in {color for _count, color in small.getcolors(32 * 32)}
    assert make_icon.draw(48).getpixel((6, 20)) == line


def test_draw_refuses_a_size_that_does_not_fit_the_grid():
    with pytest.raises(ValueError, match="multiple of 8"):
        make_icon.draw(20)


@pytest.mark.parametrize(
    ("version", "numbers"),
    [
        ("0.1.0", (0, 1, 0, 0)),
        ("1", (1, 0, 0, 0)),
        ("2.3.4.5.6", (2, 3, 4, 5)),
        ("1.2.0rc1", (1, 2, 0, 0)),
        ("0.2.0.dev3", (0, 2, 0, 0)),
        ("1.0+local.7", (1, 0, 0, 0)),
        ("v1.4", (1, 4, 0, 0)),
    ],
)
def test_numeric_version(version, numbers):
    assert version_info.numeric_version(version) == numbers


@pytest.mark.parametrize("version", ["", "dev", "1.70000"])
def test_numeric_version_refuses_bad_text(version):
    with pytest.raises(ValueError, match="version"):
        version_info.numeric_version(version)


def test_metadata_comes_from_pyproject():
    with (PACKAGING.parent / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    metadata = version_info.read_metadata()
    strings = metadata.strings("Pixheet.exe")
    assert metadata.version == project["version"]
    assert strings["ProductName"] == "Pixheet"
    assert strings["FileVersion"] == strings["ProductVersion"] == project["version"]
    assert strings["CompanyName"] == "Dinh-Anh Vu"
    assert strings["LegalCopyright"].startswith("Copyright (c) ")
    assert strings["LegalCopyright"].endswith("Dinh-Anh Vu")
    assert "License: MIT." in strings["Comments"]
    assert strings["OriginalFilename"] == "Pixheet.exe"


def test_spec_is_not_ignored_by_git():
    gitignore = (PACKAGING.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "!packaging/*.spec" in gitignore
    assert gitignore.index("!packaging/*.spec") > gitignore.index("*.spec")


HOOK = PACKAGING / "runner_job_started.ps1"
FOLDER = "adv27/pixheet/.github/workflows"
WORKFLOW = f"{FOLDER}/windows-exe.yml"


def run_hook(**variables: str) -> subprocess.CompletedProcess[str]:
    """Run the job-started hook of the runner with these GitHub variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    env |= variables
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


needs_powershell = pytest.mark.skipif(
    shutil.which("powershell") is None, reason="needs Windows PowerShell"
)


@needs_powershell
@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("push", f"{WORKFLOW}@refs/heads/master"),
        ("push", f"{WORKFLOW}@refs/tags/v0.2.0"),
        ("workflow_dispatch", f"{WORKFLOW}@refs/heads/master"),
        ("workflow_dispatch", f"{WORKFLOW}@refs/tags/v1.0.0"),
    ],
)
def test_runner_hook_runs_a_trusted_job(event, ref):
    result = run_hook(
        GITHUB_REPOSITORY="adv27/pixheet", GITHUB_EVENT_NAME=event, GITHUB_WORKFLOW_REF=ref
    )
    assert result.returncode == 0, result.stdout
    assert "run the job" in result.stdout


@needs_powershell
@pytest.mark.parametrize(
    ("repository", "event", "ref"),
    [
        # A fork PR that adds a pull_request trigger to windows-exe.yml.
        ("adv27/pixheet", "pull_request", f"{WORKFLOW}@refs/pull/7/merge"),
        ("adv27/pixheet", "pull_request_target", f"{WORKFLOW}@refs/heads/master"),
        # A fork PR that changes ci.yml, or adds a workflow, to use the runner labels.
        ("adv27/pixheet", "pull_request", f"{FOLDER}/ci.yml@refs/pull/7/merge"),
        ("adv27/pixheet", "push", f"{FOLDER}/ci.yml@refs/heads/master"),
        ("adv27/pixheet", "push", f"{FOLDER}/evil.yml@refs/heads/master"),
        # The trusted workflow from a branch that is not master, or from a tag without "v".
        ("adv27/pixheet", "push", f"{WORKFLOW}@refs/heads/feature"),
        ("adv27/pixheet", "workflow_dispatch", f"{WORKFLOW}@refs/heads/Master"),
        ("adv27/pixheet", "push", f"{WORKFLOW}@refs/tags/0.2.0"),
        ("adv27/pixheet", "schedule", f"{WORKFLOW}@refs/heads/master"),
        # Another repository, and a job without the variables.
        ("someone/pixheet", "push", f"{WORKFLOW}@refs/heads/master"),
        ("", "", ""),
    ],
)
def test_runner_hook_refuses_an_untrusted_job(repository, event, ref):
    variables = {
        "GITHUB_REPOSITORY": repository,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_WORKFLOW_REF": ref,
    }
    result = run_hook(**{k: v for k, v in variables.items() if v})
    assert result.returncode == 1, result.stdout
    assert "refuse the job" in result.stdout


def test_run_exe_returns_the_exit_code_and_the_output():
    code, output = check_exe.run_exe([sys.executable, "-c", "print('converted')"], timeout=60)
    assert code == 0
    assert "converted" in output


# A one-file exe that shows an error dialog: a parent process that waits for a child
# process that does not stop. The child keeps the output pipe open.
HUNG_EXE = """
import subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"], stdout=sys.stdout, stderr=sys.stderr
)
print("started", flush=True)
time.sleep(120)
"""


def test_run_exe_stops_the_whole_process_tree_after_the_timeout():
    start = time.monotonic()
    code, output = check_exe.run_exe([sys.executable, "-c", HUNG_EXE], timeout=5)
    seconds = time.monotonic() - start
    assert code is None
    assert "started" in output
    # If the child still ran, its open pipe would block run_exe for KILL_WAIT more seconds.
    assert seconds < 5 + check_exe.KILL_WAIT / 2


def test_result_problems():
    assert check_exe.result_problems(0, "pixheet-gui: drag and drop on\n", 120) == []
    assert check_exe.result_problems(1, "", 120) == ["The exe stopped with exit code 1."]
    assert check_exe.result_problems(None, "  \n", 120) == [
        "The exe did not stop in 120 s. The script stopped it."
    ]


FAILED_EXE = """
import sys
print("pixheet-gui: drag and drop on")
print("pixheet-gui: cannot open a window", file=sys.stderr)
sys.exit(3)
"""


def test_problem_includes_the_output_of_a_failed_exe():
    code, output = check_exe.run_exe([sys.executable, "-c", FAILED_EXE], timeout=60)
    assert code == 3
    [problem] = check_exe.result_problems(code, output, 60)
    assert problem.startswith("The exe stopped with exit code 3. The output of the exe:\n")
    assert "pixheet-gui: drag and drop on" in problem
    assert "pixheet-gui: cannot open a window" in problem


# The spec


def spec_value(name: str):
    """Return the value of the list ``name`` in the spec."""
    for node in ast.parse(SPEC.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"The spec has no {name}.")


def spec_keywords(name: str) -> list:
    """Return the values of each keyword argument ``name`` in the calls of the spec."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    return [
        ast.literal_eval(keyword.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == name
    ]


def excluded(module: str, excludes: list[str]) -> bool:
    """Return True if ``module`` is in ``excludes``, or in a package in ``excludes``."""
    return any(f"{module}.".startswith(f"{name}.") for name in excludes)


def test_spec_excludes_the_unused_modules():
    excludes = spec_value("EXCLUDES")
    assert {
        "numpy",
        "multiprocessing",
        "concurrent.futures.process",
        "ssl",
        "_ssl",
        "hashlib",
        "_hashlib",
        "urllib",
        "http",
        "email",
        "xml",
        "decimal",
        "lzma",
        "bz2",
        "compression.zstd",
        "argparse",
        "PIL.ImageTk",
        "PIL.ImageQt",
        "PIL.ImageShow",
        "PIL.ImageWin",
        "PIL.ImageFilter",
        "PIL.PdfImagePlugin",
        "PIL.PalmImagePlugin",
    } <= set(excludes)
    assert len(excludes) == len(set(excludes))
    # gzip needs compression. LAB images need ImageCms.
    for module in ("compression", "gzip", "PIL.ImageCms", "PIL._imagingcms", "zipfile", "zlib"):
        assert not excluded(module, excludes), module


def test_spec_does_not_exclude_a_required_module():
    excludes = spec_value("EXCLUDES")
    required = spec_value("REQUIRED_MODULES")
    assert {"PIL._avif", "PIL._webp", "PIL._imagingcms", "tkinterdnd2", "_tkinter"} <= set(required)
    assert [module for module in required if excluded(module, excludes)] == []


def test_spec_guards_the_excludes():
    unwanted = set(spec_value("UNWANTED_MODULES"))
    assert {"ssl", "_hashlib", "multiprocessing", "numpy", "tkinter.test", "test"} <= unwanted
    assert unwanted.isdisjoint(spec_value("REQUIRED_MODULES"))
    assert set(spec_value("UNWANTED_FILES")) == {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}
    assert "tkinterdnd2/tkdnd/win-x64-*/libtkdnd*.dll" in spec_value("REQUIRED_FILES")


def test_spec_optimizes_the_bytecode_and_does_not_use_upx_or_strip():
    assert spec_keywords("optimize") == [2]
    upx = spec_keywords("upx")
    assert upx == [False] * len(upx)
    assert len(upx) == 2  # the EXE options and COLLECT
    strip = spec_keywords("strip")
    assert strip == [False] * len(strip)
    assert len(strip) == 2


def test_spec_trims_tcl_and_tk_only_on_windows():
    text = SPEC.read_text(encoding="utf-8")
    assert 'if sys.platform == "win32":\n    a.binaries = trim_binaries(a.binaries, ' in text
    assert text.index("trim_binaries(a.binaries") < text.index("pyz = PYZ(a.pure)")


def imports(path: Path) -> set[str]:
    """Return the names of the absolute imports in the Python file ``path``."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


def test_app_code_does_not_import_an_excluded_module():
    """The exe does not have the modules in EXCLUDES. Only cli.py and __main__.py can use them."""
    excludes = spec_value("EXCLUDES")
    files = [p for p in sorted(SOURCE.glob("*.py")) if p.name not in ("cli.py", "__main__.py")]
    assert SOURCE / "gui.py" in files
    found = {
        f"{path.name}: {name}"
        for path in files
        for name in imports(path)
        if excluded(name, excludes)
    }
    assert found == set()


# The Tcl and Tk trim


@pytest.mark.parametrize(
    ("name", "dropped"),
    [
        ("tcl_library/tzdata/Europe/Paris", True),
        ("tcl_library/msgs/de.msg", True),
        ("tcl_library/http/http.tcl", True),
        ("tcl_library/tcltest/tcltest.tcl", True),
        ("tcl_library/opt/optparse.tcl", True),
        ("tcl_library/encoding/cp932.enc", True),
        ("tcl_library/encoding/cp1252.enc", False),
        ("tcl_library/encoding/cp1258.enc", False),
        ("tcl_library/init.tcl", False),
        ("tcl_library/clock.tcl", False),
        ("tcl_library/msgcat/msgcat.tcl", False),
        ("tk_library/images/logo.gif", True),
        ("tk_library/msgs/de.msg", True),
        ("tk_library/msgs/en.msg", False),
        ("tk_library/console.tcl", True),
        ("tk_library/tkfbox.tcl", True),
        ("tk_library/tk.tcl", False),
        ("tk_library/button.tcl", False),
        ("tk_library/ttk/vistaTheme.tcl", False),
        ("tk_library/ttk/aquaTheme.tcl", True),
    ],
)
def test_trim_drops_only_unused_files(name, dropped):
    assert trim_tcltk.dropped(name) is dropped


TCL_PKG_INDEX = """\
    0 http            2.10.2 {http http.tcl}
    1 opt             0.4.10 {opt optparse.tcl}
    0 msgcat          1.7.1  {msgcat msgcat.tcl}
"""
TK_INDEX = """\
set auto_index(::tk::ConsoleInit) [list source [file join $dir console.tcl]]
set auto_index(::tk::ButtonEnter) [list source [file join $dir button.tcl]]
"""
ARCHIVE = {
    "tcl_library/init.tcl": "# init\n",
    "tcl_library/pkgIndex.tcl": TCL_PKG_INDEX,
    "tcl_library/http/http.tcl": "# http\n",
    "tcl_library/opt/optparse.tcl": "# opt\n",
    "tcl_library/msgcat/msgcat.tcl": "# msgcat\n",
    "tcl_library/tzdata/Europe/Paris": "# zone\n",
    "tcl_library/encoding/cp1252.enc": "# cp1252\n",
    "tcl_library/encoding/cp932.enc": "# cp932\n" * 100,
    "tk_library/tclIndex": TK_INDEX,
    "tk_library/console.tcl": "# console\n",
    "tk_library/button.tcl": "# button\n",
}
IMAGE_END = 0x400
CHECKSUM_OFFSET = 0x80 + 24 + 64


def fake_dll(archive: dict[str, str] | None = ARCHIVE, signed: bool = False) -> bytes:
    """Return a small PE32+ file with one section, followed by a zip archive.

    The layout is the layout of tcl90.dll: the headers, the section data up to
    IMAGE_END, and then the archive.
    """
    data = bytearray(IMAGE_END)
    data[:2] = b"MZ"
    header = 0x80
    struct.pack_into("<I", data, 0x3C, header)
    data[header : header + 4] = b"PE\0\0"
    struct.pack_into("<HH", data, header + 4, 0x8664, 1)  # the machine, 1 section
    struct.pack_into("<H", data, header + 20, 240)  # the size of the optional header
    optional = header + 24
    struct.pack_into("<H", data, optional, 0x20B)  # PE32+
    struct.pack_into("<I", data, CHECKSUM_OFFSET, 0x12345678)  # an incorrect checksum
    struct.pack_into("<I", data, optional + 108, 16)  # the number of data directories
    if signed:
        struct.pack_into("<II", data, optional + 112 + 8 * 4, 0x2000, 0x100)
    table = optional + 240
    struct.pack_into("<8sIIII", data, table, b".text", 0x200, 0x1000, 0x200, 0x200)
    data[0x200:IMAGE_END] = bytes(range(256)) * 2
    if archive is not None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for name, text in archive.items():
                z.writestr(name, text)
        data += buffer.getvalue()
    return bytes(data)


def reference_checksum(data: bytes, offset: int) -> int:
    """The PE checksum, word by word, as the Windows function CheckSumMappedFile does it."""
    total = 0
    for i in range(0, len(data), 2):
        if offset <= i < offset + 4:
            continue
        total += int.from_bytes(data[i : i + 2].ljust(2, b"\0"), "little")
        total = (total & 0xFFFF) + (total >> 16)
    return total + len(data)


def test_pe_layout_finds_the_end_of_the_image_and_the_checksum():
    assert trim_tcltk.pe_layout(fake_dll(), "x.dll") == (IMAGE_END, CHECKSUM_OFFSET)


@pytest.mark.parametrize("extra", [b"", b"\x01", b"\xff" * 333])
def test_pe_checksum_agrees_with_the_reference(extra):
    data = fake_dll() + extra
    assert trim_tcltk.pe_checksum(data, CHECKSUM_OFFSET) == reference_checksum(
        data, CHECKSUM_OFFSET
    )


def test_pe_checksum_agrees_with_pefile():
    pefile = pytest.importorskip("pefile")
    data = fake_dll()
    expected = pefile.PE(data=data, fast_load=True).generate_checksum()
    assert trim_tcltk.pe_checksum(data, CHECKSUM_OFFSET) == expected


def test_trim_keeps_the_image_and_removes_the_unused_files(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll())
    numbers = trim_tcltk.trim(source, tmp_path / "out" / "tcl90.dll")
    data = (tmp_path / "out" / "tcl90.dll").read_bytes()
    assert numbers == {"before": source.stat().st_size, "after": len(data), "kept": 6, "removed": 5}
    original = source.read_bytes()
    # Only the checksum field of the PE image changes, and the new checksum is correct.
    assert data[:CHECKSUM_OFFSET] == original[:CHECKSUM_OFFSET]
    assert data[CHECKSUM_OFFSET + 4 : IMAGE_END] == original[CHECKSUM_OFFSET + 4 : IMAGE_END]
    checksum = struct.unpack_from("<I", data, CHECKSUM_OFFSET)[0]
    assert checksum == reference_checksum(data, CHECKSUM_OFFSET)
    with zipfile.ZipFile(io.BytesIO(data[IMAGE_END:])) as z:
        assert z.testzip() is None
        assert sorted(z.namelist()) == [
            "tcl_library/encoding/cp1252.enc",
            "tcl_library/init.tcl",
            "tcl_library/msgcat/msgcat.tcl",
            "tcl_library/pkgIndex.tcl",
            "tk_library/button.tcl",
            "tk_library/tclIndex",
        ]
        assert {info.compress_type for info in z.infolist()} == {zipfile.ZIP_DEFLATED}
        assert z.read("tcl_library/init.tcl") == b"# init\n"
        # The indexes no longer name the removed files.
        assert z.read("tcl_library/pkgIndex.tcl").decode() == (
            "    0 msgcat          1.7.1  {msgcat msgcat.tcl}\n"
        )
        assert z.read("tk_library/tclIndex").decode() == TK_INDEX.splitlines(keepends=True)[1]


def test_trim_stops_when_no_archive_follows_the_image(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll(archive=None) + b"\0" * 64)
    with pytest.raises(SystemExit, match="no zip archive follows the PE image"):
        trim_tcltk.trim(source, tmp_path / "out.dll")
    assert not (tmp_path / "out.dll").exists()


def test_trim_stops_for_a_signed_dll(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll(signed=True))
    with pytest.raises(SystemExit, match="has a code signature"):
        trim_tcltk.trim(source, tmp_path / "out.dll")


@pytest.mark.parametrize("data", [b"PK\x03\x04" + bytes(200), b"MZ" + bytes(0x200)])
def test_trim_stops_for_a_file_that_is_not_a_pe_file(tmp_path, data):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(data)
    with pytest.raises(SystemExit, match="is not a PE file"):
        trim_tcltk.trim(source, tmp_path / "out.dll")


def test_trim_binaries_replaces_the_tcl_and_tk_dlls(tmp_path, capsys):
    for name in ("tcl90.dll", "tcl9tk90.dll"):
        (tmp_path / name).write_bytes(fake_dll())
    binaries = [
        ("python315.dll", str(tmp_path / "python315.dll"), "BINARY"),
        ("TCL90.DLL", str(tmp_path / "tcl90.dll"), "BINARY"),
        ("tcl9tk90.dll", str(tmp_path / "tcl9tk90.dll"), "BINARY"),
    ]
    folder = tmp_path / "trim"
    result = trim_tcltk.trim_binaries(binaries, folder)
    assert result == [
        binaries[0],
        ("TCL90.DLL", str(folder / "TCL90.DLL"), "BINARY"),
        ("tcl9tk90.dll", str(folder / "tcl9tk90.dll"), "BINARY"),
    ]
    assert (folder / "tcl9tk90.dll").stat().st_size < (tmp_path / "tcl9tk90.dll").stat().st_size
    assert capsys.readouterr().out.count("5 files removed, 6 files kept") == 2


@pytest.mark.parametrize(
    "names", [[], ["tcl90.dll"], ["tcl90.dll", "tcl9tk90.dll", "sub/tcl90.dll"]]
)
def test_trim_binaries_stops_for_an_other_number_of_dlls(tmp_path, names):
    (tmp_path / "dll").write_bytes(fake_dll())
    binaries = [(name, str(tmp_path / "dll"), "BINARY") for name in names]
    with pytest.raises(SystemExit, match="expected the DLLs"):
        trim_tcltk.trim_binaries(binaries, tmp_path / "trim")
