# PyInstaller spec of the Pixheet desktop app for Windows.
#
# Run it from the repository root. The default makes one file, dist/Pixheet.exe:
#
#     uv run --extra gui --group build pyinstaller --noconfirm --clean packaging/pixheet.spec
#
# To make the folder dist/Pixheet/ with Pixheet.exe in it, add "-- --onedir". The folder
# starts faster, because the exe does not extract itself each time:
#
#     uv run --extra gui --group build pyinstaller --noconfirm --clean packaging/pixheet.spec -- --onedir
#
# packaging/build.ps1 runs the build and tests the result.
#
# This spec makes the exe smaller with these changes. Each change keeps all the features:
#   1. EXCLUDES removes standard library modules that the app never imports at run time.
#   2. EXCLUDES also removes Pillow modules that pixheet never uses.
#   3. optimize=2 removes the docstrings and the assert statements from the bytecode.
#   4. trim_tcltk.py removes files that the app does not use from the Tcl and Tk DLLs.
# The spec does not use UPX or strip. See the comments at each change. Together, the
# changes make the one-file exe about 13.8 MB, from 19.6 MB.
#
# If new code in pixheet or in the GUI imports a module in EXCLUDES, the tests pass, but
# the exe stops with ModuleNotFoundError. After such a change, run packaging/build.ps1.
# Its check_exe.py step converts many image formats with the exe.

import argparse
import fnmatch
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
from trim_tcltk import trim_binaries  # noqa: E402
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

EXCLUDES = [
    # Pillow imports numpy only for type checks. Pixheet does not use numpy. Without this
    # line, the exe includes numpy when it is in the environment of the build.
    "numpy",
    # --- Standard library: multiprocessing ---
    # pixheet uses only ThreadPoolExecutor. concurrent.futures imports .process only
    # through __getattr__, when code asks for ProcessPoolExecutor. Without this chain, the
    # multiprocessing and pkgutil runtime hooks also go.
    "multiprocessing",
    "concurrent.futures.process",
    "_multiprocessing",
    "xmlrpc",
    "decimal",
    "_decimal",
    "_pydecimal",
    "statistics",
    "pickle",
    "_compat_pickle",
    "pprint",
    "secrets",
    "runpy",
    "tracemalloc",
    # --- Standard library: network, crypto and mail (with OpenSSL, about 2.5 MB of DLLs) ---
    # pathlib imports urllib only in Path.as_uri() and Path.from_uri(). The app does not
    # call them. This removes libcrypto-3-x64.dll and libssl-3-x64.dll.
    "urllib",
    "http",
    "email",
    "ftplib",
    "netrc",
    "mimetypes",
    "ssl",
    "_ssl",
    "hashlib",
    "_hashlib",
    "hmac",
    "importlib.metadata",
    # --- Standard library: xml ---
    # PIL.Image imports ElementTree only under TYPE_CHECKING and in Image.getxmp(). The
    # app does not call getxmp(), and getxmp() needs defusedxml, which is not installed.
    "xml",
    "pyexpat",
    "_elementtree",
    # --- Standard library: zip compressors other than deflate ---
    # The xlsx file uses ZIP_DEFLATED (zlib). zipfile and shutil import the other
    # compressors in try/except ImportError.
    "lzma",
    "_lzma",
    "bz2",
    "_bz2",
    "compression.zstd",  # do not exclude "compression": gzip (FITS) needs it
    "_zstd",
    "tarfile",
    "py_compile",
    # --- Standard library: command line helpers that only main() blocks import ---
    "argparse",
    "gettext",
    "getopt",
    "sysconfig",
    "_aix_support",
    # --- Pillow: modules that pixheet never uses ---
    # Each one is imported only in a function that pixheet does not call, or is a plugin
    # that only saves files. Keep PIL.ImageCms and PIL._imagingcms: Image.convert() uses
    # them for LAB images, for example a LAB TIFF.
    "PIL.ImageTk",  # Image to Tk PhotoImage; also removes PIL._imagingtk
    "PIL._imagingtk",
    "PIL.ImageQt",  # Qt conversion
    "PIL.ImageShow",  # Image.show(); also removes shlex
    "PIL.ImageWin",  # ImageCms.get_display_profile() and ImageGrab only
    "PIL.ImageFilter",  # Image.filter() only
    "PIL.PdfImagePlugin",  # PDF is save-only in Pillow
    "PIL.PdfParser",
    "PIL.PalmImagePlugin",  # Palm is save-only in Pillow
]

# The build stops if one of these modules, or a module in one of these packages, is in
# the bundle. A new hook or a new import can bring them back.
UNWANTED_MODULES = [
    "numpy",
    "multiprocessing",
    "concurrent.futures.process",
    "ssl",
    "_ssl",
    "hashlib",
    "_hashlib",
    "decimal",
    "_decimal",
    "urllib",
    "http",
    "email",
    "xml",
    "pyexpat",
    "_elementtree",
    "_lzma",
    "_bz2",
    "_zstd",
    "test",
    "tkinter.test",
    "unittest",
    "PIL.ImageTk",
    "PIL._imagingtk",
    "PIL.ImageQt",
    "PIL.ImageShow",
]
# The build stops if one of these files is in the bundle.
UNWANTED_FILES = ["libcrypto-3-x64.dll", "libssl-3-x64.dll"]

# The build stops if one of these modules is not in the bundle. The image formats need
# the Pillow extension modules. LAB images need PIL._imagingcms. sv_ttk gives the window
# the Sun Valley theme.
REQUIRED_MODULES = [
    "pixheet.gui",
    "tkinter",
    "_tkinter",
    "tkinterdnd2",
    "sv_ttk",
    "PIL._imaging",
    "PIL._imagingcms",
    "PIL.ImageCms",
    "PIL._webp",
    "PIL._avif",
]
# The build stops if no file in the bundle agrees with one of these patterns.
# The sv-ttk hook of pyinstaller-hooks-contrib collects the Tcl scripts and images of
# the theme.
REQUIRED_FILES = [
    "tcl90.dll",
    "tcl9tk90.dll",
    "tkinterdnd2/tkdnd/win-x64-*/libtkdnd*.dll",
    "sv_ttk/sv.tcl",
    "sv_ttk/theme/light.tcl",
    "sv_ttk/theme/dark.tcl",
    "sv_ttk/theme/sprites_light.tcl",
    "sv_ttk/theme/sprites_dark.tcl",
    "sv_ttk/theme/spritesheet_light.png",
    "sv_ttk/theme/spritesheet_dark.png",
]

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
    excludes=EXCLUDES,
    noarchive=False,
    # Remove the docstrings and the assert statements from the frozen bytecode. No code
    # of the app reads __doc__. sys.flags.optimize stays 0 in the exe.
    optimize=2,
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

# Make sure that the excludes work, and that the bundle has all that the app needs.
files = [dest.replace("\\", "/").lower() for dest, _source, _kind in a.binaries + a.datas]
modules = {name.lower() for name, _source, _kind in a.pure}
# An extension module, for example PIL/_avif.cp315-win_amd64.pyd, is the module PIL._avif.
modules |= {f.rsplit(".", 2)[0].replace("/", ".") for f in files if f.endswith(".pyd")}
unwanted = sorted(
    {u for u in UNWANTED_MODULES for m in modules if f"{m}.".startswith(f"{u.lower()}.")}
)
unwanted += [f for f in files if Path(f).name in UNWANTED_FILES]
if unwanted:
    raise SystemExit(f"These excluded modules or files are in the bundle: {unwanted}")
missing = [m for m in REQUIRED_MODULES if m.lower() not in modules]
missing += [p for p in REQUIRED_FILES if not any(fnmatch.fnmatchcase(f, p) for f in files)]
if missing:
    raise SystemExit(f"These modules or files are not in the bundle: {missing}")

# Tcl 9 and Tk 9 keep their script library in a zip archive after the PE image of
# tcl90.dll and tcl9tk90.dll. trim_tcltk.py writes a copy of each DLL with a smaller
# archive: the exe gets about 1.08 MB smaller. The tool stops the build if a DLL has a
# different layout. Read trim_tcltk.py for the list of the removed files.
if sys.platform == "win32":
    a.binaries = trim_binaries(a.binaries, Path(workpath) / "tcltk-trim")

pyz = PYZ(a.pure)

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    # strip has no effect on Windows: no strip tool is on PATH. A MinGW strip on PATH
    # would run on MSVC binaries, which is not supported.
    strip=False,
    # UPX makes the exe about 4.5 MB smaller. But each start is about 0.4 s slower, and
    # antivirus programs flag UPX-packed exes.
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
