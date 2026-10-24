"""Check the shared libraries of the Linux app before PyInstaller packs them.

The spec runs these checks on Linux. They stop the build in two cases:

1. A library in the bundle needs a newer glibc than ``GLIBC_MAX``. The app then does not
   start on older systems. The libraries of the build host set this floor, so build on
   Debian 11. Pillow already needs glibc 2.27.
2. The bundle has a libX11 older than 1.7. tkdnd uses it, and with it the load of tkdnd
   stops the app with SIGABRT. Only libX11 1.7 and later export XSetIOErrorExitHandler.

The checks read the dynamic symbol table of each file with ``objdump -T`` (binutils).
Run the module by hand to see the floor of a file or of a folder:

    python packaging/linux_guards.py dist/Pixheet/
"""

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

GLIBC_MAX = (2, 27)
"""The newest glibc version that the app can need: Ubuntu 18.04, Debian 10, RHEL 8."""
X11_NAME = "libX11.so.6"
X11_SYMBOL = "XSetIOErrorExitHandler"
"""A function that only libX11 1.7 and later export."""
ELF_MAGIC = b"\x7fELF"

_GLIBC = re.compile(r"\bGLIBC_(\d+)\.(\d+)(?:\.\d+)?\b")

type Version = tuple[int, int]


class GuardError(Exception):
    """objdump cannot read a file."""


def glibc_floor(objdump_text: str) -> Version | None:
    """Return the newest glibc version in the output of ``objdump -T``, or None."""
    versions = {(int(m[1]), int(m[2])) for m in _GLIBC.finditer(objdump_text)}
    return max(versions, default=None)


def exports(objdump_text: str, symbol: str) -> bool:
    """Return True if the output of ``objdump -T`` has ``symbol`` as a defined symbol.

    A line with ``*UND*`` is a symbol that the file uses from a different library.
    """
    for line in objdump_text.splitlines():
        fields = line.split()
        if fields and fields[-1] == symbol and "*UND*" not in fields:
            return True
    return False


def objdump(path: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Return the dynamic symbol table of the ELF file ``path``. Raise GuardError."""
    try:
        result = run(["objdump", "-T", str(path)], capture_output=True, text=True, check=False)
    except OSError as e:
        raise GuardError(f"Cannot run objdump: {e}. Install binutils.") from e
    if result.returncode != 0:
        raise GuardError(f"objdump cannot read {path}: {result.stderr.strip()}")
    return result.stdout


def is_elf(path: Path) -> bool:
    """Return True when ``path`` is an ELF file."""
    try:
        with path.open("rb") as file:
            return file.read(4) == ELF_MAGIC
    except OSError:
        return False


def check_bundle(
    binaries: Iterable[tuple[str, str, str]],
    glibc_max: Version = GLIBC_MAX,
    dump: Callable[[Path], str] = objdump,
) -> tuple[Version | None, str, list[str]]:
    """Check the binaries of PyInstaller: a list of (destination, source, kind).

    Return the glibc floor, the destination of the file that sets it, and the problems.
    """
    floor: Version | None = None
    floor_file = ""
    problems = []
    x11_found = False
    for dest, source, _kind in binaries:
        path = Path(source)
        if not is_elf(path):
            continue
        text = dump(path)
        version = glibc_floor(text)
        if version is not None and (floor is None or version > floor):
            floor, floor_file = version, dest
        if Path(dest).name == X11_NAME:
            x11_found = True
            if not exports(text, X11_SYMBOL):
                problems.append(
                    f"The {X11_NAME} of the build host ({source}) is older than 1.7. tkdnd "
                    "stops the app with it. Build on a host with libX11 1.7 or later, "
                    "for example Debian 11."
                )
    if not x11_found:
        problems.append(f"The bundle has no {X11_NAME}. tkdnd needs it.")
    if floor is not None and floor > glibc_max:
        problems.append(
            f"{floor_file} needs GLIBC_{floor[0]}.{floor[1]}. The limit is "
            f"GLIBC_{glibc_max[0]}.{glibc_max[1]}. Build on an older host, for example "
            "Debian 11."
        )
    return floor, floor_file, problems


def describe(floor: Version | None, floor_file: str) -> str:
    """Return the line that tells the glibc floor of the bundle."""
    if floor is None:
        return "linux_guards: the bundle needs no versioned glibc symbol."
    return (
        f"linux_guards: the app needs glibc {floor[0]}.{floor[1]} or later "
        f"(GLIBC_{floor[0]}.{floor[1]} in {floor_file})."
    )


def main(paths: list[str]) -> int:
    """Show the glibc floor of the ELF files in ``paths`` (files or folders)."""
    files = []
    for name in paths:
        path = Path(name)
        files += sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    binaries = [(str(f), str(f), "BINARY") for f in files]
    try:
        floor, floor_file, _problems = check_bundle(binaries, glibc_max=(99, 0))
    except GuardError as e:
        print(f"linux_guards: {e}", file=sys.stderr)
        return 1
    print(describe(floor, floor_file))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
