"""Remove files that the app does not use from the Tcl and Tk library in the exe.

Tcl 9 and Tk 9 keep their script library in a zip archive. The archive follows the PE
image in tcl90.dll and in tcl9tk90.dll. Thus PyInstaller cannot remove these files with
a filter on the data files. This tool writes a copy of each DLL. The copy has the same
PE image, a smaller archive, and a correct PE checksum.

The tool removes these files:

- The time zone data, the clock messages, and the packages http, cookiejar and tcltest.
- All encodings except a small set. Tcl 9 on Windows uses utf-8 as the system encoding,
  and utf-8 and utf-16 are built in.
- The Tk demo images and the Tk messages for languages other than English.
- Tk scripts for X11 and macOS, and the Tcl versions of dialogs that Windows shows
  natively. Tk does not load them on Windows.

The tool keeps all the scripts that Tk loads on Windows, and all the ttk themes. It
stops the build when the DLL has a different layout. Check the exe again after an
update of Python or of Tcl and Tk. Run the tool by hand with:

    python packaging/trim_tcltk.py <source DLL> <target DLL>

On Linux, the Tcl and Tk library is a folder of files, not a zip archive. PyInstaller
collects the files as data in _tcl_data/, _tk_data/ and tcl9/. :func:`trim_datas` removes
files from that list with the rules of :func:`linux_dropped`. The rules are not the same
as on Windows: Tk on X11 makes its file dialog and its message box with Tcl scripts, so
the tool keeps these scripts.
"""

import fnmatch
import io
import struct
import sys
import zipfile
from array import array
from collections.abc import Iterable
from pathlib import Path

DLLS = ("tcl90.dll", "tcl9tk90.dll")
"""The DLLs that have a zip archive after the PE image, in lower case."""

KEEP_ENCODINGS = {"ascii", "cp1252", "cp1258", "cp437", "iso8859-1"}
KEEP_TK_MSGS = {"en.msg", "en_gb.msg"}
KEEP_ENCODINGS_LINUX = {"ascii", "cp1252", "cp437", "iso8859-1", "iso8859-15", "utf-8"}
"""The encodings that the Linux app keeps. Tcl uses the encoding of the locale."""

DROP = [
    # Data that the app never reads.
    "tcl_library/tzdata/*",
    "tcl_library/msgs/*",
    "tcl_library/cookiejar/*",
    "tcl_library/http/*",
    "tcl_library/tcltest/*",
    "tk_library/images/*",
    # Tk scripts for X11 or macOS, and Tcl versions of dialogs that Windows shows natively.
    "tk_library/xmfbox.tcl",
    "tk_library/tkfbox.tcl",
    "tk_library/choosedir.tcl",
    "tk_library/iconlist.tcl",
    "tk_library/clrpick.tcl",
    "tk_library/fontchooser.tcl",
    "tk_library/msgbox.tcl",
    "tk_library/mkpsenc.tcl",
    "tk_library/safetk.tcl",
    "tk_library/console.tcl",
    "tk_library/ttk/aquaTheme.tcl",
    # Tcl: safe interpreters, optparse, platform, the install helper and the ICU bridge.
    "tcl_library/safe.tcl",
    "tcl_library/opt/*",
    "tcl_library/platform/*",
    "tcl_library/install.tcl",
    "tcl_library/icu.tcl",
]

DROP_LINUX = [
    # Data and packages that the app never reads. The names are in lower case.
    "_tcl_data/tzdata/*",
    "_tcl_data/msgs/*",
    "_tcl_data/cookiejar*/*",
    "_tcl_data/http*/*",
    "_tcl_data/tcltest*/*",
    "_tcl_data/opt*/*",
    "_tcl_data/platform*/*",
    "tcl9/*/http-*",
    "tcl9/*/tcltest-*",
    "tcl9/*/platform*",
    "tcl9/*/cookiejar*",
    "_tk_data/images/*",
    "_tk_data/demos/*",
    # Tcl: safe interpreters, the install helper, the ICU bridge, and C sources.
    "_tcl_data/safe.tcl",
    "_tcl_data/install.tcl",
    "_tcl_data/icu.tcl",
    "_tcl_data/tclappinit.c",
    # Tk: the ttk themes of Windows and macOS, the console, and C sources. Keep the Tcl
    # dialogs: tkfbox.tcl, iconlist.tcl, choosedir.tcl, msgbox.tcl, clrpick.tcl,
    # fontchooser.tcl and xmfbox.tcl.
    "_tk_data/ttk/aquatheme.tcl",
    "_tk_data/ttk/wintheme.tcl",
    "_tk_data/ttk/xptheme.tcl",
    "_tk_data/ttk/vistatheme.tcl",
    "_tk_data/safetk.tcl",
    "_tk_data/mkpsenc.tcl",
    "_tk_data/console.tcl",
    "_tk_data/tkappinit.c",
]

ZIP_MAGIC = b"PK\x03\x04"
PE32_PLUS = 0x20B
SECURITY_DIRECTORY = 4
"""The index of the certificate table in the data directories of the optional header."""


def dropped(name: str) -> bool:
    """Return True if the tool removes the archive member ``name``."""
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in DROP):
        return True
    if name.startswith("tcl_library/encoding/") and name.endswith(".enc"):
        return Path(name).stem not in KEEP_ENCODINGS
    if name.startswith("tk_library/msgs/"):
        return Path(name).name not in KEEP_TK_MSGS
    return False


def fix_index(name: str, body: bytes, gone: set[str]) -> bytes:
    """Remove the lines of an index file that point to a removed file.

    ``name`` is a ``tclIndex`` or a ``pkgIndex.tcl`` file. Without this step, Tcl finds a
    package in the index but cannot load it. With it, the lookup fails with a clear error.
    """
    folder = name.rsplit("/", 1)[0]
    index = Path(name).name
    lines = body.decode("utf-8").splitlines(keepends=True)
    keep = []
    for line in lines:
        hit = False
        for path in gone:
            if not path.startswith(folder + "/"):
                continue
            parts = path[len(folder) + 1 :].split("/")
            if index == "tclIndex" and len(parts) == 1:
                hit = f"$dir {parts[0]}]" in line
            elif index == "pkgIndex.tcl" and len(parts) == 2:
                hit = f"{{{parts[0]} {parts[1]}}}" in line
            if hit:
                break
        if not hit:
            keep.append(line)
    return "".join(keep).encode("utf-8")


def pe_layout(data: bytes, name: str) -> tuple[int, int]:
    """Return the end of the PE image and the offset of its checksum field.

    Stop the build if ``data`` is not a PE file, or if it has a code signature. A new
    archive makes a signature incorrect.
    """
    if data[:2] != b"MZ":
        raise SystemExit(f"trim_tcltk: {name} is not a PE file.")
    # struct.error tells that the file stops before the end of its headers.
    try:
        header = struct.unpack_from("<I", data, 0x3C)[0]
        if data[header : header + 4] != b"PE\0\0":
            raise SystemExit(f"trim_tcltk: {name} is not a PE file.")
        sections, optional_size = struct.unpack_from("<H12xH", data, header + 6)
        optional = header + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        directories = optional + (112 if magic == PE32_PLUS else 96)
        _address, size = struct.unpack_from("<II", data, directories + 8 * SECURITY_DIRECTORY)
        if size:
            raise SystemExit(f"trim_tcltk: {name} has a code signature. Do not change it.")
        table = optional + optional_size
        end = 0
        for i in range(sections):
            raw_size, raw_pointer = struct.unpack_from("<II", data, table + 40 * i + 16)
            end = max(end, raw_pointer + raw_size)
    except struct.error:
        raise SystemExit(f"trim_tcltk: {name} is not a PE file.") from None
    return end, optional + 64


def pe_checksum(data: bytes, offset: int) -> int:
    """Return the PE checksum of ``data``. The checksum field is at ``offset``.

    The checksum is the sum of the 16-bit words of the file, without the checksum field.
    The sum adds each carry back. At the end, it adds the length of the file.
    """
    padded = bytearray(data)
    padded[offset : offset + 4] = bytes(4)
    if len(padded) % 2:
        padded.append(0)
    words = array("H", padded)
    if sys.byteorder == "big":
        words.byteswap()
    total = sum(words)
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return total + len(data)


def trim(source: Path, target: Path) -> dict[str, int]:
    """Write ``source`` to ``target`` with a smaller archive. Return the numbers.

    Stop the build if the tool cannot read ``source``. Also stop it if no correct zip
    archive follows the PE image of ``source``.
    """
    try:
        data = source.read_bytes()
    except OSError as error:
        raise SystemExit(f"trim_tcltk: cannot read {source}: {error.strerror or error}.") from None
    end, checksum_offset = pe_layout(data, source.name)
    if data[end : end + 4] != ZIP_MAGIC:
        raise SystemExit(f"trim_tcltk: no zip archive follows the PE image of {source}.")
    try:
        old = zipfile.ZipFile(io.BytesIO(data[end:]))
    except zipfile.BadZipFile as error:
        raise SystemExit(f"trim_tcltk: the zip archive in {source} is damaged: {error}") from None
    removed = kept = 0
    archive = io.BytesIO()
    with old, zipfile.ZipFile(archive, "w") as new:
        gone = {info.filename for info in old.infolist() if dropped(info.filename)}
        for info in old.infolist():
            if info.filename in gone:
                removed += 1
                continue
            member = zipfile.ZipInfo(info.filename, info.date_time)
            member.external_attr = info.external_attr
            member.create_system = info.create_system
            body = old.read(info)
            if Path(info.filename).name in ("tclIndex", "pkgIndex.tcl") and gone:
                body = fix_index(info.filename, body, gone)
            new.writestr(member, body, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            kept += 1
    result = bytearray(data[:end] + archive.getvalue())
    struct.pack_into("<I", result, checksum_offset, pe_checksum(result, checksum_offset))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result)
    return {"before": len(data), "after": len(result), "kept": kept, "removed": removed}


def trim_binaries(binaries: Iterable[tuple[str, str, str]], folder: Path) -> list:
    """Return the PyInstaller binaries with trimmed copies of the Tcl and Tk DLLs.

    The copies go into ``folder``. Stop the build if the binaries do not have each DLL in
    ``DLLS`` one time.
    """
    result = []
    trimmed = []
    for dest, source, kind in binaries:
        name = Path(dest).name
        if name.lower() in DLLS:
            target = folder / name
            numbers = trim(Path(source), target)
            print(
                f"trim_tcltk: {name}: {numbers['before']:,} -> {numbers['after']:,} bytes, "
                f"{numbers['removed']} files removed, {numbers['kept']} files kept"
            )
            source = str(target)
            trimmed.append(name.lower())
        result.append((dest, source, kind))
    if sorted(trimmed) != sorted(DLLS):
        raise SystemExit(f"trim_tcltk: expected the DLLs {list(DLLS)}, found {trimmed}.")
    return result


def linux_dropped(dest: str) -> bool:
    """Return True if the Linux app does not need the data file ``dest`` of PyInstaller."""
    name = dest.replace("\\", "/").lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in DROP_LINUX):
        return True
    if name.startswith("_tcl_data/encoding/") and name.endswith(".enc"):
        return Path(name).stem not in KEEP_ENCODINGS_LINUX
    if name.startswith("_tk_data/msgs/"):
        return Path(name).name not in KEEP_TK_MSGS
    return False


def trim_datas(datas: Iterable[tuple[str, str, str]], folder: Path) -> tuple[list, int, int]:
    """Remove the Tcl and Tk files that the Linux app does not need from the data files.

    Return the new list, the number of removed files and their size in bytes. The index
    files tclIndex and pkgIndex.tcl lose the lines of the removed files. Their new copies
    go into ``folder``.
    """
    datas = [(dest.replace("\\", "/"), source, kind) for dest, source, kind in datas]
    gone = {dest for dest, _source, _kind in datas if linux_dropped(dest)}
    size = sum(Path(source).stat().st_size for dest, source, _kind in datas if dest in gone)
    result = []
    for dest, source, kind in datas:
        if dest in gone:
            continue
        if Path(dest).name in ("tclIndex", "pkgIndex.tcl"):
            body = Path(source).read_bytes()
            fixed = fix_index(dest, body, gone)
            if fixed != body:
                target = folder / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(fixed)
                source = str(target)
        result.append((dest, source, kind))
    return result, len(gone), size


def main(source: Path, target: Path) -> None:
    """Trim ``source`` into ``target`` from the command line, and show the numbers.

    Refuse a target that is the source. Thus a run by hand cannot change the DLL of a
    Python installation or of a virtual environment.
    """
    if target.resolve() == source.resolve():
        raise SystemExit("trim_tcltk: the target DLL must not be the source DLL.")
    print(trim(source, target))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python trim_tcltk.py <source DLL> <target DLL>")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
