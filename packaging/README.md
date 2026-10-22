# Packaging and CI

This folder holds the build of `Pixheet.exe`, the desktop app for Windows. This page is
for maintainers.

## Files

| File | Content |
|---|---|
| `pixheet_gui.py` | The entry script that PyInstaller freezes. |
| `pixheet.spec` | The PyInstaller spec. It makes one file by default. It leaves unused modules out of the exe. |
| `trim_tcltk.py` | Removes unused files from the Tcl and Tk library in `tcl90.dll` and `tcl9tk90.dll`. The spec runs it. |
| `version_info.py` | Reads the version resource of the exe from `pyproject.toml` and `LICENSE`. |
| `pixheet.ico` | The icon of the exe and of the window, in 7 sizes from 16 to 256 pixels. |
| `make_icon.py` | Draws `pixheet.ico` with Pillow. |
| `build.ps1` | Builds the exe and tests it. |
| `check_exe.py` | Makes sure that a built exe starts, and that it converts test images of 11 formats and modes correctly. |
| `runner_job_started.ps1` | The job-started hook of the self-hosted runner. It refuses each job that is not a trusted build. |

## Build on your computer

Run this command from the repository root:

```console
> powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script does these steps:

1. It installs the locked dependencies with the `gui` extra and the `build` group.
2. It builds `dist\Pixheet.exe` from `pixheet.spec`.
3. It runs `check_exe.py`. The exe must pass the smoke test, and it must convert a test
   image correctly. Then the exe converts images of 11 formats and modes, for example
   AVIF, WebP, a CMYK JPEG with an ICC profile, and a LAB TIFF. Each sheet must be the
   same, byte for byte, as the sheet of `pixheet.convert` in Python. If the exe does not
   stop in 120 seconds, `check_exe.py` stops it and all processes that it started.
4. It writes `dist\Pixheet.exe.sha256`.

The script sets `PYINSTALLER_CONFIG_DIR` to `build\pyinstaller-config`. With `--clean`,
PyInstaller deletes its cache folder, so the build does not touch the cache folder that
other PyInstaller builds share.

The build is reproducible. Two builds of one commit with the same versions of Python, uv
and the locked packages give the same exe, byte for byte. Thus they also give the same
SHA256. For the PyInstaller step only, the script sets these two variables:

- `PYTHONHASHSEED=0`. Without it, the order of the files in `base_library.zip` changes
  from build to build. Then the size of the exe changes by about 1 KB.
- `SOURCE_DATE_EPOCH`. PyInstaller writes it as the time stamp in the PE header of the
  exe. The script uses the time of the last commit. When you set `SOURCE_DATE_EPOCH`
  before you start the script, the script uses your value.

Add `-OneDir` to build the folder `dist\Pixheet\` in place of one file. To run
PyInstaller without the script, use these commands:

```console
> uv run --extra gui --group build pyinstaller --noconfirm --clean packaging\pixheet.spec
> uv run --extra gui --group build pyinstaller --noconfirm --clean packaging\pixheet.spec -- --onedir
```

With `--smoke`, the app opens the window and closes it when it has nothing to do. When
you give image paths, the app converts them first. The exit code is 1 when the app
finds a problem. A built app must also have the window icon and drag and drop.

The exe shows the version of `pyproject.toml` in its properties. To change the icon,
edit `make_icon.py`, then run `uv run python packaging/make_icon.py`. Commit the script
and the new `pixheet.ico`. A test makes sure that the two agree.

## The size of the exe

The one-file exe is about 13.7 MB. Without the changes below, it is about
19.6 MB. `build.ps1` shows 13.1 MB, because it counts 1 MB as 1,048,576 bytes. The spec
keeps all the features. It makes these changes. Each number is the saving of one change
alone:

| Change | Saves | Why it is safe |
|---|---|---|
| `EXCLUDES`: `urllib`, `http`, `email`, `ssl`, `hashlib` and more | about 2.5 MB, with OpenSSL | The app never opens a URL. `pathlib` imports `urllib` only in `Path.as_uri()` and `Path.from_uri()`. |
| `EXCLUDES`: `multiprocessing`, `decimal`, `xml`, `lzma`, `bz2`, `compression.zstd`, `argparse` and more | about 1.8 MB | pixheet uses threads, not processes. The xlsx file uses only deflate. Pillow needs `xml` only in `Image.getxmp()`. Only the CLI uses `argparse`, and the exe has no CLI. |
| `EXCLUDES`: `PIL.ImageTk`, `PIL.ImageQt`, `PIL.ImageShow`, `PIL.ImageWin`, `PIL.ImageFilter`, and the save-only PDF and Palm plugins | about 0.2 MB | pixheet never calls these modules. `PIL.ImageCms` stays, because LAB images need it. |
| `optimize=2` | about 0.5 MB | It removes the docstrings and the `assert` statements from the bytecode. No code of the app reads `__doc__`. The one `assert`, in `_xlsx.py`, is always true on Windows. |
| `trim_tcltk.py` | about 1.1 MB | It removes the time zone data, most encodings, and the Tk demo images. It also removes the messages for other languages, and the Tk scripts for X11 and macOS. |

These changes are not in the spec:

- UPX makes the exe about 4.5 MB smaller. But each start is about 0.4 s slower, and
  antivirus programs flag UPX-packed exes.
- AVIF support stays. `PIL._avif` is 4.3 MB, but AVIF is a real image format.

The spec also stops the build in these conditions:

- A module in `UNWANTED_MODULES` or a file in `UNWANTED_FILES` is in the bundle. A new
  hook or a new import can bring back an excluded module.
- A module in `REQUIRED_MODULES` or a file in `REQUIRED_FILES` is not in the bundle,
  for example `PIL._avif` or the tkdnd DLL.
- `trim_tcltk.py` cannot read a Tcl or Tk DLL, or the DLL is not a PE file. A code
  signature also stops the build.
- `trim_tcltk.py` does not find a correct zip archive after the PE image of a DLL.
- `trim_tcltk.py` does not find each of the two DLLs one time.

### Maintenance rule

The tests run pixheet in Python, which has all the modules. Thus, if new code in
pixheet or in the GUI imports a module in `EXCLUDES`, the tests pass, but the exe stops
with `ModuleNotFoundError`. A test finds a direct import of an excluded module in
`src/pixheet`. It does not find an import in a library that pixheet uses. Thus do
these steps after a change to the imports, or after an update of Pillow, Python, or
PyInstaller:

1. Run `packaging\build.ps1`. Its `check_exe.py` step converts images of many formats
   with the exe.
2. If the exe needs an excluded module, remove the module from `EXCLUDES` and from
   `UNWANTED_MODULES`.

When the exe cannot import a module, it does not stop. The windowed exe shows the
traceback in a dialog, and the dialog stays open. Thus `check_exe.py` reports that the
exe did not stop in 120 s. The output of the exe then shows the `ModuleNotFoundError`.

## The workflows

| Workflow | Runner | Triggers | Steps |
|---|---|---|---|
| `.github/workflows/ci.yml` | GitHub-hosted `windows-latest` | pull request, push to `master`, manual | tests, `ruff check`, `ruff format --check` |
| `.github/workflows/windows-exe.yml` | self-hosted, labels `self-hosted`, `Windows`, `X64` | push to `master`, push of a `v*` tag, manual | tests, `build.ps1`, upload of the artifact `Pixheet-windows-x64` |

For a `v*` tag, a second job publishes `Pixheet.exe` and `Pixheet.exe.sha256` in a
GitHub release. That job runs on a GitHub-hosted runner. The tag must agree with the
version in `pyproject.toml`, for example `v0.1.0` for version `0.1.0`.

To make a release, do these steps:

1. Change `version` in `pyproject.toml`, and run `uv lock`.
2. Commit the change, and push it to `master`.
3. Push a tag: `git tag v0.2.0`, then `git push origin v0.2.0`.

The release job never replaces a file of a published release, because users can have
downloaded the file already. A new build of the same commit can also give a different
exe. For example, each job installs the newest patch release of Python 3.15. When the
job runs again for a tag, for example after a re-run, it fails if the
release already has `Pixheet.exe` or `Pixheet.exe.sha256`. To replace a file, delete it
in the release on GitHub, and then run the job again.

You can also protect the releases in the settings of the repository:

- Turn on immutable releases.
- Add a tag ruleset for `v*` that lets only you make and delete these tags.

The workflows pin each action to a full commit SHA. The comment after the SHA gives the
version. When you update an action, update the SHA and the comment together. Then run
these linters:

```console
> uvx zizmor .github/workflows
> uvx --from actionlint-py actionlint
```

## Register the self-hosted runner

The runner must be a Windows x64 computer. It needs Git. The workflow installs uv, and
uv installs Python 3.15. Each job downloads uv and Python again, and keeps the uv cache
and Python in the temporary folder of the job. The runner deletes that folder at the end
of the job. Thus no file from an earlier job goes into a release.

1. Make a local Windows account only for the runner. Do not use your own account or an
   administrator account. This page calls it the "runner account".
2. Let PowerShell run local scripts. Open PowerShell as an administrator, and run this
   command:

   ```console
   > Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine
   ```

   Windows 10 and Windows 11 refuse all scripts by default. The runner runs each
   `shell: powershell` step and the job-started hook as a script, so without this
   command each job fails. As an alternative, run the command with `-Scope CurrentUser`
   as the runner account.
3. On GitHub, open **Settings > Actions > Runners**, and click **New self-hosted runner**.
4. Select **Windows** and **x64**.
5. Run the download and configure commands that GitHub shows, in PowerShell.
6. When `config.cmd` asks for labels, keep the default labels `self-hosted`, `Windows`,
   and `X64`. The workflow needs these labels.
7. When `config.cmd` asks to run the runner as a service, type `Y`. Give the runner
   account for the service.
8. Install the job-started hook. Do this before the runner takes its first job.

The smoke test opens a window. A service runs in session 0, which has no desktop that
you can see. Tk can usually open a window there. If the step "Build and test the exe"
fails with "cannot open a window", do these steps:

1. Stop the service, and start the runner with `run.cmd` in a session where the
   runner account is logged on.
2. Or, make a scheduled task that starts `run.cmd` when the runner account logs on.

Use only the runner account for `run.cmd`. Never start the runner in your own logon
session, because then each job can read and change your files.

### Install the job-started hook

`runner_job_started.ps1` makes the runner refuse each job that is not a trusted build.
The runner runs a job only when all these conditions are true:

- The workflow is `windows-exe.yml` of this repository.
- The event is a push or a manual start.
- The ref is `master` or a `v*` tag.

The runner stops all other jobs in the step "Set up runner", and the job fails. A
manual start of `windows-exe.yml` from a branch that is not `master` also fails.

To install the hook, do these steps on the runner computer as an administrator:

1. Make the folder `C:\actions-runner-hooks`. Do not put the hook in the folder of the
   runner application, because GitHub tells you not to.
2. Copy `packaging\runner_job_started.ps1` from a clone of the repository into that
   folder.
3. Give the runner account read access to the folder, but not write access. Then a job
   cannot change the hook.
4. Add this line to the file `.env` in the folder of the runner application:

   ```text
   ACTIONS_RUNNER_HOOK_JOB_STARTED=C:\actions-runner-hooks\runner_job_started.ps1
   ```

5. Restart the runner service.
6. Start `windows-exe.yml` by hand on `master`. The log of the step "Set up runner" must
   show "runner_job_started: run the job".

When the hook changes in the repository, copy it again. If the repository gets a new
name or a new default branch, change the hook and its tests in
`tests/test_packaging.py`.

An ephemeral runner (`config.cmd --ephemeral`) is another option. It takes one job, and
then you must register it again. It does not refuse a job that it must not run. Thus
use the hook also on an ephemeral runner.

## Safety rule for the public repository

The repository is public. A pull request from a fork can contain any code, and also
new workflow files. For a pull request, GitHub runs the workflow files of the pull
request, not the files on `master`. Thus a fork can add a `pull_request` trigger to
`windows-exe.yml`, or use the labels of the runner in a different workflow. That code
must never run on the self-hosted runner, because the runner is your computer.

The rules in the workflow files do not stop an attacker. These controls protect the
runner:

- The job-started hook refuses each job that is not a trusted build. The runner itself
  enforces this control. Install the hook before the runner takes its first job.
- In **Settings > Actions > General**, under **Approval for running fork pull request
  workflows from contributors**, select **Require approval for all external
  contributors**.
- Before you approve the workflows of a pull request from a fork, read each change
  under `.github/`. Refuse the run if a change adds a `runs-on` with a label of the
  runner (`self-hosted`, `Windows`, or `X64`), or changes the triggers of
  `windows-exe.yml`.
- `windows-exe.yml` has no `pull_request` trigger and no `pull_request_target`
  trigger. Do not add them.
- Do not use the labels of the self-hosted runner in `ci.yml` or in a new workflow
  that a pull request starts.
- Keep the default permissions of the `GITHUB_TOKEN` at read access. Each job asks for
  the permissions that it needs.
