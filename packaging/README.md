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
| `release_version.py` | Reads the version from `pyproject.toml` for the release job. It also stops a pull request that changes the version to a version with a tag. |
| `check_exe.py` | Makes sure that a built exe starts, and that it converts test images of 11 formats and modes correctly. |
| `runner_job_started.ps1` | The job-started hook for a later self-hosted runner. It refuses each job that is not a trusted build. Read [Later: self-hosted runner](#later-self-hosted-runner). |

## Build on your computer

Run this command from the repository root:

```console
> powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script does these steps:

1. It installs the locked dependencies with the `gui` extra and the `build` group.
2. It builds `dist\Pixheet.exe` from `pixheet.spec`.
3. It runs `check_exe.py`. The exe must pass the smoke test with the light and the dark
   Sun Valley theme, and it must convert a test image correctly. Then the exe converts
   images of 11 formats and modes, for example AVIF, WebP, a CMYK JPEG with an ICC
   profile, and a LAB TIFF. Each sheet must be the same, byte for byte, as the sheet of
   `pixheet.convert` in Python. If the exe does not stop in 120 seconds, `check_exe.py`
   stops it and all processes that it started. Each run of the exe gets its own
   temporary folder in `TEMP` and `TMP`, and `check_exe.py` deletes it after the run.
   Thus a stopped exe cannot leave a `_MEI...` folder in `%TEMP%`.
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
finds a problem. A built app must also have the window icon, drag and drop, and the
Sun Valley theme of sv-ttk. The app also prints the name of its ttk theme.
`check_exe.py` runs the smoke test with `PIXHEET_THEME=light` and with
`PIXHEET_THEME=dark`. The name must be `sun-valley-light` or `sun-valley-dark`, not the
name of a standard Tk theme.

The exe shows the version of `pyproject.toml` in its properties. To change the icon,
edit `make_icon.py`, then run `uv run python packaging/make_icon.py`. Commit the script
and the new `pixheet.ico`. A test makes sure that the two agree.

## The size of the exe

The one-file exe is about 13.8 MB. Without the changes below, it is about
19.6 MB. `build.ps1` shows 13.2 MB, because it counts 1 MB as 1,048,576 bytes. The
Sun Valley theme adds about 55 KB. The spec
keeps all the features. It makes these changes. Each number is the saving of one change
alone:

| Change | Saves | Why it is safe |
|---|---|---|
| `EXCLUDES`: `urllib`, `http`, `email`, `ssl`, `hashlib` and more | about 2.5 MB, with OpenSSL | The app never opens a URL. `pathlib` imports `urllib` only in `Path.as_uri()` and `Path.from_uri()`. |
| `EXCLUDES`: `multiprocessing`, `decimal`, `xml`, `lzma`, `bz2`, `compression.zstd`, `argparse` and more | about 1.8 MB | pixheet uses threads, not processes. The xlsx file uses only deflate. Pillow needs `xml` only in `Image.getxmp()`. Only the CLI uses `argparse`, and the exe has no CLI. |
| `EXCLUDES`: `PIL.ImageTk`, `PIL.ImageQt`, `PIL.ImageShow`, `PIL.ImageWin`, `PIL.ImageFilter`, and the save-only PDF and Palm plugins | about 0.2 MB | pixheet never calls these modules. `PIL.ImageCms` stays, because LAB images need it. |
| `optimize=2` | about 0.5 MB | It removes the docstrings and the `assert` statements from the bytecode. No code of the app reads `__doc__`. The one `assert`, in `_xlsx.py`, is always true on Windows. |
| `trim_tcltk.py` | about 1.1 MB | It removes the time zone data, most encodings, and the Tk demo images. It also removes the messages for other languages, and the Tk scripts for X11 and macOS. It keeps all ttk themes, also `clam`, which the Sun Valley theme extends. |

These changes are not in the spec:

- UPX makes the exe about 4.5 MB smaller. But each start is about 0.4 s slower, and
  antivirus programs flag UPX-packed exes.
- AVIF support stays. `PIL._avif` is 4.3 MB, but AVIF is a real image format.

The spec also stops the build in these conditions:

- A module in `UNWANTED_MODULES` or a file in `UNWANTED_FILES` is in the bundle. A new
  hook or a new import can bring back an excluded module.
- A module in `REQUIRED_MODULES` or a file in `REQUIRED_FILES` is not in the bundle,
  for example `PIL._avif`, the tkdnd DLL, or the Tcl and PNG files of sv-ttk.
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
| `.github/workflows/ci.yml` | GitHub-hosted `windows-latest` and `ubuntu-latest` | pull request, push to `master`, manual | tests with coverage, `ruff check`, `ruff format --check`, upload of the coverage and the test results to Codecov; for a pull request, the check of the version tag |
| `.github/workflows/windows-exe.yml` | GitHub-hosted `windows-2025` | pull request, push to `master`, push of a `v*` tag, manual | tests, `build.ps1`, upload of the artifact `Pixheet-windows-x64` |

Each job runs on a new virtual machine. GitHub deletes the machine after the job. Thus
no file from an earlier job goes into a release. For the same reason, the build job does
not use the GitHub Actions cache.

### Codecov

The test job of `ci.yml` runs pytest with `--cov` and `--junitxml`. Then it sends
`coverage.xml` and `junit.xml` to [Codecov](https://codecov.io/gh/adv27/pixheet). These
rules apply:

- The upload uses the repository secret `CODECOV_TOKEN`. Never print the token.
- A pull request from a fork gets no secrets. Codecov accepts its upload without a
  token, because the repository is public. Then an upload error does not stop the job.
- `windows-exe.yml` sends nothing to Codecov. Thus the job that can write never gets a
  different secret.
- `codecov.yml` lets the coverage of the project go down by 1 % or less, and keeps the
  comment on a pull request short.

`pyproject.toml` configures coverage in `[tool.coverage.run]` and
`[tool.coverage.report]`. A plain `uv run pytest` does not measure coverage, so it stays
fast.

### Releases

To publish a release, change `version` in `pyproject.toml`, run `uv lock`, and merge the
pull request.

After the build job passes on `master`, the release job reads the version that the build
job gives. If the tag `v<version>` does not exist yet, the job makes the tag at the
commit of the push. Then it publishes `Pixheet.exe` and `Pixheet.exe.sha256` in the
GitHub release "Pixheet v<version>", with generated release notes. If the tag exists,
the job does nothing. The artifact of the build stays on the Actions page.

These rules apply to the release job:

- It runs on a GitHub-hosted Linux runner.
- It is the only job that can write to the repository.
- It never runs for a pull request, or for a manual start on a branch that is not
  `master`.
- Only one release job runs at a time. When two merges come quickly, the second job
  finds the tag of the first job and does nothing.
- A tag that the job makes with `GITHUB_TOKEN` does not start a new workflow run. This
  is correct, because the job publishes the release itself.

A pull request that changes the version fails in `ci.yml` when the tag `v<version>`
exists already. Without this check, the merge does not publish a release, and nobody
sees the problem.

You can also push a tag by hand: `git tag v0.2.0`, then `git push origin v0.2.0`. Then
the release job publishes the exe of the tag. The tag must agree with the version in
`pyproject.toml`, for example `v0.1.0` for version `0.1.0`.

The release job never replaces a file of a published release, because users can have
downloaded the file already. A new build of the same commit can also give a different
exe. For example, each job installs the newest patch release of Python 3.15. When the
job runs again for a tag, for example after a re-run, it fails if the
release already has `Pixheet.exe` or `Pixheet.exe.sha256`. To replace a file, delete it
in the release on GitHub, and then run the job again.

You can also protect the releases in the settings of the repository:

- Turn on immutable releases.
- Add a tag ruleset for `v*` that stops the update and the deletion of these tags. Do
  not stop the creation of the tags, because the release job makes them.

### Pinned actions

The workflows pin each action to a full commit SHA. The comment after the SHA gives the
version. When you update an action, update the SHA and the comment together. Then run
these linters:

```console
> uvx zizmor .github/workflows
> uvx --from actionlint-py actionlint
```

## Safety rule for the public repository

The repository is public. A pull request from a fork can contain any code, and also
new workflow files. For a pull request, GitHub runs the workflow files of the pull
request, not the files on `master`. These rules keep the repository safe:

- Use the `pull_request` trigger, not `pull_request_target`. For `pull_request`, a fork
  gets no secrets and a token that can only read.
- Keep the default permissions of the `GITHUB_TOKEN` in the repository settings at read
  access. Each job asks for the permissions that it needs. Only the release job can
  write.
- Use only GitHub-hosted runners for a workflow that a pull request starts.

## Later: self-hosted runner

The build job can move to a self-hosted Windows runner later, for example to build
faster. A self-hosted runner is your computer, and it keeps its files from job to job.
A pull request from a fork must never run code on it. To move the job, do these steps:

1. Remove the `pull_request` trigger from `windows-exe.yml`. You MUST do this step
   first.
2. Make a local Windows account only for the runner. Do not use an administrator
   account.
3. Install the job-started hook `runner_job_started.ps1` before the runner takes its
   first job. Then the runner refuses each job that is not `windows-exe.yml` from a push
   or a manual start on `master` or on a `v*` tag. Set the hook in the `.env` file of the
   runner with `ACTIONS_RUNNER_HOOK_JOB_STARTED`. Give the runner account only read
   access to the hook.
4. Add steps that delete the workspace before and after the job. Keep the uv cache and
   Python in `RUNNER_TEMP`. Then no file from an earlier job goes into a release.
5. Keep the release job on a GitHub-hosted runner. Then the token that can write never
   goes to your computer.
6. In **Settings > Actions > General**, require approval for the workflows of all
   external contributors.

The tests in `tests/test_packaging.py` check the hook. If the repository gets a new name
or a new default branch, change the hook and its tests.
