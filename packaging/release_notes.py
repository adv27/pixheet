"""Print the release notes of a version: its section of CHANGELOG.md, then the download steps.

The release job of release.yml gives the output to ``gh release create --notes-file``,
together with ``--generate-notes``. GitHub then adds the list of the pull requests. The
version job of ci.yml runs the script for each pull request, so a version without a
section in CHANGELOG.md cannot get to master. Usage:

    python packaging/release_notes.py VERSION [CHANGELOG]

The default CHANGELOG is ``CHANGELOG.md``. A section starts at a line ``## VERSION`` or
``## [VERSION]``, and stops at the next line that starts with ``## ``. When the file has
no section for VERSION, or the section is empty, the exit code is 1.

The script needs only the standard library. It must also run on Python 3.12, because the
release job runs it with the Python of the Ubuntu runner.
"""

import argparse
import re
import sys
from pathlib import Path

REPOSITORY = "https://github.com/adv27/pixheet"

DOWNLOADS = """\
## Downloads

- `Pixheet.exe` is the desktop app for Windows. It does not need Python.
- `Pixheet-linux-x86_64` is the desktop app for x86_64 Linux. It needs glibc 2.27 or later
  and an X server.
- Each app has a file of third-party notices, for example
  `THIRD-PARTY-NOTICES-windows-x64.txt`. It gives the licenses of the parts in the app.
- Each app has a `.sha256` file. Use it to compare the SHA-256 hash of the download:
  - PowerShell: `(Get-FileHash Pixheet.exe -Algorithm SHA256).Hash.ToLower()`
  - Linux: `sha256sum -c Pixheet-linux-x86_64.sha256`
- The exe has no code signature yet. Thus Windows SmartScreen can show "Windows protected
  your PC". To start the app, click **More info**, then **Run anyway**.
- A download does not keep the permission to run the Linux file. To add it, run
  `chmod +x Pixheet-linux-x86_64`.
- The [README]({repository}/blob/v{version}/README.md) of this version tells more.
"""


LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+\.)\s")
"""The start of an item of a list."""


class NotesError(Exception):
    """CHANGELOG.md has no section for the version, or the section is empty."""


def changelog_section(text: str, version: str) -> str:
    """Return the section of ``version`` in the changelog ``text``, without its heading.

    Raise ``NotesError`` when the text has no section for ``version``, or when the
    section has no text.
    """
    if not re.fullmatch(r"\S+", version):
        raise NotesError(f"The version {version!r} is not correct.")
    heading = re.compile(rf"## (?:{re.escape(version)}|\[{re.escape(version)}\])(?:\s.*)?")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.fullmatch(line.rstrip())), None)
    if start is None:
        raise NotesError(f"CHANGELOG.md has no section '## {version}'. Add the section.")
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line.rstrip())
    section = "\n".join(body).strip("\n")
    if not section.strip():
        raise NotesError(f"The section '## {version}' of CHANGELOG.md is empty. Add the changes.")
    return section + "\n"


def unwrap(text: str) -> str:
    """Join the wrapped lines of each paragraph and of each list item into one line.

    GitHub can show each line end in the release notes as a line break. The changelog
    wraps its lines, so the notes get one line for each paragraph. The text in a code
    block does not change.
    """
    out: list[str] = []
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        starts_block = not line.strip() or line.startswith("#") or LIST_ITEM.match(line)
        previous = out[-1] if out else ""
        if fence or starts_block or not previous.strip() or previous.startswith(("#", "```")):
            out.append(line)
        else:
            out[-1] = f"{previous} {line.strip()}"
    return "\n".join(out) + "\n"


def release_notes(text: str, version: str) -> str:
    """Return the release notes of ``version``: its changelog section, then the downloads."""
    downloads = DOWNLOADS.format(repository=REPOSITORY, version=version)
    return unwrap(f"{changelog_section(text, version)}\n{downloads}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the version, for example 0.1.0")
    parser.add_argument("changelog", nargs="?", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args(argv)
    text = args.changelog.read_text(encoding="utf-8")
    try:
        notes = release_notes(text, args.version)
    except NotesError as e:
        print(f"::error::{e}")
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
