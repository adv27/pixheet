# PyInstaller spec of the Pixheet desktop app for Windows.
#
# Run it from the repository root. The default makes one file, dist/Pixheet.exe:
#
#     uv run pyinstaller --noconfirm --clean packaging/pixheet.spec
#
# To make the folder dist/Pixheet/ with Pixheet.exe in it, add "-- --onedir". The folder
# starts faster, because the exe does not extract itself each time:
#
#     uv run pyinstaller --noconfirm --clean packaging/pixheet.spec -- --onedir
#
# packaging/build.ps1 runs the build and tests the result.

import argparse
import sys
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

parser = argparse.ArgumentParser(prog="pixheet.spec")
parser.add_argument("--onedir", action="store_true", help="make a folder, not one file")
options = parser.parse_args()

HERE = Path(SPECPATH)
sys.path.insert(0, str(HERE))
from version_info import read_metadata  # noqa: E402

NAME = "Pixheet"
ICON = HERE / "pixheet.ico"
metadata = read_metadata(HERE.parent)

# The version resource: the Details tab of the file properties in Explorer shows it.
version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=metadata.numbers, prodvers=metadata.numbers),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # U.S. English, Unicode
                    [StringStruct(k, v) for k, v in metadata.strings(f"{NAME}.exe").items()],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(HERE / "pixheet_gui.py")],
    pathex=[],
    binaries=[],
    # gui.py reads the window icon from the folder of the app.
    datas=[(str(ICON), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow imports numpy only for type checks. Pixheet does not use numpy. Without this
    # line, the exe includes numpy when it is in the environment of the build.
    excludes=["numpy"],
    noarchive=False,
    optimize=0,
)

# The tkinterdnd2 hook of pyinstaller-hooks-contrib collects only the tkdnd folder of
# this platform and Tcl version, for example win-x64-tcl9. Make sure that it stays so.
tkdnd = {
    Path(dest).parts[2]
    for dest, _source, _kind in a.binaries + a.datas
    if Path(dest).parts[:2] == ("tkinterdnd2", "tkdnd") and len(Path(dest).parts) > 3
}
if len(tkdnd) != 1 or not next(iter(tkdnd)).startswith("win-x64"):
    raise SystemExit(f"Expected one win-x64 tkdnd folder in the bundle. Found: {sorted(tkdnd)}")

pyz = PYZ(a.pure)

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX makes the build slower and not reproducible, and antivirus programs flag it.
    upx=False,
    console=False,  # a windowed app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ICON)],
    version=version,
)

if options.onedir:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **common)
