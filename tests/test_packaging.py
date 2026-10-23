"""Tests of the scripts in packaging/: the icon, the version resource, the spec, the Tcl and
Tk trim, the exe check, the release version, and the runner hook."""

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

import pixheet

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
release_version = load("release_version")


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


def test_committed_png_icon_is_the_output_of_make_icon(tmp_path):
    fresh = make_icon.write_png(tmp_path / "pixheet.png")
    with Image.open(make_icon.PNG) as committed, Image.open(fresh) as new:
        assert committed.format == new.format == "PNG"
        assert committed.size == (make_icon.PNG_SIZE, make_icon.PNG_SIZE)
        assert committed.convert("RGBA").tobytes() == new.convert("RGBA").tobytes()


def test_make_icon_writes_the_file_type_of_each_output(tmp_path, monkeypatch, capsys):
    ico, png = tmp_path / "a.ico", tmp_path / "a.PNG"
    monkeypatch.setattr(sys, "argv", ["make_icon.py", str(ico), str(png)])
    make_icon.main()
    with Image.open(ico) as first, Image.open(png) as second:
        assert (first.format, second.format) == ("ICO", "PNG")
    assert capsys.readouterr().out.split() == [str(ico), str(png)]


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


def test_run_exe_adds_the_environment_variables():
    script = "import os; print(os.environ['PIXHEET_THEME'], 'PATH' in os.environ)"
    code, output = check_exe.run_exe(
        [sys.executable, "-c", script], timeout=60, env={"PIXHEET_THEME": "dark"}
    )
    assert code == 0
    assert output.split() == ["dark", "True"]


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_theme_problems_accepts_the_sun_valley_theme(mode):
    output = f"pixheet-gui: drag and drop on\r\npixheet-gui: theme sun-valley-{mode}\r\n"
    assert check_exe.theme_problems(output, mode) == []


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ("pixheet-gui: drag and drop on\n", "The exe did not name its theme."),
        ("", "The exe did not name its theme."),
        ("pixheet-gui: theme vista\n", "The exe used the theme vista, not sun-valley-dark."),
        (
            "pixheet-gui: theme sun-valley-light\n",
            "The exe used the theme sun-valley-light, not sun-valley-dark.",
        ),
    ],
)
def test_theme_problems_refuses_a_fallback_theme(output, problem):
    [found] = check_exe.theme_problems(output, "dark")
    assert found.startswith(problem)
    if output:
        assert found.endswith(f"The output of the exe:\n{output.strip()}")
    else:
        assert found == problem


SMOKE_EXE = """
import os
print("pixheet-gui: drag and drop on")
print("pixheet-gui: theme sun-valley-" + os.environ["PIXHEET_THEME"])
"""


def test_check_smoke_runs_both_themes(monkeypatch, capsys):
    runs = []
    real_run_exe = check_exe.run_exe

    def run_exe(command, timeout, env=None):
        runs.append(env)
        return real_run_exe([sys.executable, "-c", SMOKE_EXE], timeout, env)

    monkeypatch.setattr(check_exe, "run_exe", run_exe)
    assert check_exe.check_smoke(Path("Pixheet.exe"), 60) == []
    assert runs == [{"PIXHEET_THEME": "light"}, {"PIXHEET_THEME": "dark"}]
    assert "sun-valley-dark" in capsys.readouterr().out


def test_check_smoke_fails_without_the_theme(monkeypatch):
    monkeypatch.setattr(
        check_exe, "run_exe", lambda command, timeout, env=None: (0, "pixheet-gui: theme vista\n")
    )
    [problem] = check_exe.check_smoke(Path("Pixheet.exe"), 60)
    assert problem.startswith("The exe used the theme vista, not sun-valley-light.")


def test_problem_includes_the_output_of_a_failed_exe():
    code, output = check_exe.run_exe([sys.executable, "-c", FAILED_EXE], timeout=60)
    assert code == 3
    [problem] = check_exe.result_problems(code, output, 60)
    assert problem.startswith("The exe stopped with exit code 3. The output of the exe:\n")
    assert "pixheet-gui: drag and drop on" in problem
    assert "pixheet-gui: cannot open a window" in problem


# A fake one-file exe: it extracts itself to a folder _MEI... in its temporary folder.
# With "hang", a child process keeps a file in that folder open, and both processes do
# not stop, like an exe that shows an error dialog.
MEI_EXE = """
import os, subprocess, sys, tempfile, time
from pathlib import Path
mei = Path(tempfile.gettempdir()) / "_MEI12345"
mei.mkdir()
(mei / "python315.dll").write_bytes(b"fake")
print("mei", mei, flush=True)
print("temp", os.environ["TEMP"], os.environ["TMP"], flush=True)
if sys.argv[1:] == ["hang"]:
    code = "import sys, time; f = open(sys.argv[1], 'rb'); time.sleep(120)"
    subprocess.Popen([sys.executable, "-c", code, str(mei / "python315.dll")])
    time.sleep(120)
"""


def mei_folder(output: str) -> Path:
    """Return the _MEI folder that MEI_EXE printed."""
    [line] = [line for line in output.splitlines() if line.startswith("mei ")]
    return Path(line.removeprefix("mei "))


def test_run_exe_gives_the_exe_its_own_temporary_folder():
    before = (os.environ.get("TEMP"), os.environ.get("TMP"))
    code, output = check_exe.run_exe([sys.executable, "-c", MEI_EXE], timeout=60)
    assert code == 0, output
    mei = mei_folder(output)
    temp = mei.parent
    assert temp.name.startswith("pixheet-exe-")
    [line] = [line for line in output.splitlines() if line.startswith("temp ")]
    assert line.split()[1:] == [str(temp), str(temp)]
    assert not temp.exists()
    assert (os.environ.get("TEMP"), os.environ.get("TMP")) == before


def test_run_exe_deletes_the_temporary_folder_of_a_stopped_exe():
    code, output = check_exe.run_exe([sys.executable, "-c", MEI_EXE, "hang"], timeout=5)
    assert code is None
    mei = mei_folder(output)
    assert mei.parent.name.startswith("pixheet-exe-")
    assert not mei.exists()
    assert not mei.parent.exists()


def test_remove_folder_tries_again_while_a_file_is_locked(tmp_path, monkeypatch):
    folder = tmp_path / "temp"
    folder.mkdir()
    calls = []
    real_rmtree = shutil.rmtree

    def rmtree(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("locked")
        real_rmtree(path)

    monkeypatch.setattr(check_exe.shutil, "rmtree", rmtree)
    check_exe.remove_folder(folder, wait=30)
    assert len(calls) == 3
    assert not folder.exists()


def test_remove_folder_stops_after_the_wait(tmp_path, monkeypatch):
    def rmtree(path):
        raise PermissionError("locked")

    monkeypatch.setattr(check_exe.shutil, "rmtree", rmtree)
    with pytest.raises(check_exe.CleanupError, match="cannot delete the folder"):
        check_exe.remove_folder(tmp_path, wait=0.3)


def test_remove_folder_accepts_a_missing_folder(tmp_path):
    check_exe.remove_folder(tmp_path / "missing", wait=0)


def test_main_reports_a_folder_that_it_cannot_delete(tmp_path, monkeypatch, capsys):
    exe = tmp_path / "Pixheet.exe"
    exe.write_bytes(b"MZ")

    def check(path, timeout=check_exe.TIMEOUT):
        raise check_exe.CleanupError("The script cannot delete the folder X: locked")

    monkeypatch.setattr(check_exe, "check", check)
    monkeypatch.setattr(sys, "argv", ["check_exe.py", str(exe)])
    assert check_exe.main() == 1
    assert "check_exe: The script cannot delete the folder X" in capsys.readouterr().err


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


def test_spec_requires_the_sun_valley_theme():
    assert "sv_ttk" in spec_value("REQUIRED_MODULES")
    files = spec_value("REQUIRED_FILES")
    assert "sv_ttk/sv.tcl" in files
    for mode in ("light", "dark"):
        assert f"sv_ttk/theme/{mode}.tcl" in files
        assert f"sv_ttk/theme/spritesheet_{mode}.png" in files


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


def test_build_script_makes_a_reproducible_build():
    """PYTHONHASHSEED and SOURCE_DATE_EPOCH are set for PyInstaller, but not for the check."""
    text = (PACKAGING / "build.ps1").read_text(encoding="utf-8")
    build = text.index("pyinstaller --noconfirm --clean")
    seed = text.index("$env:PYTHONHASHSEED = '0'")
    epoch = text.index('$env:SOURCE_DATE_EPOCH = "$epoch"')
    restore = text.index("$env:PYTHONHASHSEED = $oldHashSeed")
    check = text.index(r"python packaging\check_exe.py")
    assert seed < build < restore < check
    assert epoch < build < text.index("$env:SOURCE_DATE_EPOCH = $oldEpoch") < check
    assert "git -C $root log -1 --format=%ct" in text


def imports(path: Path) -> set[str]:
    """Return the names of the imports in the Python file ``path``.

    A relative import, for example ``from . import cli``, gives a name in the package
    ``pixheet``, for example ``pixheet.cli``. The files in src/pixheet are all at the top
    of the package.
    """
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module
            else:
                module = ".".join(filter(None, ["pixheet", node.module]))
            if module:
                names.add(module)
                names |= {f"{module}.{alias.name}" for alias in node.names}
    return names


def test_imports_finds_absolute_and_relative_imports(tmp_path):
    path = tmp_path / "x.py"
    path.write_text(
        "import os.path\nfrom zipfile import ZipFile\n"
        "from . import cli\nfrom .core import convert\n",
        encoding="utf-8",
    )
    assert imports(path) == {
        "os.path",
        "zipfile",
        "zipfile.ZipFile",
        "pixheet",
        "pixheet.cli",
        "pixheet.core",
        "pixheet.core.convert",
    }


def test_only_the_main_module_imports_the_cli():
    """The CLI imports argparse, and the exe does not have argparse.

    Thus a module that the GUI can load must not import pixheet.cli.
    """
    files = [p for p in sorted(SOURCE.glob("*.py")) if p.name not in ("cli.py", "__main__.py")]
    files.append(PACKAGING / "pixheet_gui.py")
    found = {
        f"{path.name}: {name}"
        for path in files
        for name in imports(path)
        if excluded(name, ["pixheet.cli"])
    }
    assert found == set()
    assert "pixheet.cli" in imports(SOURCE / "__main__.py")


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


def test_trim_of_a_trimmed_dll_changes_nothing(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll())
    first = tmp_path / "first" / "tcl90.dll"
    second = tmp_path / "second" / "tcl90.dll"
    trim_tcltk.trim(source, first)
    numbers = trim_tcltk.trim(first, second)
    assert numbers["removed"] == 0
    assert numbers["before"] == numbers["after"]
    assert second.read_bytes() == first.read_bytes()


def test_trim_gives_the_same_bytes_each_time(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll())
    trim_tcltk.trim(source, tmp_path / "a.dll")
    trim_tcltk.trim(source, tmp_path / "b.dll")
    assert (tmp_path / "a.dll").read_bytes() == (tmp_path / "b.dll").read_bytes()


def test_trim_stops_for_a_damaged_archive(tmp_path):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll()[:-20])  # This cuts the end of the central directory.
    with pytest.raises(SystemExit, match=r"^trim_tcltk: the zip archive in .* is damaged"):
        trim_tcltk.trim(source, tmp_path / "out.dll")
    assert not (tmp_path / "out.dll").exists()


def test_trim_stops_for_a_missing_file(tmp_path):
    with pytest.raises(SystemExit, match=r"^trim_tcltk: cannot read .*missing\.dll"):
        trim_tcltk.trim(tmp_path / "missing.dll", tmp_path / "out.dll")


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04" + bytes(200),
        b"MZ" + bytes(0x200),
        b"MZ" + bytes(30),  # shorter than the DOS header
        fake_dll()[: 0x80 + 24 + 20],  # cut in the optional header
        fake_dll()[: 0x80 + 24 + 240 + 10],  # cut in the section table
    ],
)
def test_trim_stops_for_a_file_that_is_not_a_pe_file(tmp_path, data):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(data)
    with pytest.raises(SystemExit, match="is not a PE file"):
        trim_tcltk.trim(source, tmp_path / "out.dll")
    assert not (tmp_path / "out.dll").exists()


def test_trim_command_refuses_to_change_the_source_dll(tmp_path, monkeypatch):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="must not be the source DLL"):
        trim_tcltk.main(source, Path("tcl90.dll"))
    assert source.read_bytes() == fake_dll()


def test_trim_command_shows_the_numbers(tmp_path, capsys):
    source = tmp_path / "tcl90.dll"
    source.write_bytes(fake_dll())
    trim_tcltk.main(source, tmp_path / "out.dll")
    assert "'removed': 5" in capsys.readouterr().out
    assert (tmp_path / "out.dll").exists()


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


# The format check of the exe


def test_format_images_have_the_formats_and_modes_of_the_check(tmp_path):
    expected, problems = check_exe.make_format_images(tmp_path / check_exe.FORMATS_FOLDER)
    assert problems == []
    assert len(expected) == len(check_exe.format_cases()) == 11
    assert len({path.stem for path in expected}) == len(expected)
    found = {}
    for path in expected:
        with Image.open(path) as image:
            found[path.name] = (image.format, image.mode)
            if path.name == "exif rotated jpg.jpg":
                assert image.getexif()[0x0112] == 6
            if path.name == "cmyk icc jpg.jpg":
                assert image.info["icc_profile"]
    assert found == {
        "rgba png.png": ("PNG", "RGBA"),
        "palette transparent png.png": ("PNG", "P"),
        "i16 png.png": ("PNG", "I;16"),
        "cmyk icc jpg.jpg": ("JPEG", "CMYK"),
        "exif rotated jpg.jpg": ("JPEG", "RGB"),
        "rgba webp.webp": ("WEBP", "RGBA"),
        "rgba avif.avif": ("AVIF", "RGBA"),
        "palette gif.gif": ("GIF", "P"),
        "lzw tif.tif": ("TIFF", "RGB"),
        "lab tif.tif": ("TIFF", "LAB"),
        "rgb bmp.bmp": ("BMP", "RGB"),
    }


def test_output_problems_finds_a_missing_or_different_sheet(tmp_path):
    expected, problems = check_exe.make_format_images(tmp_path)
    assert problems == []
    for path in expected:
        pixheet.convert(path, path.with_suffix(".xlsx"))
    assert check_exe.output_problems(expected) == []

    paths = list(expected)
    paths[0].with_suffix(".xlsx").unlink()
    pixheet.convert(paths[2], paths[1].with_suffix(".xlsx"))
    assert check_exe.output_problems(expected) == [
        f"The exe did not write {paths[0].stem}.xlsx.",
        f"The exe wrote a different xl/worksheets/sheet1.xml, xl/styles.xml for {paths[1].name}.",
    ]


def write_pyproject(path: Path, version: str) -> Path:
    path.write_text(f'[project]\nname = "pixheet"\nversion = "{version}"\n', encoding="utf-8")
    return path


def test_read_version_reads_pyproject(tmp_path):
    assert release_version.read_version(write_pyproject(tmp_path / "p.toml", "1.2.3")) == "1.2.3"
    with (PACKAGING.parent / "pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert release_version.read_version(PACKAGING.parent / "pyproject.toml") == version


def test_pr_problems_ignores_a_pull_request_that_keeps_the_version():
    def tag_exists(tag):
        raise AssertionError("The check must not ask for the tag.")

    assert release_version.pr_problems("0.1.0", "0.1.0", tag_exists) == []


def test_pr_problems_accepts_a_new_version_without_a_tag():
    asked = []
    assert release_version.pr_problems("0.1.0", "0.2.0", lambda t: asked.append(t)) == []
    assert asked == ["v0.2.0"]


def test_pr_problems_refuses_a_new_version_with_a_tag():
    [problem] = release_version.pr_problems("0.2.0", "0.1.0", lambda tag: tag == "v0.1.0")
    assert "from 0.2.0 to 0.1.0" in problem
    assert "the tag v0.1.0 exists already" in problem


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@needs_git
def test_remote_tag_exists_asks_the_remote(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git("init", "-q", cwd=remote)
    git("commit", "-q", "--allow-empty", "-m", "first", cwd=remote)
    git("tag", "v0.1.0", cwd=remote)
    git("tag", "-a", "v0.2.0", "-m", "annotated", cwd=remote)
    assert release_version.remote_tag_exists("v0.1.0", str(remote))
    assert release_version.remote_tag_exists("v0.2.0", str(remote))
    # The pattern must not find a tag that only ends with the same text.
    assert not release_version.remote_tag_exists("0.1.0", str(remote))
    assert not release_version.remote_tag_exists("v0.1", str(remote))
    assert not release_version.remote_tag_exists("v0.3.0", str(remote))


@needs_git
def test_remote_tag_exists_stops_for_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="git ls-remote stopped with exit code"):
        release_version.remote_tag_exists("v0.1.0", str(tmp_path / "no remote"))


def test_release_version_command_prints_the_version(tmp_path, capsys):
    pyproject = write_pyproject(tmp_path / "pyproject.toml", "3.4.5")
    assert release_version.main(["version", str(pyproject)]) == 0
    assert capsys.readouterr().out == "3.4.5\n"


@pytest.mark.parametrize(
    ("base", "head", "tags", "code", "text"),
    [
        ("0.1.0", "0.1.0", {"v0.1.0"}, 0, "does not change the version 0.1.0"),
        ("0.1.0", "0.2.0", {"v0.1.0"}, 0, "the tag v0.2.0 does not exist"),
        ("0.1.0", "0.2.0", {"v0.1.0", "v0.2.0"}, 1, "::error::"),
    ],
)
def test_check_pr_command(tmp_path, monkeypatch, capsys, base, head, tags, code, text):
    monkeypatch.setattr(release_version, "remote_tag_exists", lambda tag: tag in tags)
    base_file = write_pyproject(tmp_path / "base.toml", base)
    head_file = write_pyproject(tmp_path / "head.toml", head)
    assert release_version.main(["check-pr", str(base_file), str(head_file)]) == code
    assert text in capsys.readouterr().out


def test_workflows_use_the_release_version_script():
    workflows = PACKAGING.parent / ".github" / "workflows"
    ci = (workflows / "ci.yml").read_text(encoding="utf-8")
    exe = (workflows / "windows-exe.yml").read_text(encoding="utf-8")
    assert "packaging/release_version.py check-pr" in ci
    assert "packaging/release_version.py version" in exe


def test_release_version_script_runs_on_python_3_12():
    """ci.yml runs the script with the Python 3.12 of the Ubuntu runner."""
    source = (PACKAGING / "release_version.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 12))
