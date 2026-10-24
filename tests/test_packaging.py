"""Tests of the scripts in packaging/: the icon, the version resource, the spec, the Tcl and
Tk trim, the exe check, the release version, the release plan, and the runner hook."""

import ast
import importlib.util
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
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
linux_guards = load("linux_guards")
release_version = load("release_version")
release_plan = load("release_plan")
release_notes = load("release_notes")


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
    assert strings["Comments"].endswith("read THIRD-PARTY-NOTICES.txt.")
    assert strings["OriginalFilename"] == "Pixheet.exe"


def test_spec_is_not_ignored_by_git():
    gitignore = (PACKAGING.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "!packaging/*.spec" in gitignore
    assert gitignore.index("!packaging/*.spec") > gitignore.index("*.spec")


HOOK = PACKAGING / "runner_job_started.ps1"
FOLDER = "adv27/pixheet/.github/workflows"
WORKFLOW = f"{FOLDER}/release.yml"


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
        # A fork PR that adds a pull_request trigger to release.yml.
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


def test_run_exe_starts_the_app_in_the_hidden_mode():
    """Each run of the app keeps its window off the screen, also with more variables."""
    gui = pytest.importorskip("pixheet.gui")
    assert check_exe.HIDDEN_ENV == gui.HIDDEN_ENV
    script = "import os; print(os.environ['PIXHEET_HIDDEN_WINDOW'], os.environ['PIXHEET_THEME'])"
    code, output = check_exe.run_exe(
        [sys.executable, "-c", script], timeout=60, env={check_exe.THEME_ENV: "dark"}
    )
    assert code == 0
    assert output.split() == ["1", "dark"]


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
    assert check_exe.result_problems(1, "", 120) == ["The app stopped with exit code 1."]
    assert check_exe.result_problems(None, "  \n", 120) == [
        "The app did not stop in 120 s. The script stopped it."
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
        ("pixheet-gui: drag and drop on\n", "The app did not name its theme."),
        ("", "The app did not name its theme."),
        ("pixheet-gui: theme vista\n", "The app used the theme vista, not sun-valley-dark."),
        (
            "pixheet-gui: theme sun-valley-light\n",
            "The app used the theme sun-valley-light, not sun-valley-dark.",
        ),
    ],
)
def test_theme_problems_refuses_a_fallback_theme(output, problem):
    [found] = check_exe.theme_problems(output, "dark")
    assert found.startswith(problem)
    if output:
        assert found.endswith(f"The output of the app:\n{output.strip()}")
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
    out = capsys.readouterr().out
    assert "sun-valley-dark" in out
    assert out.count("Drag and drop: on.") == 2


def test_check_smoke_fails_without_the_theme(monkeypatch):
    monkeypatch.setattr(
        check_exe, "run_exe", lambda command, timeout, env=None: (0, "pixheet-gui: theme vista\n")
    )
    [problem] = check_exe.check_smoke(Path("Pixheet.exe"), 60)
    assert problem.startswith("The app used the theme vista, not sun-valley-light.")


def test_problem_includes_the_output_of_a_failed_exe():
    code, output = check_exe.run_exe([sys.executable, "-c", FAILED_EXE], timeout=60)
    assert code == 3
    [problem] = check_exe.result_problems(code, output, 60)
    assert problem.startswith("The app stopped with exit code 3. The output of the app:\n")
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


def spec_keywords(name: str) -> list[str]:
    """Return the source text of each keyword argument ``name`` in the calls of the spec."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    return [
        ast.unparse(keyword.value)
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
    windows = set(spec_value("UNWANTED_FILES_WINDOWS"))
    assert windows == {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}
    assert set(spec_value("UNWANTED_FILES_LINUX")) == {"libcrypto.so*", "libssl.so*"}
    assert "tkinterdnd2/tkdnd/win-x64-*/libtkdnd*.dll" in spec_value("REQUIRED_FILES_WINDOWS")
    linux = spec_value("REQUIRED_FILES_LINUX")
    assert "tkinterdnd2/tkdnd/linux-x64-*/lib*tkdnd*.so" in linux
    assert "libx11.so.6" in linux


def test_spec_patterns_of_files_are_in_lower_case():
    """The spec compares the patterns with the paths in lower case."""
    for name in ("SV_TTK_FILES", "REQUIRED_FILES_WINDOWS", "REQUIRED_FILES_LINUX"):
        for pattern in spec_value(name):
            assert pattern == pattern.lower(), pattern


def test_spec_requires_the_x11_dialogs_on_linux():
    """The Linux trim must keep the Tcl dialogs. trim_tcltk drops them on Windows only."""
    files = spec_value("REQUIRED_FILES_LINUX")
    names = ("tkfbox.tcl", "choosedir.tcl", "msgbox.tcl", "iconlist.tcl")
    for name in (f"_tk_data/{name}" for name in names):
        assert name in files
        assert not trim_tcltk.linux_dropped(name)
    assert "tcl9/*/msgcat-*.tm" in files
    assert "pixheet.png" in files


def test_spec_requires_the_sun_valley_theme():
    assert "sv_ttk" in spec_value("REQUIRED_MODULES")
    files = spec_value("SV_TTK_FILES")
    assert "sv_ttk/sv.tcl" in files
    for mode in ("light", "dark"):
        assert f"sv_ttk/theme/{mode}.tcl" in files
        assert f"sv_ttk/theme/spritesheet_{mode}.png" in files


def test_spec_optimizes_the_bytecode_and_strips_only_on_linux():
    assert spec_keywords("optimize") == ["2"]
    assert spec_keywords("upx") == ["False", "False"]  # the EXE options and COLLECT
    assert spec_keywords("strip") == ["not WINDOWS", "not WINDOWS"]
    text = SPEC.read_text(encoding="utf-8")
    assert 'WINDOWS = sys.platform == "win32"\n' in text


def test_spec_uses_the_windows_resources_only_on_windows():
    text = SPEC.read_text(encoding="utf-8")
    assert "\nif WINDOWS:\n    # PyInstaller.utils.win32 needs pefile" in text
    assert "\nfrom PyInstaller.utils.win32" not in text
    assert "if WINDOWS:\n    # An ELF file on Linux has no icon and no version resource.\n" in text
    assert "    common.update(icon=[str(ICON)], version=version)\n" in text


def test_spec_trims_tcl_and_tk_for_each_system():
    text = SPEC.read_text(encoding="utf-8")
    assert "if WINDOWS:\n    a.binaries = trim_binaries(a.binaries, " in text
    assert "else:\n    a.datas, removed, size = trim_datas(a.datas, " in text
    assert text.index("trim_binaries(a.binaries") < text.index("pyz = PYZ(a.pure)")
    assert text.index("trim_datas(a.datas") < text.index("pyz = PYZ(a.pure)")


def test_spec_checks_the_linux_bundle_before_the_trim():
    text = SPEC.read_text(encoding="utf-8")
    check = text.index("if LINUX:\n    floor, floor_file, problems = check_bundle(a.binaries)")
    assert check < text.index("a.binaries = trim_binaries(")
    assert 'raise SystemExit("linux_guards: " + " ".join(problems))' in text


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


def test_linux_build_script_makes_a_reproducible_build():
    """The variables are set only for the PyInstaller command, not for the check."""
    text = (PACKAGING / "build.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    env = text.index('PYTHONHASHSEED=0 SOURCE_DATE_EPOCH="$epoch" \\\n')
    build = text.index("pyinstaller --noconfirm --clean packaging/pixheet.spec")
    assert env < build < text.index("python packaging/check_exe.py")
    assert "export PYTHONHASHSEED" not in text
    assert "export SOURCE_DATE_EPOCH" not in text
    assert 'git -C "$root" log -1 --format=%ct' in text
    assert 'xvfb-run -a "${check[@]}"' in text
    assert 'final="$root/dist/Pixheet-linux-$(uname -m)"' in text
    assert 'sha256sum --binary "$name" > "$name.sha256"' in text


def test_shell_scripts_have_lf_line_ends_and_can_run():
    root = PACKAGING.parent
    assert "*.sh text eol=lf" in (root / ".gitattributes").read_text(encoding="utf-8")
    assert b"\r" not in (PACKAGING / "build.sh").read_bytes()
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("needs a git checkout")
    result = subprocess.run(
        ["git", "ls-files", "--stage", "packaging/build.sh"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("100755 "), result.stdout


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
    """The app does not have the modules in EXCLUDES. Only cli.py and __main__.py can use them."""
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


@pytest.mark.parametrize(
    ("name", "dropped"),
    [
        ("_tcl_data/tzdata/Europe/Paris", True),
        ("_tcl_data/msgs/de.msg", True),
        ("_tcl_data/opt0.4/optparse.tcl", True),
        ("_tcl_data/encoding/cp932.enc", True),
        ("_tcl_data/encoding/utf-8.enc", False),
        ("_tcl_data/encoding/iso8859-15.enc", False),
        ("_tcl_data/init.tcl", False),
        ("_tcl_data/clock.tcl", False),
        ("_tcl_data/safe.tcl", True),
        ("_tcl_data/tclAppInit.c", True),
        ("tcl9/9.0/http-2.10.2.tm", True),
        ("tcl9/9.0/tcltest-2.5.11.tm", True),
        ("tcl9/9.0/platform-1.1.0.tm", True),
        ("tcl9/9.0/platform/shell-1.1.4.tm", True),
        ("tcl9/9.0/msgcat-1.7.1.tm", False),
        ("_tk_data/images/logo.gif", True),
        ("_tk_data/demos/widget", True),
        ("_tk_data/msgs/de.msg", True),
        ("_tk_data/msgs/en.msg", False),
        ("_tk_data/console.tcl", True),
        ("_tk_data/tkfbox.tcl", False),
        ("_tk_data/iconlist.tcl", False),
        ("_tk_data/choosedir.tcl", False),
        ("_tk_data/msgbox.tcl", False),
        ("_tk_data/clrpick.tcl", False),
        ("_tk_data/fontchooser.tcl", False),
        ("_tk_data/xmfbox.tcl", False),
        ("_tk_data/tk.tcl", False),
        ("_tk_data/ttk/clamTheme.tcl", False),
        ("_tk_data/ttk/vistaTheme.tcl", True),
        ("_tk_data/ttk/aquaTheme.tcl", True),
        ("sv_ttk/theme/dark.tcl", False),
        ("pixheet.png", False),
    ],
)
def test_linux_trim_drops_only_unused_files(name, dropped):
    assert trim_tcltk.linux_dropped(name) is dropped


def test_trim_datas_removes_the_files_and_their_index_lines(tmp_path):
    source = tmp_path / "source"
    files = {
        "_tk_data/tk.tcl": "# tk\n",
        "_tk_data/console.tcl": "# console\n",
        "_tk_data/tclIndex": (
            "set auto_index(tk::ConsoleInit) [list source [file join $dir console.tcl]]\n"
            "set auto_index(tk::MessageBox) [list source [file join $dir msgbox.tcl]]\n"
        ),
        "_tcl_data/init.tcl": "# init\n",
        "_tcl_data/tclIndex": "set auto_index(parray) [list source [file join $dir parray.tcl]]\n",
    }
    datas = []
    for dest, text in files.items():
        path = source / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        datas.append((dest, str(path), "DATA"))
    result, removed, size = trim_tcltk.trim_datas(datas, tmp_path / "trim")
    assert (removed, size) == (1, len("# console\n"))
    by_dest = {dest: Path(src) for dest, src, _kind in result}
    assert sorted(by_dest) == sorted(set(files) - {"_tk_data/console.tcl"})
    index = by_dest["_tk_data/tclIndex"]
    assert index == tmp_path / "trim" / "_tk_data" / "tclIndex"
    assert index.read_text(encoding="utf-8") == (
        "set auto_index(tk::MessageBox) [list source [file join $dir msgbox.tcl]]\n"
    )
    # An index without a removed file keeps its source.
    assert by_dest["_tcl_data/tclIndex"] == source / "_tcl_data" / "tclIndex"
    assert (source / "_tk_data" / "console.tcl").is_file()  # the source does not change


# The checks of the Linux bundle

LIBX11_17 = """
/usr/lib/x86_64-linux-gnu/libX11.so.6:     file format elf64-x86-64

DYNAMIC SYMBOL TABLE:
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.2.5 malloc
0000000000000000      DF *UND*  0000000000000000 (GLIBC_2.26) reallocarray
0000000000000000      DF *UND*  0000000000000000  XSetIOErrorExitHandler
00000000000377d0 g    DF .text  0000000000000049  Base        XSetIOErrorExitHandler
0000000000031a20 g    DF .text  00000000000000b0  Base        XOpenDisplay
"""
LIBX11_16 = """
DYNAMIC SYMBOL TABLE:
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.14  memcpy
0000000000031a20 g    DF .text  00000000000000b0  Base        XOpenDisplay
"""
PILLOW = """
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.27  powf
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.3.4 __memcpy_chk
"""


def test_glibc_floor_reads_the_newest_version():
    assert linux_guards.glibc_floor(LIBX11_17) == (2, 26)
    assert linux_guards.glibc_floor(PILLOW) == (2, 27)
    assert linux_guards.glibc_floor("GLIBC_2.9 GLIBC_2.10 GLIBCXX_3.4.30") == (2, 10)
    assert linux_guards.glibc_floor("DYNAMIC SYMBOL TABLE:\n") is None


def test_exports_needs_a_defined_symbol():
    assert linux_guards.exports(LIBX11_17, "XSetIOErrorExitHandler")
    assert not linux_guards.exports(LIBX11_16, "XSetIOErrorExitHandler")
    undefined = "0000000000000000      DF *UND*  0000000000000000  XSetIOErrorExitHandler\n"
    assert not linux_guards.exports(undefined, "XSetIOErrorExitHandler")


def elf_files(folder: Path, texts: dict[str, str]) -> tuple[list, dict[Path, str]]:
    """Make fake ELF files. Return the binaries of PyInstaller and a fake objdump."""
    folder.mkdir(parents=True, exist_ok=True)
    binaries, dumps = [], {}
    for name, text in texts.items():
        path = folder / name.replace("/", "_")
        path.write_bytes(linux_guards.ELF_MAGIC + b"\0" * 12)
        binaries.append((name, str(path), "BINARY"))
        dumps[path] = text
    (folder / "data.txt").write_text("not ELF", encoding="utf-8")
    binaries.append(("data.txt", str(folder / "data.txt"), "DATA"))
    return binaries, dumps


def test_check_bundle_accepts_a_bundle_of_debian_11(tmp_path):
    binaries, dumps = elf_files(tmp_path, {"libX11.so.6": LIBX11_17, "PIL/_imagingmath.so": PILLOW})
    floor, where, problems = linux_guards.check_bundle(binaries, dump=dumps.__getitem__)
    assert (floor, where, problems) == ((2, 27), "PIL/_imagingmath.so", [])
    line = linux_guards.describe(floor, where)
    assert line == (
        "linux_guards: the app needs glibc 2.27 or later (GLIBC_2.27 in PIL/_imagingmath.so)."
    )


def test_check_bundle_refuses_an_old_libx11_and_a_new_glibc(tmp_path):
    newer = "0000000000000000      DF *UND*  0000000000000000  GLIBC_2.34  __libc_start_main"
    binaries, dumps = elf_files(tmp_path, {"libX11.so.6": LIBX11_16, "libgcc_s.so.1": newer})
    floor, where, problems = linux_guards.check_bundle(binaries, dump=dumps.__getitem__)
    assert (floor, where) == ((2, 34), "libgcc_s.so.1")
    assert len(problems) == 2
    assert "older than 1.7" in problems[0]
    assert problems[1].startswith("libgcc_s.so.1 needs GLIBC_2.34. The limit is GLIBC_2.27.")


def test_check_bundle_needs_libx11(tmp_path):
    binaries, dumps = elf_files(tmp_path, {"PIL/_imaging.so": PILLOW})
    _floor, _where, problems = linux_guards.check_bundle(binaries, dump=dumps.__getitem__)
    assert problems == ["The bundle has no libX11.so.6. tkdnd needs it."]


def test_objdump_reports_a_missing_tool_and_a_bad_file(tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError(2, "No such file", command[0])

    with pytest.raises(linux_guards.GuardError, match="Install binutils"):
        linux_guards.objdump(tmp_path / "a.so", run=missing)

    def bad(command, **kwargs):
        assert command == ["objdump", "-T", str(tmp_path / "a.so")]
        return subprocess.CompletedProcess(command, 1, "", "file format not recognized\n")

    with pytest.raises(linux_guards.GuardError, match="file format not recognized"):
        linux_guards.objdump(tmp_path / "a.so", run=bad)


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
        f"The app did not write {paths[0].stem}.xlsx.",
        f"The app wrote a different xl/worksheets/sheet1.xml, xl/styles.xml for {paths[1].name}.",
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
    release = (workflows / "release.yml").read_text(encoding="utf-8")
    assert "packaging/release_version.py check-pr" in ci
    assert "packaging/release_version.py version" in release


WORKFLOWS = PACKAGING.parent / ".github" / "workflows"
RELEASE_FILES = (
    "Pixheet.exe",
    "Pixheet.exe.sha256",
    "Pixheet-linux-x86_64",
    "Pixheet-linux-x86_64.sha256",
)
RELEASE_NOTICES = ("THIRD-PARTY-NOTICES-windows-x64.txt", "THIRD-PARTY-NOTICES-linux-x86_64.txt")


def test_release_waits_for_both_builds_and_publishes_four_files():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "pull_request_target" not in text.replace("Do not use pull_request_target", "")
    assert "\n  build-windows:\n" in text
    assert "\n  build-linux:\n" in text
    assert "    needs: [build-windows, build-linux]\n" in text
    assert "VERSION: ${{ needs.build-windows.outputs.version }}" in text
    assert f"files=({' '.join(RELEASE_FILES)}\n" in text
    assert 'release_plan.py upload "$release" "${files[@]}"' in text
    assert 'gh release upload "$TAG" "${missing[@]}"' in text
    assert 'gh release create "$TAG" "${missing[@]}"' in text
    assert "sha256sum -c Pixheet.exe.sha256 Pixheet-linux-x86_64.sha256" in text
    for name in ("Pixheet-windows-x64", "Pixheet-linux-x86_64"):
        assert f"          name: {name}\n" in text  # upload and download


def test_linux_build_runs_in_debian_11():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    job = text[text.index("\n  build-linux:\n") : text.index("\n  release:\n")]
    assert "    runs-on: ubuntu-latest\n" in job
    # The image is pinned to a digest, the same as each action is pinned to a commit.
    assert re.search(r"\n    container: debian:11@sha256:[0-9a-f]{64}\n", job)
    assert "archive.debian.org/debian bullseye main" in job
    assert "binutils" in job
    assert "xvfb" in job
    assert "sudo apt" not in job  # the container runs as root
    assert "run: xvfb-run -a uv run --locked --extra gui --group build pytest" in job
    assert "run: bash packaging/build.sh" in job
    assert "dist/Pixheet-linux-x86_64.sha256" in job
    assert "contents: read" in job
    assert "contents: write" not in job


def test_ci_tests_on_windows_and_linux():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "os: [windows-latest, ubuntu-latest]" in text
    assert "runs-on: ${{ matrix.os }}" in text
    assert "fail-fast: false" in text
    assert "run=(xvfb-run -a)" in text
    assert text.count("flags: ${{ runner.os }}") == 2


def test_all_jobs_use_only_the_python_of_uv():
    """Without only-managed, uv takes the Python of the Windows runner image for the exe."""
    with (PACKAGING.parent / "pyproject.toml").open("rb") as f:
        assert tomllib.load(f)["tool"]["uv"]["python-preference"] == "only-managed"
    for name in ("ci.yml", "release.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "UV_PYTHON_PREFERENCE" not in text, name  # it would replace the setting
        assert "actions/setup-python" not in text, name
    # The release log shows which Python went into the app.
    assert "pixheet.spec: Python {sys.version.split()[0]} from {sys.base_prefix}" in (
        SPEC.read_text(encoding="utf-8")
    )


def test_ci_uses_one_exact_uv_and_users_can_use_a_newer_uv():
    """An upper bound in required-version stops the README commands with each new uv."""
    with (PACKAGING.parent / "pyproject.toml").open("rb") as f:
        required = tomllib.load(f)["tool"]["uv"]["required-version"]
    match = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", required)
    assert match, required
    minimum = tuple(map(int, match.groups()))
    step = re.compile(
        r"uses: astral-sh/setup-uv@\S+ # \S+\n        with:\n(?:          \S.*\n)*?"
        r'          version: "(\d+\.\d+\.\d+)"\n'
    )
    versions = []
    for name in ("ci.yml", "release.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        found = step.findall(text)
        assert len(found) == text.count("uses: astral-sh/setup-uv@"), name
        versions += found
    assert len(versions) == 3
    assert len(set(versions)) == 1, versions
    assert tuple(map(int, versions[0].split("."))) >= minimum


@pytest.mark.parametrize("name", ["release_version", "release_plan", "release_notes"])
def test_release_scripts_run_on_python_3_12(name):
    """ci.yml and the release job run the scripts with the Python 3.12 of the Ubuntu runner."""
    source = (PACKAGING / f"{name}.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 12))


SHA = "a" * 40


@pytest.mark.parametrize(
    ("tag_commit", "action"),
    [(None, "create"), ("", "create"), (SHA, "publish"), ("b" * 40, "skip")],
)
def test_tag_action(tag_commit, action):
    assert release_plan.tag_action(tag_commit, SHA) == action


def test_upload_plan_creates_a_missing_release_with_all_files():
    assert release_plan.upload_plan(False, [], RELEASE_FILES) == ("create", list(RELEASE_FILES))


def test_upload_plan_adds_only_the_missing_pairs():
    # An earlier run uploaded the Windows pair, then stopped.
    existing = ["Pixheet.exe", "Pixheet.exe.sha256", "notes.txt"]
    assert release_plan.upload_plan(True, existing, RELEASE_FILES) == (
        "upload",
        ["Pixheet-linux-x86_64", "Pixheet-linux-x86_64.sha256"],
    )


def test_upload_plan_uploads_all_files_to_an_empty_release():
    assert release_plan.upload_plan(True, [], RELEASE_FILES) == ("upload", list(RELEASE_FILES))


def test_upload_plan_does_nothing_for_a_complete_release():
    existing = [*reversed(RELEASE_FILES), "notes.txt"]
    assert release_plan.upload_plan(True, existing, RELEASE_FILES) == ("done", [])


@pytest.mark.parametrize("present", ["Pixheet.exe", "Pixheet-linux-x86_64.sha256"])
def test_upload_plan_stops_for_half_a_pair(present):
    """A new build can differ, so a new checksum file must not go next to an old file."""
    with pytest.raises(release_plan.PlanError, match=f"The release has {present}, but not"):
        release_plan.upload_plan(True, [present], RELEASE_FILES)


def test_release_plan_tag_command(capsys):
    assert release_plan.main(["tag", SHA, ""]) == 0
    assert release_plan.main(["tag", SHA]) == 0
    assert release_plan.main(["tag", SHA, SHA]) == 0
    assert release_plan.main(["tag", SHA, "b" * 40]) == 0
    assert capsys.readouterr().out.split() == ["create", "create", "publish", "skip"]


@pytest.mark.parametrize(
    ("release", "assets", "code", "out"),
    [
        ("false", "", 0, ["create", *RELEASE_FILES]),
        ("true", "Pixheet.exe\nPixheet.exe.sha256\n", 0, ["upload", *RELEASE_FILES[2:]]),
        ("true", "\n".join(RELEASE_FILES) + "\n", 0, ["done"]),
        ("true", "Pixheet.exe\n", 1, ["::error::The release has Pixheet.exe, but not"]),
    ],
)
def test_release_plan_upload_command(monkeypatch, capsys, release, assets, code, out):
    monkeypatch.setattr(sys, "stdin", io.StringIO(assets))
    assert release_plan.main(["upload", release, *RELEASE_FILES]) == code
    lines = capsys.readouterr().out.splitlines()
    if code:
        assert lines[0].startswith(out[0])
    else:
        assert lines == out


def test_release_job_uses_the_release_plan():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    job = text[text.index("\n  release:\n") :]
    assert 'python3 packaging/release_plan.py tag "$SHA" "$commit"' in job
    assert "persist-credentials: false" in job
    # The decision comes before the downloads, so that "skip" downloads nothing.
    assert job.index("release_plan.py tag") < job.index("Download the Windows app")


def test_release_runs_never_cancel_a_push():
    """A cancelled release job can leave a tag without a release."""
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    top = text[: text.index("\njobs:\n")]
    assert "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n" in top
    job = text[text.index("\n  release:\n") :]
    assert "      group: ${{ github.workflow }}-release\n" in job
    assert "      cancel-in-progress: false\n" in job


CHANGELOG = """\
# Changelog

Text before the first version.

## [0.2.0] - 2026-11-01

### Added

- Item two.

## 0.1.0

The first release.

### Fixed

- Item one.
## 0.0.9
"""


def test_changelog_section_gives_the_text_of_one_version():
    assert release_notes.changelog_section(CHANGELOG, "0.2.0") == "### Added\n\n- Item two.\n"
    assert release_notes.changelog_section(CHANGELOG, "0.1.0") == (
        "The first release.\n\n### Fixed\n\n- Item one.\n"
    )


def test_changelog_section_reads_a_file_without_a_final_line_end():
    text = "# Changelog\n\n## 1.0.0\n\n- The last line.  "
    assert release_notes.changelog_section(text, "1.0.0") == "- The last line.\n"


@pytest.mark.parametrize(
    ("version", "problem"),
    [
        ("0.3.0", "has no section '## 0.3.0'"),
        ("0.1", "has no section '## 0.1'"),  # 0.1 is not 0.1.0
        ("0.0.9", "is empty"),
        ("", "is not correct"),
    ],
)
def test_changelog_section_refuses_a_missing_or_empty_section(version, problem):
    with pytest.raises(release_notes.NotesError, match=re.escape(problem)):
        release_notes.changelog_section(CHANGELOG, version)


def test_release_notes_add_the_downloads_of_the_version():
    notes = release_notes.release_notes(CHANGELOG, "0.1.0")
    assert notes.startswith("The first release.\n")
    assert "\n\n## Downloads\n" in notes
    assert "https://github.com/adv27/pixheet/blob/v0.1.0/README.md" in notes
    for name in ("Pixheet.exe", "Pixheet-linux-x86_64.sha256", "chmod +x", "Run anyway"):
        assert name in notes


def test_unwrap_joins_the_lines_of_each_paragraph_and_item():
    text = (
        "### Added\n\nA long\nparagraph.\n\n- An item\n  with two lines.\n"
        "- Next item:\n  - A sub-item\n    goes on.\n1. A step\n   goes on.\n\n"
        "```console\n$ one\n$ two\n```\nText after.\n"
    )
    assert release_notes.unwrap(text) == (
        "### Added\n\nA long paragraph.\n\n- An item with two lines.\n"
        "- Next item:\n  - A sub-item goes on.\n1. A step goes on.\n\n"
        "```console\n$ one\n$ two\n```\nText after.\n"
    )


def test_release_notes_command(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert release_notes.main(["0.2.0", str(changelog)]) == 0
    assert capsys.readouterr().out.startswith("### Added\n\n- Item two.\n\n## Downloads\n")
    assert release_notes.main(["0.3.0", str(changelog)]) == 1
    assert capsys.readouterr().out.startswith("::error::CHANGELOG.md has no section")


def test_changelog_has_the_version_of_pyproject():
    """Without the section, the release job stops after the merge."""
    root = PACKAGING.parent
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    section = release_notes.changelog_section(
        text, release_version.read_version(root / "pyproject.toml")
    )
    assert "### Added" in section or "### Fixed" in section or "### Changed" in section


def test_workflows_use_the_release_notes():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("\n  version:\n") :]
    assert 'python3 packaging/release_notes.py "$version" > /dev/null' in job
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    job = text[text.index("\n  release:\n") :]
    notes = 'python3 packaging/release_notes.py "$VERSION" > "$RUNNER_TEMP/notes.md"'
    assert notes in job
    # A missing section stops the job before it makes the tag.
    assert job.index(notes) < job.index("release_plan.py tag")
    assert '--notes-file "$RUNNER_TEMP/notes.md" --generate-notes' in job


third_party_notices = load("third_party_notices")

# The bundle of the Windows build at a0b4519 (build/pixheet/PKG-00.toc), before the trim.
WINDOWS_BUNDLE = [
    *(
        (name, f"C:/python/{name}", "BINARY")
        for name in (
            "python315.dll",
            "VCRUNTIME140.dll",
            "VCRUNTIME140_1.dll",
            "libffi-8.dll",
            "tcl90.dll",
            "tcl9tk90.dll",
            "libtommath.dll",
            "zlib1.dll",
            "tkinterdnd2/tkdnd/win-x64-tcl9/libtkdnd2.10.3.dll",
        )
    ),
    *(
        (name, f"C:/python/DLLs/{name}", "EXTENSION")
        for name in (
            "select.pyd",
            "unicodedata.pyd",
            "_ctypes.pyd",
            "_socket.pyd",
            "_wmi.pyd",
            "_tkinter.pyd",
            "_queue.pyd",
            "PIL/_webp.cp315-win_amd64.pyd",
            "PIL/_avif.cp315-win_amd64.pyd",
            "PIL/_imagingcms.cp315-win_amd64.pyd",
            "PIL/_imaging.cp315-win_amd64.pyd",
            "PIL/_imagingmath.cp315-win_amd64.pyd",
        )
    ),
    ("pixheet.ico", "packaging/pixheet.ico", "DATA"),
    ("sv_ttk/sv.tcl", "site-packages/sv_ttk/sv.tcl", "DATA"),
    ("tkinterdnd2/tkdnd/win-x64-tcl9/tkdnd.tcl", "site-packages/tkdnd.tcl", "DATA"),
    ("base_library.zip", "build/base_library.zip", "DATA"),
]
WINDOWS_BUILTINS = ("_abc", "_sha2", "binascii", "nt", "zlib")

# The system libraries of the Linux build of the release run 39914134950.
LINUX_SYSTEM_LIBRARIES = {
    "libX11.so.6": "libx11-6",
    "libXau.so.6": "libxau6",
    "libXcursor.so.1": "libxcursor1",
    "libXdmcp.so.6": "libxdmcp6",
    "libXfixes.so.3": "libxfixes3",
    "libXrender.so.1": "libxrender1",
    "libbsd.so.0": "libbsd0",
    "libgcc_s.so.1": "libgcc-s1",
    "libmd.so.0": "libmd0",
    "libstdc++.so.6": "libstdc++6",
    "libz.so.1": "zlib1g",
}
# The other files of that build: all binaries of a kind, and a part of the data.
LINUX_BUNDLE = [
    *((name, f"/usr/lib/x86_64-linux-gnu/{name}", "BINARY") for name in LINUX_SYSTEM_LIBRARIES),
    *(
        (name, f"/opt/python/lib/{name}", "BINARY")
        for name in ("libpython3.15.so.1.0", "libtcl9.0.so", "libtcl9tk9.0.so")
    ),
    *(
        (f"pillow.libs/{name}", f"/venv/pillow.libs/{name}", "BINARY")
        for name in ("libjpeg-6dc8d115.so.62.4.0", "libxcb-9065bb12.so.1.1.0")
    ),
    # PyInstaller adds a link at the top for each library in pillow.libs.
    ("libxcb-9065bb12.so.1.1.0", "pillow.libs/libxcb-9065bb12.so.1.1.0", "SYMLINK"),
    ("PIL/_imaging.cpython-315-x86_64-linux-gnu.so", "/venv/PIL/_imaging.so", "EXTENSION"),
    (
        "python3.15/lib-dynload/_tkinter.cpython-315-x86_64-linux-gnu.so",
        "/opt/python/lib/python3.15/lib-dynload/_tkinter.so",
        "EXTENSION",
    ),
    (
        "tkinterdnd2/tkdnd/linux-x64-tcl9/libtcl9tkdnd2.10.3.so",
        "/venv/tkinterdnd2/tkdnd/linux-x64-tcl9/libtcl9tkdnd2.10.3.so",
        "BINARY",
    ),
    ("_tcl_data/init.tcl", "/opt/python/lib/tcl9.0/init.tcl", "DATA"),
    ("_tk_data/tkfbox.tcl", "/opt/python/lib/tk9.0/tkfbox.tcl", "DATA"),
    ("tcl9/9.0/msgcat-1.7.1.tm", "/opt/python/lib/tcl9/9.0/msgcat-1.7.1.tm", "DATA"),
    ("pixheet.png", "packaging/pixheet.png", "DATA"),
    ("sv_ttk/sv.tcl", "/venv/sv_ttk/sv.tcl", "DATA"),
    ("base_library.zip", "build/base_library.zip", "DATA"),
]
LINUX_BUILTINS = (
    "_bz2",
    "_ctypes",
    "_curses",
    "_decimal",
    "_hashlib",
    "_lzma",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "_zstd",
    "pyexpat",
    "readline",
    "zlib",
)


def fake_license(name: str) -> str:
    return f"License of {name}.\n"


def fake_copyright(source: str) -> tuple[str, str]:
    package = LINUX_SYSTEM_LIBRARIES[Path(source).name]
    return package, f"Copyright of {package}.\n"


def collect_notices(entries, system, builtins, package_license=fake_license):
    return third_party_notices.collect(
        entries,
        system=system,
        builtins=builtins,
        root=PACKAGING.parent,
        python_text="License of Python.\n",
        package_license=package_license,
        system_copyright=fake_copyright,
    )


def test_each_file_of_the_windows_bundle_has_an_owner():
    owners = {dest: third_party_notices.owner(dest) for dest, _source, _kind in WINDOWS_BUNDLE}
    assert owners["python315.dll"] == owners["_ctypes.pyd"] == owners["base_library.zip"]
    assert owners["python315.dll"] == "python"
    assert owners["tcl90.dll"] == "tcl"
    assert owners["tcl9tk90.dll"] == "tk"
    assert owners["PIL/_imaging.cp315-win_amd64.pyd"] == "pillow"
    assert owners["tkinterdnd2/tkdnd/win-x64-tcl9/libtkdnd2.10.3.dll"] == "tkdnd"
    assert owners["VCRUNTIME140_1.dll"] == "msvc"
    assert None not in owners.values()


def test_windows_notices_have_each_part_of_the_bundle():
    notices = collect_notices(WINDOWS_BUNDLE, "Windows", WINDOWS_BUILTINS)
    titles = [n.title for n in notices]
    assert titles == [
        "Pixheet",
        "Python",
        "PyInstaller",
        "Pillow",
        "sv-ttk",
        "tkinterdnd2",
        "tkdnd",
        "Microsoft Visual C++ runtime",
        "Tcl",
        "Tk",
        "libffi",
        "libtommath",
        "zlib",
        "zlib-ng",
    ]
    by_title = {n.title: n for n in notices}
    assert by_title["Pixheet"].text == (PACKAGING.parent / "LICENSE").read_text(encoding="utf-8")
    assert by_title["Pillow"].text == "License of pillow.\n"
    assert by_title["libffi"].text == third_party_notices.fixed_text("libffi")
    assert "Files: libffi-8.dll." in by_title["libffi"].about
    assert "Python modules: _ctypes." in by_title["libffi"].about
    assert "Python modules: _tkinter, binascii, zlib." in by_title["zlib"].about
    # The OpenSSL DLLs are not in the Windows bundle, so their license is not either.
    assert "OpenSSL 3" not in titles


def test_linux_notices_have_the_static_libraries_and_the_debian_packages():
    notices = collect_notices(LINUX_BUNDLE, "Linux", LINUX_BUILTINS)
    by_title = {n.title: n for n in notices}
    # libpython3.15.so includes these libraries for its built-in modules.
    for title in ("OpenSSL 3", "SQLite", "Expat", "libedit", "ncurses", "mpdecimal", "bzip2"):
        assert title in by_title, title
    assert "Python modules: _hashlib, _ssl." in by_title["OpenSSL 3"].about
    # libtcl9tk9.0.so includes libX11, libXau and libxcb.
    for title in ("libX11", "libXau", "libxcb"):
        assert "Python modules: _tkinter." in by_title[title].about
    assert "Microsoft Visual C++ runtime" not in by_title
    for name, package in LINUX_SYSTEM_LIBRARIES.items():
        notice = by_title[f"Debian package {package}"]
        assert notice.text == f"Copyright of {package}.\n"
        assert name in notice.about
    assert (
        by_title["Tcl"].about
        == "The Linux app includes Tcl. Files: libtcl9.0.so. Python modules: _tkinter."
    )


@pytest.mark.parametrize(
    ("system", "bundle"), [("Windows", WINDOWS_BUNDLE), ("Linux", LINUX_BUNDLE)]
)
def test_notices_stop_for_a_file_without_an_owner(system, bundle):
    entries = [*bundle, ("libssl-3-x64.dll", "C:/python/DLLs/libssl-3-x64.dll", "BINARY")]
    with pytest.raises(third_party_notices.NoticeError, match=r"\['libssl-3-x64\.dll'\]"):
        collect_notices(entries, system, WINDOWS_BUILTINS)


def test_a_pattern_without_a_folder_agrees_only_with_a_file_at_the_top():
    assert third_party_notices.owner("_ctypes.pyd") == "python"
    assert third_party_notices.owner("other/_speedups.pyd") is None
    assert third_party_notices.owner("PIL/_imaging.pyd") == "pillow"
    entries = [*LINUX_BUNDLE, ("other/libfoo.so.1", "/venv/other/libfoo.so.1", "BINARY")]
    with pytest.raises(third_party_notices.NoticeError, match="other/libfoo"):
        collect_notices(entries, "Linux", LINUX_BUILTINS)


def test_notices_stop_for_a_link_to_a_file_that_is_not_in_the_bundle():
    entries = [*LINUX_BUNDLE, ("libfoo.so.1", "pillow.libs/libfoo-1234.so.1", "SYMLINK")]
    with pytest.raises(third_party_notices.NoticeError, match="libfoo-1234"):
        collect_notices(entries, "Linux", LINUX_BUILTINS)


def test_windows_notices_stop_for_a_library_without_an_owner():
    """Only Linux takes the license of a system library from its Debian package."""
    entries = [*WINDOWS_BUNDLE, ("libfoo.so.1", "C:/foo/libfoo.so.1", "BINARY")]
    with pytest.raises(third_party_notices.NoticeError, match=r"libfoo\.so\.1"):
        collect_notices(entries, "Windows", WINDOWS_BUILTINS)


def test_notices_stop_for_an_empty_text():
    def license_text(name):
        return "" if name == "pillow" else "Text.\n"

    with pytest.raises(third_party_notices.NoticeError, match="license text of Pillow is empty"):
        collect_notices(WINDOWS_BUNDLE, "Windows", WINDOWS_BUILTINS, license_text)


def test_render_numbers_the_parts_and_gives_each_text():
    notices = collect_notices(WINDOWS_BUNDLE, "Windows", WINDOWS_BUILTINS)
    text = third_party_notices.render(notices, "Windows")
    assert text.startswith("Third-party notices of Pixheet for Windows\n\nPixheet.exe includes")
    assert "\n  1. Pixheet\n  2. Python\n" in text
    assert "\n14. zlib-ng\n" in text
    assert "\r" not in text
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    for notice in notices:
        assert notice.text.strip() in text
    # The same inputs give the same text, so two builds give the same app.
    again = collect_notices(list(reversed(WINDOWS_BUNDLE)), "Windows", WINDOWS_BUILTINS)
    assert third_party_notices.render(again, "Windows") == text


def test_write_notices_writes_utf8_with_lf(tmp_path, monkeypatch):
    collect = third_party_notices.collect
    monkeypatch.setattr(
        third_party_notices,
        "collect",
        lambda *args, **kwargs: collect(*args, package_license=fake_license, **kwargs),
    )
    (tmp_path / "LICENSE.txt").write_bytes(b"Python\r\nlicense\r\n")
    path = tmp_path / "out" / "THIRD-PARTY-NOTICES.txt"
    third_party_notices.write_notices(
        path,
        WINDOWS_BUNDLE,
        system="Windows",
        builtins=WINDOWS_BUILTINS,
        root=PACKAGING.parent,
        base_prefix=tmp_path,
        stdlib=tmp_path / "Lib",
    )
    data = path.read_bytes()
    assert b"\r" not in data
    assert b"\nPython\nlicense\n" in data
    data.decode("utf-8")


def test_python_license_is_in_the_folder_of_python_or_of_the_standard_library(tmp_path):
    stdlib = tmp_path / "lib" / "python3.15"
    stdlib.mkdir(parents=True)
    with pytest.raises(third_party_notices.NoticeError, match=r"has no LICENSE\.txt"):
        third_party_notices.python_license(tmp_path, stdlib)
    (stdlib / "LICENSE.txt").write_text("Linux layout\n", encoding="utf-8")
    assert third_party_notices.python_license(tmp_path, stdlib) == "Linux layout\n"
    (tmp_path / "LICENSE.txt").write_text("Windows layout\n", encoding="utf-8")
    assert third_party_notices.python_license(tmp_path, stdlib) == "Windows layout\n"


def test_python_of_the_build_has_a_license():
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    text = third_party_notices.python_license(Path(sys.base_prefix), stdlib)
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in text


@pytest.mark.parametrize("name", ["pillow", "sv-ttk", "tkinterdnd2"])
def test_installed_distributions_have_a_license(name):
    text = third_party_notices.distribution_license(name)
    assert "permission" in text.lower()


def test_pillow_license_has_the_libraries_of_the_wheel():
    """The Windows wheel and the Linux wheel use different headings for the libraries."""
    text = third_party_notices.distribution_license("pillow").lower()
    for library in ("jpeg", "tiff", "webp", "lcms", "openjp", "avif"):
        assert library in text, library


def test_pyinstaller_license_has_the_bootloader_exception():
    pytest.importorskip("PyInstaller", reason="PyInstaller is only in the build group")
    text = third_party_notices.distribution_license("pyinstaller")
    assert "Bootloader Exception" in text


def test_distribution_license_stops_for_a_missing_distribution():
    with pytest.raises(third_party_notices.NoticeError, match="is not installed"):
        third_party_notices.distribution_license("no-such-distribution-of-pixheet")


def test_each_fixed_text_has_a_file_and_each_file_is_used():
    names = {path.stem for path in third_party_notices.NOTICES.glob("*")}
    assert names == set(third_party_notices.FIXED)
    tables = (third_party_notices.MODULE_LIBRARIES, third_party_notices.LINUX_MODULE_LIBRARIES)
    used = {library for table in tables for value in table.values() for library in value}
    assert used <= set(third_party_notices.FIXED)
    for name in names:
        data = (third_party_notices.NOTICES / f"{name}.txt").read_bytes()
        assert data.strip(), name
        assert b"\r" not in data, name
        data.decode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="the Tcl and Tk DLLs are on Windows")
@pytest.mark.parametrize(
    ("dll", "member", "name"),
    [
        ("tcl90.dll", "tcl_library/license.terms", "tcl"),
        ("tcl9tk90.dll", "tk_library/license.terms", "tk"),
    ],
)
def test_fixed_tcl_and_tk_texts_are_the_texts_in_the_dlls(dll, member, name):
    with zipfile.ZipFile(Path(sys.base_prefix) / "DLLs" / dll) as archive:
        text = archive.read(member).decode("utf-8").replace("\r\n", "\n")
    assert text == third_party_notices.fixed_text(name)


def fake_dpkg(owned: str):
    """Return a subprocess.run for dpkg-query that knows only the path ``owned``."""

    def run(command, **_kwargs):
        if command[-1] == owned:
            return subprocess.CompletedProcess(command, 0, f"libx11-6:amd64: {owned}\n", "")
        return subprocess.CompletedProcess(command, 1, "", "no path found")

    return run


def test_debian_copyright_tries_the_path_without_usr(tmp_path, monkeypatch):
    calls = []
    run = fake_dpkg("/lib/x86_64-linux-gnu/libX11.so.6")

    def record(command, **kwargs):
        calls.append(command[-1])
        return run(command, **kwargs)

    monkeypatch.setattr(third_party_notices.subprocess, "run", record)
    monkeypatch.setattr(third_party_notices, "DEBIAN_DOCS", tmp_path)
    (tmp_path / "libx11-6").mkdir()
    (tmp_path / "libx11-6" / "copyright").write_text("X11 copyright\n", encoding="utf-8")
    source = "/usr/lib/x86_64-linux-gnu/libX11.so.6"
    assert third_party_notices.debian_copyright(source) == ("libx11-6", "X11 copyright\n")
    assert calls[0] == source
    assert "/lib/x86_64-linux-gnu/libX11.so.6" in calls


def test_debian_copyright_stops_without_a_copyright_file(tmp_path, monkeypatch):
    monkeypatch.setattr(third_party_notices.subprocess, "run", fake_dpkg("/usr/lib/libX11.so.6"))
    monkeypatch.setattr(third_party_notices, "DEBIAN_DOCS", tmp_path)
    with pytest.raises(third_party_notices.NoticeError, match="libx11-6 has no"):
        third_party_notices.debian_copyright("/usr/lib/libX11.so.6")


def test_debian_copyright_stops_without_a_package(monkeypatch):
    monkeypatch.setattr(third_party_notices.subprocess, "run", fake_dpkg("/nothing"))
    with pytest.raises(third_party_notices.NoticeError, match="No Debian package"):
        third_party_notices.debian_copyright("/opt/libfoo.so.1")


def test_debian_copyright_stops_without_dpkg(monkeypatch):
    def run(command, **_kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(third_party_notices.subprocess, "run", run)
    with pytest.raises(third_party_notices.NoticeError, match="Build on Debian 11"):
        third_party_notices.debian_copyright("/usr/lib/libfoo.so.1")


def test_spec_writes_the_notices_into_the_app_before_the_guards_and_the_trim():
    text = SPEC.read_text(encoding="utf-8")
    for name in ("REQUIRED_FILES_WINDOWS", "REQUIRED_FILES_LINUX"):
        assert "third-party-notices.txt" in spec_value(name)
    write = text.index("    write_notices(\n")
    assert text.index("\na = Analysis(") < write
    assert write < text.index("files = [dest.replace")  # the guard of REQUIRED_FILES
    assert write < text.index("trim_binaries(a.binaries")
    assert 'a.datas += [(FILE_NAME, str(notices), "DATA")]' in text
    assert 'NOTICES_ASSET = "THIRD-PARTY-NOTICES-windows-x64.txt"' in text
    assert 'NOTICES_ASSET = f"THIRD-PARTY-NOTICES-linux-{os.uname().machine}.txt"' in text
    assert 'raise SystemExit(f"third_party_notices: {e}") from e' in text


def test_builds_and_release_publish_the_notices():
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    for name in RELEASE_NOTICES:
        assert f"            dist/{name}\n" in release  # upload-artifact
    files = re.search(r"files=\(([^)]*)\)", release).group(1).split()
    assert files == [*RELEASE_FILES, *RELEASE_NOTICES]
    # A notices file has no .sha256 file. release_plan.py uploads it alone.
    assert release_plan.upload_plan(True, RELEASE_FILES, files) == ("upload", [*RELEASE_NOTICES])
    build_ps1 = (PACKAGING / "build.ps1").read_text(encoding="utf-8")
    assert "dist\\THIRD-PARTY-NOTICES-windows-x64.txt" in build_ps1
    build_sh = (PACKAGING / "build.sh").read_text(encoding="utf-8")
    assert 'notices="$root/dist/THIRD-PARTY-NOTICES-linux-$(uname -m).txt"' in build_sh
