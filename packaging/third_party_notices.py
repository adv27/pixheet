"""Write THIRD-PARTY-NOTICES.txt: the license texts of the third-party code in the app.

The spec calls :func:`write_notices` after the analysis, with the files of the bundle.
The function stops the build when a file of the bundle has no known owner, or when a
license text is missing. The spec puts the file in the app, and copies it to the dist
folder for the release. The texts come from these sources:

- Pixheet: the file LICENSE of the repository.
- Python: the file LICENSE.txt of the Python of the build (python-build-standalone).
- Pillow, sv-ttk, tkinterdnd2 and PyInstaller: the license files of the installed
  distributions. The file of Pillow also has the licenses of the libraries in its wheel.
- The files in packaging/notices/: fixed texts for the parts that come without a license
  file. Read :data:`FIXED` for the source of each text.
- Linux: the copyright file of the Debian package of each system library in the bundle.

The output does not change between two builds with the same inputs. The module needs
only the standard library. The tests import it.
"""

import fnmatch
import os
import posixpath
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
NOTICES = HERE / "notices"
FILE_NAME = "THIRD-PARTY-NOTICES.txt"
"""The name of the file in the app."""
DEBIAN_DOCS = Path("/usr/share/doc")
"""Each Debian package has its copyright file in DEBIAN_DOCS/<package>/copyright."""

FIXED = {
    # From the "full" archives of python-build-standalone 20261021 (python/licenses/).
    # The texts are the same on Windows and on Linux.
    "bzip2": "bzip2",
    "expat": "Expat",
    "libedit": "libedit",
    "libffi": "libffi",
    "liblzma": "liblzma (XZ Utils)",
    "libuuid": "libuuid (util-linux)",
    "libx11": "libX11",
    "libxau": "libXau",
    "libxcb": "libxcb",
    "mpdecimal": "mpdecimal",
    "ncurses": "ncurses",
    "openssl-3": "OpenSSL 3",
    "sqlite": "SQLite",
    "zlib": "zlib",
    "zlib-ng": "zlib-ng",
    "zstd": "Zstandard",
    # tcl_library/license.terms in tcl90.dll and tk_library/license.terms in tcl9tk90.dll
    # of the same Python. A test compares them on Windows.
    "tcl": "Tcl",
    "tk": "Tk",
    # LICENSE of https://github.com/libtom/libtommath. Tcl 9 includes libtommath.
    "libtommath": "libtommath",
    # license.terms of https://github.com/petasis/tkdnd. The tkinterdnd2 wheel includes
    # the tkdnd library, but not its license.
    "tkdnd": "tkdnd",
}
"""The fixed texts: the name of the file in packaging/notices/ (without .txt), and a title."""

MODULE_LIBRARIES = {
    "_bz2": ("bzip2",),
    "_ctypes": ("libffi",),
    "_curses": ("ncurses",),
    "_curses_panel": ("ncurses",),
    "_decimal": ("mpdecimal",),
    "_hashlib": ("openssl-3",),
    "_lzma": ("liblzma",),
    "_remote_debugging": ("zstd",),
    "_sqlite3": ("sqlite",),
    "_ssl": ("openssl-3",),
    "_tkinter": ("tcl", "tk", "libtommath", "zlib", "zlib-ng"),
    "_uuid": ("libuuid",),
    "_zstd": ("zstd",),
    "binascii": ("zlib", "zlib-ng"),
    "pyexpat": ("expat",),
    "readline": ("libedit", "ncurses"),
    "zlib": ("zlib", "zlib-ng"),
}
"""The libraries in each Python module, from PYTHON.json of python-build-standalone."""

LINUX_MODULE_LIBRARIES = {"_tkinter": ("libx11", "libxau", "libxcb")}
"""On Linux, libtcl9tk9.0.so also includes libX11, libXau and libxcb."""

MSVC_RUNTIME = """\
VCRUNTIME140.dll and VCRUNTIME140_1.dll are files of the Microsoft Visual C++
Redistributable. Microsoft lets programs include these files under the Microsoft
Software License Terms of Microsoft Visual Studio. Read
https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files.
"""

OWNERS = [
    # (owner, patterns of the destination in the bundle, in lower case)
    ("pixheet", ("pixheet.ico", "pixheet.png")),
    ("pillow", ("pil/*", "pillow.libs/*")),
    ("sv-ttk", ("sv_ttk/*",)),
    ("tkdnd", ("tkinterdnd2/tkdnd/*",)),
    ("msvc", ("vcruntime140.dll", "vcruntime140_1.dll")),
    ("libffi", ("libffi-*.dll",)),
    ("libtommath", ("libtommath.dll",)),
    ("zlib", ("zlib1.dll",)),
    ("tk", ("tcl9tk*.dll", "libtcl9tk*.so", "_tk_data/*")),
    ("tcl", ("tcl9*.dll", "libtcl9*.so", "_tcl_data/*", "tcl9/*")),
    (
        "python",
        ("base_library.zip", "python3*.dll", "*.pyd", "libpython3*.so*", "python3.*/lib-dynload/*"),
    ),
]
"""The owner of each file of the bundle. The first pattern that agrees wins."""

SYSTEM_LIBRARY = "lib*.so*"
"""On Linux, a library of the system that PyInstaller adds to the top of the bundle."""


class NoticeError(Exception):
    """A file of the bundle has no owner, or a license text is missing."""


@dataclass(frozen=True, slots=True)
class Notice:
    """One part of the app: a title, a short text about the part, and its license text."""

    title: str
    about: str
    text: str


def matches(name: str, pattern: str) -> bool:
    """Return True if the path ``name`` agrees with ``pattern``.

    A pattern without a slash agrees only with a file at the top of the bundle. Thus
    "*.pyd" does not take an extension module of a different package.
    """
    if "/" not in pattern and "/" in name:
        return False
    return fnmatch.fnmatchcase(name, pattern)


def owner(dest: str) -> str | None:
    """Return the owner of the file ``dest`` of the bundle, or None."""
    name = dest.replace("\\", "/").lower()
    for key, patterns in OWNERS:
        if any(matches(name, p) for p in patterns):
            return key
    return None


def bundled_modules(entries: Iterable[tuple[str, str, str]]) -> set[str]:
    """Return the names of the extension modules of the standard library in the bundle."""
    modules = set()
    for dest, _source, _kind in entries:
        path = PurePosixPath(dest.replace("\\", "/"))
        # Windows: _ctypes.pyd at the top. Linux: python3.15/lib-dynload/_tkinter.*.so.
        top_pyd = len(path.parts) == 1 and path.suffix == ".pyd"
        if top_pyd or "lib-dynload" in path.parts:
            modules.add(path.name.split(".")[0])
    return modules


def fixed_text(key: str) -> str:
    """Return the fixed license text ``key`` from packaging/notices/."""
    path = NOTICES / f"{key}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise NoticeError(f"The license text {path} is missing: {e}") from e


def distribution_license(name: str) -> str:
    """Return the license files of the installed distribution ``name``, joined."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError as e:
        raise NoticeError(f"The distribution {name} is not installed.") from e
    texts = []
    for file in dist.metadata.get_all("License-File") or []:
        text = dist.read_text(f"licenses/{file}") or dist.read_text(file)
        if text:
            texts.append(text)
    if not texts:
        raise NoticeError(f"The distribution {name} has no license file.")
    return "\n".join(texts)


def python_license(base_prefix: Path, stdlib: Path) -> str:
    """Return LICENSE.txt of the Python of the build.

    On Windows, the file is in the folder of python.exe. On Linux, it is in lib/python3.X/.
    """
    for path in (base_prefix / "LICENSE.txt", stdlib / "LICENSE.txt"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise NoticeError(f"The Python in {base_prefix} has no LICENSE.txt.")


def debian_copyright(source: str) -> tuple[str, str]:
    """Return the Debian package of the file ``source``, and its copyright file.

    dpkg knows a file by the path of the package. With a merged /usr, the same file can
    also have a path without /usr, or the other way around. Thus try both paths.
    """
    names = [source]
    real = os.path.realpath(source)
    if real.startswith("/"):
        names.append(real)  # the target of a symbolic link
    names += [n.removeprefix("/usr") if n.startswith("/usr/") else f"/usr{n}" for n in names]
    for name in dict.fromkeys(names):
        try:
            result = subprocess.run(
                ["dpkg-query", "--search", name], capture_output=True, text=True, check=False
            )
        except OSError as e:
            raise NoticeError(f"dpkg-query cannot run: {e}. Build on Debian 11.") from e
        if result.returncode == 0:
            package = result.stdout.splitlines()[0].split(":", 1)[0].strip()
            copyright_file = DEBIAN_DOCS / package / "copyright"
            try:
                return package, copyright_file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise NoticeError(f"The package {package} has no {copyright_file}.") from e
    raise NoticeError(f"No Debian package has the file {source}.")


def collect(
    entries: Sequence[tuple[str, str, str]],
    *,
    system: str,
    builtins: Iterable[str],
    root: Path,
    python_text: str,
    package_license: Callable[[str], str] = distribution_license,
    system_copyright: Callable[[str], tuple[str, str]] = debian_copyright,
) -> list[Notice]:
    """Return the notices for the bundle ``entries``: (destination, source, kind) tuples.

    ``system`` is "Windows" or "Linux". ``builtins`` are the names of the modules in the
    Python library of the build, ``sys.builtin_module_names``. Raise ``NoticeError`` when
    a file has no owner, or when a text is missing.
    """
    binaries: dict[str, list[str]] = {}
    packages: dict[str, tuple[str, list[str]]] = {}
    unknown = []
    names = {dest.replace("\\", "/") for dest, _source, kind in entries if kind != "SYMLINK"}
    for dest, source, kind in entries:
        name = dest.replace("\\", "/")
        if kind == "SYMLINK":
            # On Linux, PyInstaller adds a link at the top for each library in pillow.libs.
            # The source is the target of the link, relative to the folder of the link.
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), source))
            if target not in names:
                unknown.append(f"{name} -> {source}")
            continue
        key = owner(name)
        if key is None and system == "Linux" and matches(name, SYSTEM_LIBRARY):
            package, text = system_copyright(source)
            packages.setdefault(package, (text, []))[1].append(name)
        elif key is None:
            unknown.append(name)
        elif kind != "DATA":
            # Only the libraries. The spec removes some data files after this step.
            binaries.setdefault(key, []).append(name)
    if unknown:
        raise NoticeError(
            "These files of the bundle have no license owner in third_party_notices.py: "
            f"{sorted(unknown)}. Add the owner and its license text."
        )

    modules = sorted(set(builtins) | bundled_modules(entries))
    users: dict[str, list[str]] = {}
    for module in modules:
        libraries = MODULE_LIBRARIES.get(module, ())
        if system == "Linux":
            libraries += LINUX_MODULE_LIBRARIES.get(module, ())
        for library in libraries:
            users.setdefault(library, []).append(module)

    app = "Pixheet.exe" if system == "Windows" else "The Linux app"

    def about(text: str, key: str) -> str:
        """Add the files and the modules of the part ``key`` to ``text``."""
        if key in binaries:
            text += f" Files: {', '.join(sorted(binaries[key], key=str.lower))}."
        if key in users:
            text += f" Python modules: {', '.join(users[key])}."
        return text

    notices = [
        Notice(
            "Pixheet",
            about("The app itself.", "pixheet"),
            (root / "LICENSE").read_text(encoding="utf-8"),
        ),
        Notice(
            "Python",
            about("The Python interpreter and its standard library.", "python"),
            python_text,
        ),
        Notice(
            "PyInstaller",
            "The bootloader of PyInstaller starts the app. The license has an exception for it.",
            package_license("pyinstaller"),
        ),
        Notice("Pillow", about("Pillow opens the images.", "pillow"), package_license("pillow")),
        Notice("sv-ttk", about("The Sun Valley theme.", "sv-ttk"), package_license("sv-ttk")),
        Notice(
            "tkinterdnd2",
            "The Python part of drag and drop.",
            package_license("tkinterdnd2"),
        ),
        Notice("tkdnd", about("The Tk extension for drag and drop.", "tkdnd"), fixed_text("tkdnd")),
    ]
    if "msvc" in binaries:
        notices.append(Notice("Microsoft Visual C++ runtime", about("", "msvc"), MSVC_RUNTIME))

    libraries = ["tcl", "tk", *sorted(set(users) - {"tcl", "tk"})]
    for key in libraries:
        if key not in FIXED:
            raise NoticeError(f"No license text for the library {key}.")
        text = f"{app} includes {FIXED[key]}."
        notices.append(Notice(FIXED[key], about(text, key), fixed_text(key)))
    for package in sorted(packages):
        text, names = packages[package]
        about_package = (
            f"The Debian package {package} of the build system. Files: {', '.join(sorted(names))}."
        )
        notices.append(Notice(f"Debian package {package}", about_package, text))
    for notice in notices:
        if not notice.text.strip():
            raise NoticeError(f"The license text of {notice.title} is empty.")
    return notices


def render(notices: Sequence[Notice], system: str) -> str:
    """Return the text of THIRD-PARTY-NOTICES.txt for ``notices``."""
    app = "Pixheet.exe" if system == "Windows" else "The Pixheet app for Linux"
    rule = "=" * 78
    lines = [
        f"Third-party notices of Pixheet for {system}",
        "",
        f"{app} includes the software below. Each part has its own license.",
        "This file gives the license text of each part.",
        "",
        "Contents:",
        *(f"  {i}. {n.title}" for i, n in enumerate(notices, start=1)),
    ]
    for i, notice in enumerate(notices, start=1):
        text = notice.text.replace("\r\n", "\n").replace("\r", "\n")
        about = " ".join(notice.about.split())
        lines += [
            "",
            "",
            rule,
            f"{i}. {notice.title}",
            rule,
            "",
            about.strip(),
            "",
            text.strip("\n"),
        ]
    return "\n".join(lines) + "\n"


def write_notices(
    path: Path,
    entries: Sequence[tuple[str, str, str]],
    *,
    system: str,
    builtins: Iterable[str],
    root: Path,
    base_prefix: Path,
    stdlib: Path,
) -> list[Notice]:
    """Write THIRD-PARTY-NOTICES.txt for the bundle ``entries`` to ``path``.

    Return the notices. Raise ``NoticeError`` when a file has no owner, or when a text
    is missing.
    """
    notices = collect(
        entries,
        system=system,
        builtins=builtins,
        root=root,
        python_text=python_license(base_prefix, stdlib),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(notices, system).encode("utf-8"))
    return notices
