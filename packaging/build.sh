#!/usr/bin/env bash
# Build the Pixheet desktop app for Linux and test the result.
#
# The script does these steps:
#   1. Install the locked dependencies with uv: the gui extra and the build group.
#   2. Build dist/Pixheet with PyInstaller from packaging/pixheet.spec. Two builds of one
#      commit with the same tools give the same file. The spec stops the build when the
#      bundle needs a glibc newer than 2.27 or has a libX11 older than 1.7. Thus build on
#      Debian 11 (see packaging/README.md).
#   3. Test the app with packaging/check_exe.py. The app must pass the smoke test
#      (--smoke) and convert a test image correctly. It must also convert images of many
#      formats to the same sheets as pixheet in Python. Without DISPLAY, the script runs
#      the check under xvfb-run.
#   4. Rename the file to dist/Pixheet-linux-<machine>, for example
#      dist/Pixheet-linux-x86_64, and write its .sha256 file.
# The script stops with a non-zero exit code when a step fails.
#
# Usage, from any folder:
#
#     bash packaging/build.sh            # one file
#     bash packaging/build.sh --onedir   # the folder dist/Pixheet/, no rename
set -euo pipefail

onedir=0
case "${1:-}" in
  "") ;;
  --onedir) onedir=1 ;;
  *)
    echo "usage: bash packaging/build.sh [--onedir]" >&2
    exit 2
    ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

step() {
  echo "==> $1"
}

# uv.lock pins the exact versions of PyInstaller, pyinstaller-hooks-contrib and
# setuptools. See the same step in build.ps1.
step "Install the dependencies"
uv sync --locked --extra gui --group build

# Use a PyInstaller cache folder in the git-ignored build folder, not the cache folder of
# the user. See the same step in build.ps1.
export PYINSTALLER_CONFIG_DIR="$root/build/pyinstaller-config"

# Make the build reproducible. PYTHONHASHSEED=0 keeps the files in base_library.zip in the
# same order. PyInstaller uses SOURCE_DATE_EPOCH as the time stamp of the files. The
# script uses the time of the commit, or a value that the user sets.
epoch="${SOURCE_DATE_EPOCH:-}"
if [[ -z "$epoch" ]]; then
  epoch="$(git -C "$root" log -1 --format=%ct 2> /dev/null || true)"
fi
if [[ ! "$epoch" =~ ^[0-9]+$ ]]; then
  # There is no git or no commit. Use 1980-01-01, the first date that a zip file can keep.
  epoch=315532800
fi

spec_args=()
if [[ $onedir -eq 1 ]]; then
  spec_args=(-- --onedir)
fi
step "Build the app"
# The check and the app run without these values.
PYTHONHASHSEED=0 SOURCE_DATE_EPOCH="$epoch" \
  uv run --locked --extra gui --group build \
  pyinstaller --noconfirm --clean packaging/pixheet.spec "${spec_args[@]}"

if [[ $onedir -eq 1 ]]; then
  app="$root/dist/Pixheet/Pixheet"
else
  app="$root/dist/Pixheet"
fi
if [[ ! -f "$app" ]]; then
  echo "build.sh: $app does not exist." >&2
  exit 1
fi

# The app needs an X server. check_exe.py has a time limit, and then it stops the whole
# process tree. Thus the build cannot hang here.
step "Test the app: smoke test and conversion"
check=(uv run --locked --extra gui --group build python packaging/check_exe.py "$app")
if [[ -n "${DISPLAY:-}" ]]; then
  "${check[@]}"
else
  xvfb-run -a "${check[@]}"
fi

if [[ $onedir -eq 0 ]]; then
  final="$root/dist/Pixheet-linux-$(uname -m)"
  mv -f "$app" "$final"
  app="$final"
fi
name="$(basename "$app")"
# The format of sha256sum: "<hash> *<name>". Test it with: sha256sum -c <name>.sha256
(cd "$(dirname "$app")" && sha256sum --binary "$name" > "$name.sha256")
hash="$(cut -d ' ' -f 1 "$app.sha256")"
size="$(stat -c %s "$app")"
echo "build.sh: built $app ($(awk "BEGIN { printf \"%.1f\", $size / 1048576 }") MB)"
echo "build.sh: SHA256 $hash"
