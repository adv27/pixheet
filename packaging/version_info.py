"""Read the data for the Windows version resource of Pixheet.exe from pyproject.toml.

The spec file uses this module at build time, so the exe always shows the version of
the package. The module needs only the standard library. The tests import it.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """Return the first four numbers of ``version``, with zeros at the end if necessary.

    Windows keeps a file version as four numbers from 0 to 65535. The function ignores a
    pre-release, post-release, or local part. For example, "1.2.0rc1" gives (1, 2, 0, 0).
    """
    match = re.match(r"\d+(?:\.\d+)*", version.strip().removeprefix("v"))
    if match is None:
        raise ValueError(f"The version must start with a number. The value is {version!r}.")
    numbers = [int(part) for part in match.group().split(".")][:4]
    if any(n > 0xFFFF for n in numbers):
        raise ValueError(f"Each number of the version must be 65535 or less: {version!r}.")
    numbers += [0] * (4 - len(numbers))
    return (numbers[0], numbers[1], numbers[2], numbers[3])


@dataclass(frozen=True, slots=True)
class Metadata:
    """The package data that the exe shows in its properties."""

    name: str
    version: str
    description: str
    author: str
    license: str
    copyright: str

    @property
    def numbers(self) -> tuple[int, int, int, int]:
        return numeric_version(self.version)

    def strings(self, exe_name: str) -> dict[str, str]:
        """Return the strings of the version resource."""
        return {
            "CompanyName": self.author,
            "FileDescription": self.name,
            "FileVersion": self.version,
            "InternalName": self.name,
            "LegalCopyright": self.copyright,
            "OriginalFilename": exe_name,
            "ProductName": self.name,
            "ProductVersion": self.version,
            "Comments": (
                f"{self.description} License: {self.license}. The third-party parts have "
                "their own licenses: read THIRD-PARTY-NOTICES.txt."
            ),
        }


def read_metadata(root: Path = ROOT) -> Metadata:
    """Read the metadata from ``pyproject.toml`` and the copyright line from ``LICENSE``."""
    with (root / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    author = project["authors"][0]["name"]
    copyright_line = next(
        (
            line.strip()
            for line in (root / "LICENSE").read_text(encoding="utf-8").splitlines()
            if line.strip().lower().startswith("copyright")
        ),
        f"Copyright (c) {author}",
    )
    return Metadata(
        name=project["name"].capitalize(),
        version=project["version"],
        description=project["description"],
        author=author,
        license=project["license"],
        copyright=copyright_line,
    )
