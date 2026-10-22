"""Tests of the scripts in packaging/: the icon, the version resource, and the runner hook."""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from PIL import Image

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def load(name: str):
    """Import a module from packaging/, which is not a package."""
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icon = load("make_icon")
version_info = load("version_info")
check_exe = load("check_exe")


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
