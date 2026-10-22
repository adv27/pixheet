# Packaging and CI

This folder holds the build of `Pixheet.exe`, the desktop app for Windows. This page is
for maintainers.

## Files

| File | Content |
|---|---|
| `pixheet_gui.py` | The entry script that PyInstaller freezes. |
| `pixheet.spec` | The PyInstaller spec. It makes one file by default. |
| `version_info.py` | Reads the version resource of the exe from `pyproject.toml` and `LICENSE`. |
| `pixheet.ico` | The icon of the exe and of the window, in 7 sizes from 16 to 256 pixels. |
| `make_icon.py` | Draws `pixheet.ico` with Pillow. |
| `build.ps1` | Builds the exe and tests it. |
| `check_exe.py` | Makes sure that a built exe converts a test image correctly. |

## Build on your computer

Run this command from the repository root:

```console
> powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script does these steps:

1. It installs the locked dependencies with the `gui` extra and the `build` group.
2. It builds `dist\Pixheet.exe` from `pixheet.spec`.
3. It starts `Pixheet.exe --smoke`. The exit code must be 0.
4. It runs `check_exe.py`, which converts a test image with the exe.
5. It writes `dist\Pixheet.exe.sha256`.

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

The workflows pin each action to a full commit SHA. The comment after the SHA gives the
version. When you update an action, update the SHA and the comment together. Then run
these linters:

```console
> uvx zizmor .github/workflows
> uvx --from actionlint-py actionlint
```

## Register the self-hosted runner

The runner must be a Windows x64 computer. It needs Git. The workflow installs uv, and
uv installs Python 3.15. The runner keeps the uv cache and the Python installation in
the profile of its user.

1. On GitHub, open **Settings > Actions > Runners**, and click **New self-hosted runner**.
2. Select **Windows** and **x64**.
3. Run the download and configure commands that GitHub shows, in PowerShell.
4. When `config.cmd` asks for labels, keep the default labels `self-hosted`, `Windows`,
   and `X64`. The workflow needs these labels.
5. When `config.cmd` asks to run the runner as a service, type `Y`.
6. Use a normal user account for the service, not an administrator account.

The smoke test opens a window. A service runs in session 0, which has no desktop that
you can see. Tk can usually open a window there. If the step "Build and test the exe"
fails with "cannot open a window", do these steps:

1. Stop the service, and start the runner with `run.cmd` in a session where the
   runner user is logged on.
2. Or, make a scheduled task that starts `run.cmd` when the runner user logs on.

## Safety rule for the public repository

The repository is public. A pull request from a fork can contain any code. That code
must never run on the self-hosted runner, because the runner is your computer.

- `windows-exe.yml` has no `pull_request` trigger and no `pull_request_target`
  trigger. Do not add them.
- Do not use the labels of the self-hosted runner in `ci.yml` or in a new workflow
  that a pull request starts.
- In **Settings > Actions > General**, under **Approval for running fork pull request
  workflows from contributors**, select **Require approval for all external
  contributors**.
- Keep the default permissions of the `GITHUB_TOKEN` at read access. Each job asks for
  the permissions that it needs.
