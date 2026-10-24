# Packaging and CI

This folder holds the build of the desktop app in one file: `Pixheet.exe` for Windows and
`Pixheet-linux-x86_64` for Linux. This page is for maintainers.

## Files

| File | Content |
|---|---|
| `pixheet_gui.py` | The entry script that PyInstaller freezes. |
| `pixheet.spec` | The PyInstaller spec, for Windows and Linux. It makes one file by default. It leaves unused modules out of the app. |
| `trim_tcltk.py` | Removes unused files from the Tcl and Tk library: in `tcl90.dll` and `tcl9tk90.dll` on Windows, from the data files on Linux. The spec runs it. |
| `linux_guards.py` | On Linux, reads each library of the bundle with `objdump -T`. It stops the build for a glibc newer than 2.27 or a libX11 older than 1.7. The spec runs it. |
| `third_party_notices.py` | Writes `THIRD-PARTY-NOTICES.txt`, the license texts of the third-party parts of the app. The spec runs it. Read [Third-party notices](#third-party-notices). |
| `notices/` | The fixed license texts of the parts that come without a license file, and of the software in Python. |
| `version_info.py` | Reads the version resource of the exe from `pyproject.toml` and `LICENSE`. |
| `pixheet.ico` | The icon of the exe and of the window on Windows, in 7 sizes from 16 to 256 pixels. |
| `pixheet.png` | The icon of the window on Linux, 128 pixels. |
| `make_icon.py` | Draws `pixheet.ico` and `pixheet.png` with Pillow. |
| `build.ps1` | Builds the exe on Windows and tests it. |
| `build.sh` | Builds the Linux app and tests it. Run it on Debian 11. |
| `release_version.py` | Reads the version from `pyproject.toml` for the release job. It also stops a pull request that changes the version to a version with a tag. |
| `release_notes.py` | Prints the release notes of a version: its section of `CHANGELOG.md`, then the download steps. The release job uses the output. The job "Check the version tag" of `ci.yml` runs it for each pull request. |
| `release_plan.py` | Decides the steps of the release job from the tag and the release on GitHub: make the tag, publish the missing files, or do nothing. |
| `check_exe.py` | Makes sure that a built app starts, and that it converts test images of 11 formats and modes correctly. It works on Windows and on Linux. |
| `runner_job_started.ps1` | The job-started hook for a later self-hosted runner. It refuses each job that is not a trusted build. Read [Later: self-hosted runner](#later-self-hosted-runner). |

## Build on your computer

Run this command from the repository root:

```console
> powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script does these steps:

1. It installs the locked dependencies with the `gui` extra and the `build` group.
2. It builds `dist\Pixheet.exe` from `pixheet.spec`. The spec also writes
   `dist\THIRD-PARTY-NOTICES-windows-x64.txt`, and it puts the same text in the exe.
3. It runs `check_exe.py`. The exe must pass the smoke test with the light and the dark
   Sun Valley theme, and it must convert a test image correctly. Then the exe converts
   images of 11 formats and modes, for example AVIF, WebP, a CMYK JPEG with an ICC
   profile, and a LAB TIFF. Each sheet must be the same, byte for byte, as the sheet of
   `pixheet.convert` in Python. If the exe does not stop in 120 seconds, `check_exe.py`
   stops it and all processes that it started.
4. It writes `dist\Pixheet.exe.sha256`.

`check_exe.py` gives each run of the exe its own temporary folder in `TEMP` and `TMP`,
and it deletes the folder after the run. Thus a stopped exe cannot leave a `_MEI...`
folder in `%TEMP%`.

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

uv uses only a Python that it installs itself (python-build-standalone), because
`pyproject.toml` sets `python-preference = "only-managed"`. This is also true when the
computer has a different Python 3.15. Thus CI, the release builds and a local build all
use python-build-standalone, not the Python of the system or of the runner image.

The uv version selects the release of python-build-standalone. CI and the release builds
use uv 0.13.5, which installs CPython 3.15.1 from python-build-standalone 20261021. A
local build uses the newest Python 3.15 that uv installed on your computer. This can be a
different patch release. Read "Pinned actions".

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

`check_exe.py` starts the app with `PIXHEET_HIDDEN_WINDOW=1` for each run. This
variable is for the build checks and the tests. The window opens off the screen and does
not take the focus. On Windows, the window also gets no taskbar button. Thus the checks
show no window on your desktop. The app does the same steps as without the variable.

The exe shows the version of `pyproject.toml` in its properties. To change the icon,
edit `make_icon.py`, then run `uv run python packaging/make_icon.py`. Commit the script,
the new `pixheet.ico` and the new `pixheet.png`. Tests make sure that they agree.

## Build the Linux app

Use Debian 11 or a `debian:11` container. First install these system packages. The job
`build-linux` of `release.yml` installs the same packages:

- `git` and `ca-certificates`. `git` clones the repository over HTTPS, and `build.sh`
  reads the time of the last commit.
- `binutils`. `linux_guards.py` uses its `objdump`.
- `libx11-6` and `libxcursor1`. The app includes these X libraries of the system.
- `xvfb`, `xauth` and `xfonts-base`. They give an X server with the core X fonts to the
  tests and to `check_exe.py`, the same as in CI.

Debian 11 gets no more updates, and its packages are on `archive.debian.org`. Thus the
apt sources must point to the archive, as in `release.yml`. In a `debian:11` container,
run these commands as root:

```console
# printf '%s\n' 'deb http://archive.debian.org/debian bullseye main' \
    'deb http://archive.debian.org/debian-security bullseye-security main' \
    > /etc/apt/sources.list
# apt-get -o Acquire::Check-Valid-Until=false update
# apt-get install -y git ca-certificates binutils libx11-6 libxcursor1 xvfb xauth xfonts-base
```

Then run this command from the repository root:

```console
$ bash packaging/build.sh
```

The script does the same steps as `build.ps1`, with these differences:

- It builds `dist/Pixheet` from the same `pixheet.spec`. The notices file is
  `dist/THIRD-PARTY-NOTICES-linux-x86_64.txt`.
- It runs `check_exe.py` under `xvfb-run` when `DISPLAY` is not set.
- It renames the file to `dist/Pixheet-linux-x86_64` and writes
  `dist/Pixheet-linux-x86_64.sha256`. With `--onedir`, it builds the folder
  `dist/Pixheet/` and does not rename it.
- PyInstaller uses `SOURCE_DATE_EPOCH` as the time stamp of the files in the bundle.

### Why Debian 11

PyInstaller copies the shared libraries of the build host into the app, for example
`libX11.so.6`, `libstdc++.so.6` and `libgcc_s.so.1`. These libraries set two limits:

- **glibc.** The app runs only on a system with the newest glibc version that a library
  of the bundle needs. Pillow already needs glibc 2.27. On Debian 11 (glibc 2.31), the
  libraries of the host need glibc 2.26 or less. Thus the app needs glibc 2.27: Ubuntu
  18.04, Debian 10, RHEL 8 and later. On Ubuntu 22.04, the app would need glibc 2.35. On
  the `ubuntu-latest` runner (Ubuntu 24.04), it would need glibc 2.39.
- **libX11.** The Tk of the uv Python (python-build-standalone) has its own static copy of
  Xlib. tkdnd uses the shared `libX11.so.6`. With a libX11 older than 1.7, the load of
  tkdnd stops the app with SIGABRT ("[xcb] Extra reply data still left in queue").
  Python cannot catch that. Debian 11 has libX11 1.7.2. Ubuntu 20.04 and RHEL 8 have
  1.6, so they are not good build hosts. Only libX11 1.7 and later export the function
  `XSetIOErrorExitHandler`.

`linux_guards.py` makes sure of both limits in the spec. It reads each library with
`objdump -T`. The build stops when a library needs a glibc newer than 2.27, or when the
bundled `libX11.so.6` does not export `XSetIOErrorExitHandler`. The log shows the floor,
for example `linux_guards: the app needs glibc 2.27 or later (GLIBC_2.27 in
PIL/_imagingmath...)`.

Before the app loads tkdnd, it does the same libX11 check (see `src/pixheet/_desktop.py`).
This check is necessary for a source install with `pip` or `uv` on Ubuntu 20.04 or RHEL 8.
There, the app runs without drag and drop and does not stop.

The app also checks the shared `libxcb.so.1`, because libX11 uses it. tkdnd needs libxcb
1.14 or later, and only these versions export the function `xcb_total_read`. The built
app has its own libX11 1.7.2, but it uses the libxcb of the system. Ubuntu 18.04, Debian
10 and RHEL 8 have libxcb 1.13. Thus the built app runs there without drag and drop.

Debian 11 is past its LTS. Its packages are on `archive.debian.org`, and they get no
more security updates. If the archive goes away, do these steps:

1. Change the container of the job `build-linux` in `release.yml` to a newer system with
   libX11 1.7, for example `ubuntu:22.04`.
2. Change `GLIBC_MAX` in `linux_guards.py` to the new floor.

You can test a build on a host with an older libX11, for example WSL with Ubuntu 20.04.
Copy `libX11.so.6` and `libxcb.so.1` of Debian 11 to a folder. Put that folder first in
`LD_LIBRARY_PATH`. PyInstaller then copies that libX11.

### Fonts and other limits on Linux

The Tk of python-build-standalone has no Xft and no fontconfig. Thus the app uses the
core X bitmap fonts, without antialiasing, and some letters show as boxes. Only a
different Python build or a Tk with Xft can fix this. The app also has no `.desktop`
file. The README of the repository lists the other limits.

## Third-party notices

The app includes third-party parts, and each part has its own license. The spec calls
`third_party_notices.py` with the files of the bundle. The script writes the license
texts to `THIRD-PARTY-NOTICES.txt`, and the spec puts this file in the app. The spec also
copies it to `dist`, as `THIRD-PARTY-NOTICES-windows-x64.txt` or
`THIRD-PARTY-NOTICES-linux-x86_64.txt`. The build artifact and the release include this
copy.

The build stops when a file of the bundle has no owner in `OWNERS`, or when a license text
is missing. The texts come from these sources:

- Pixheet: the file `LICENSE` of the repository.
- Python: the file `LICENSE.txt` of the Python of the build, then the licenses of the
  software in Python. `LICENSE.txt` does not have these licenses.
- Pillow, sv-ttk, tkinterdnd2 and PyInstaller: the license files of the installed
  distributions.
- `packaging/notices/`: the fixed texts. The texts of the libraries in Python come from
  `python/licenses/` in the full archives of python-build-standalone 20261021. The texts
  of Tcl and Tk are their `license.terms`. libtommath and tkdnd have the license files of
  their repositories. The files `python-*.txt` come from the section "Licenses and
  Acknowledgements for Incorporated Software" of `Doc/license.rst` of CPython 3.15.1.
  `PYTHON_PARTS` in `third_party_notices.py` lists them, with the module that has the
  code. The other sections are for code that is not in the app.
- On Linux: the file `/usr/share/doc/<package>/copyright` of the Debian package of each
  system library in the bundle. Thus the build needs `dpkg`. Debian 11 has these files.

After an update of python-build-standalone, for example after a change of the uv version
in the workflows, do these steps:

1. Get the full archive of the new release for Windows and for Linux.
2. Compare `python/licenses/` of the archive with the files in `packaging/notices/`.
3. Compare `PYTHON.json` of the archive with `MODULE_LIBRARIES` in
   `third_party_notices.py`.
4. For a new version of CPython, compare the section "Licenses and Acknowledgements for
   Incorporated Software" of its `Doc/license.rst` with the files `python-*.txt`.

## The size of the exe

The one-file exe is about 13.9 MB. Without the changes below, it is about 19.6 MB.
`build.ps1` shows 13.3 MB, because it counts 1 MB as 1,048,576 bytes. The Sun Valley
theme adds about 55 KB.

The Linux file is about 19.4 MB (`build.sh` shows 18.5 MB). On Linux, `strip` removes
the debug symbols of the libraries and saves about 3.8 MB. The trim saves about 0.6 MB.

The spec keeps all the features. It makes the changes in this table. Each number is the
saving of one change alone:

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
- A module in `REQUIRED_MODULES` or a file in `REQUIRED_FILES` is not in the bundle.
  Examples are `PIL._avif`, the tkdnd DLL, and the Tcl and PNG files of sv-ttk.
- `third_party_notices.py` finds a file of the bundle without an owner, or a license
  text is missing.
- `trim_tcltk.py` cannot read a Tcl or Tk DLL, or the DLL is not a PE file. A code
  signature also stops the build.
- `trim_tcltk.py` does not find a correct zip archive after the PE image of a DLL.
- `trim_tcltk.py` does not find each of the two DLLs one time.
- On Linux, `linux_guards.py` finds a library that needs a glibc newer than 2.27, or a
  `libX11.so.6` older than 1.7.

On Linux, the trim removes data files. It keeps the Tcl scripts of the file dialog and
of the message box, for example `tkfbox.tcl` and `msgbox.tcl`. Tk on X11 makes these
dialogs with Tcl. The smoke test of the built app makes sure that Tk finds them.

### Maintenance rule

The tests run pixheet in Python, which has all the modules. Thus, if new code in
pixheet or in the GUI imports a module in `EXCLUDES`, the tests pass, but the exe stops
with `ModuleNotFoundError`. A test finds a direct import of an excluded module in
`src/pixheet`. It does not find an import in a library that pixheet uses. Thus do
these steps after a change to the imports, or after an update of Pillow, Python, or
PyInstaller:

1. Run `packaging\build.ps1`, or `packaging/build.sh` on Linux. Its `check_exe.py` step
   converts images of many formats with the app.
2. If the exe needs an excluded module, remove the module from `EXCLUDES` and from
   `UNWANTED_MODULES`.

When the exe cannot import a module, it does not stop. The windowed exe shows the
traceback in a dialog, and the dialog stays open. Thus `check_exe.py` reports that the
exe did not stop in 120 s. The output of the exe then shows the `ModuleNotFoundError`.

## The workflows

| Workflow | Runner | Triggers | Steps |
|---|---|---|---|
| `.github/workflows/ci.yml` | GitHub-hosted `windows-latest` and `ubuntu-latest` | pull request, push to `master`, manual | tests with coverage, `ruff check`, `ruff format --check`, upload of the coverage and the test results to Codecov; for a pull request, the check of the version tag and of `CHANGELOG.md` |
| `.github/workflows/release.yml` | GitHub-hosted `windows-2025`; GitHub-hosted `ubuntu-latest` with the container `debian:11` | pull request, push to `master`, push of a `v*` tag, manual | job `build-windows`: tests, `build.ps1`, upload of the artifact `Pixheet-windows-x64`; job `build-linux`: tests, `build.sh`, upload of the artifact `Pixheet-linux-x86_64`; job `release` |

Each job runs on a new virtual machine. GitHub deletes the machine after the job. Thus
no file from an earlier job goes into a release. For the same reason, the build job does
not use the GitHub Actions cache.

### Codecov

The test jobs of `ci.yml` run pytest with `--cov` and `--junitxml`, on Windows and on
Linux. On Linux, `xvfb-run` gives the GUI tests an X server. Then each job sends
`coverage.xml` and `junit.xml` to [Codecov](https://codecov.io/gh/adv27/pixheet), with
the flag `Windows` or `Linux`. Codecov merges the two reports. These rules apply:

- The upload uses the repository secret `CODECOV_TOKEN`. Never print the token.
- A pull request from a fork gets no secrets. Codecov accepts its upload without a
  token, because the repository is public. Then an upload error does not stop the job.
- `release.yml` sends nothing to Codecov. Thus the job that can write never gets a
  different secret.
- `codecov.yml` lets the coverage of the project go down by 1 % or less, and keeps the
  comment on a pull request short.

`pyproject.toml` configures coverage in `[tool.coverage.run]` and
`[tool.coverage.report]`. A plain `uv run pytest` does not measure coverage, so it stays
fast.

### Releases

To publish a release, do these steps in a pull request:

1. Add a section `## <version>` at the top of `CHANGELOG.md`, with the changes of the
   version.
2. Change `version` in `pyproject.toml`, and run `uv lock`.
3. Merge the pull request.

After both build jobs pass on `master`, the release job reads the version that the build
job gives. If the tag `v<version>` does not exist yet, the job makes the tag at the
commit of the push. Then it publishes six files in the GitHub release "Pixheet
v<version>":

- `Pixheet.exe`, `Pixheet.exe.sha256` and `THIRD-PARTY-NOTICES-windows-x64.txt`.
- `Pixheet-linux-x86_64`, `Pixheet-linux-x86_64.sha256` and
  `THIRD-PARTY-NOTICES-linux-x86_64.txt`.

The job waits for both build jobs. When one build fails, the job does not run.

The release notes have three parts:

1. The section of the version in `CHANGELOG.md`.
2. The download steps of `release_notes.py`.
3. The list of the pull requests. `gh release create --generate-notes` makes it.

The job writes the notes before it makes the tag. Thus a missing section stops the job
before the tag.

When the tag exists, `release_plan.py` compares the commit of the tag with the commit of
the push:

- The tag is at the commit of the push. An earlier run of the job made the tag, and then
  it stopped, for example for a network problem. The job creates the release if it does
  not exist. Then it uploads the files that the release does not have.
- The tag is at a different commit. That version has a release already. The job does
  nothing. The artifact of the build stays on the Actions page.

These rules apply to the release job:

- It runs on a GitHub-hosted Linux runner.
- It is the only job that can write to the repository.
- It never runs for a pull request, or for a manual start on a branch that is not
  `master`.
- Only one release job runs at a time. Two quick merges can have the same version. Then
  the second job finds the tag of the first job at a different commit, and it does
  nothing.
- A tag that the job makes with `GITHUB_TOKEN` does not start a new workflow run. This
  is correct, because the job publishes the release itself.

GitHub keeps only one waiting run of `release.yml` for `master`. Thus three quick pushes
to `master` have this result:

1. The run of the first push starts, and the run of the second push waits.
2. The third push cancels the waiting run of the second push.
3. The run of the third push releases the same version at its own, later commit.

Thus the tag of a version can be at a later commit than the change of the version. A
version that a newer version replaces before its run starts gets no release. To tag a
version at a given commit, push the tag `v<version>` at that commit.

For each pull request, the job "Check the version tag" of `ci.yml` makes these checks:

- The tag `v<version>` must not exist yet when the pull request changes the version.
  Without this check, the merge does not publish a release, and nobody sees the problem.
- `CHANGELOG.md` must have a section for the version in `pyproject.toml`.

You can also push a tag by hand: `git tag v0.2.0`, then `git push origin v0.2.0`. Then
the release job publishes the apps of the tag. The tag must agree with the version in
`pyproject.toml`, for example `v0.1.0` for version `0.1.0`.

The release job never replaces a file of a published release, because users can have
downloaded the file already. A new build of the same commit can also give a different
exe, for example after a change of the uv version in the workflows. When the job runs
again for a tag, for example after a re-run, it uploads only the files that the release
does not have. It stops when the release has a file but not its `.sha256` file,
or the opposite, because the new checksum can be different. To replace a file, delete it
in the release on GitHub, and then run the job again.

`gh release create` makes a draft, uploads the files, and then publishes the draft. When
the job stops during the upload, for example after a cancel, the draft stays. Users
cannot see a draft. When the job runs again, it uploads the missing files to the draft,
and then publishes the draft.

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

The job `build-linux` pins its container image by digest, the same as an action:
`debian:11@sha256:6f519a81...`. No tool updates the digest. To get the current digest,
run `docker buildx imagetools inspect debian:11`. Debian 11 gets no more updates, so the
digest changes rarely.

The three `setup-uv` steps install one exact uv version, 0.13.5, with the `version`
input. One step is in `ci.yml`, and one step is in each build job of `release.yml`. To
change the uv of CI, change the three steps together. A test in `tests/test_packaging.py`
makes sure that they agree. `pyproject.toml` accepts uv 0.13.1 or later
(`required-version`), so a newer uv on your computer also works.

The uv version also selects the release of python-build-standalone, and thus the Python
in the apps. uv 0.13.5 installs CPython 3.15.1 from python-build-standalone 20261021. To
see the Python of a uv version, run `uv python list --only-downloads`. After a change of
the uv version, do the steps in "Third-party notices", because the new Python can have
different licenses.

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

1. Remove the `pull_request` trigger from `release.yml`. You MUST do this step
   first.
2. Make a local Windows account only for the runner. Do not use an administrator
   account.
3. Install the job-started hook `runner_job_started.ps1` before the runner takes its
   first job. Then the runner refuses each job that is not `release.yml` from a push
   or a manual start on `master` or on a `v*` tag. Set the hook in the `.env` file of the
   runner with `ACTIONS_RUNNER_HOOK_JOB_STARTED`. Give the runner account only read
   access to the hook.
4. Add steps that delete the workspace before and after the job. Keep the uv cache and
   Python in `RUNNER_TEMP`. Then no file from an earlier job goes into a release. By
   default, uv puts its Python in the uv folder of the runner account, and that folder
   stays from job to job. Thus set `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` to folders
   in `RUNNER_TEMP`.
5. Keep the release job on a GitHub-hosted runner. Then the token that can write never
   goes to your computer.
6. In **Settings > Actions > General**, require approval for the workflows of all
   external contributors.

The tests in `tests/test_packaging.py` check the hook. If the repository gets a new name
or a new default branch, change the hook and its tests.
