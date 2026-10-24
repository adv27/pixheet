"""Read the package version for the release, and check a pull request that changes it.

The workflow release.yml publishes a release when a push to master changes the
version. A pull request that changes the version to a version with a tag would not make a
release. Thus ci.yml stops such a pull request. The script has two commands:

    python packaging/release_version.py version [PYPROJECT]
    python packaging/release_version.py check-pr BASE_PYPROJECT [PYPROJECT]

``version`` prints the version in PYPROJECT. ``check-pr`` compares the version in
BASE_PYPROJECT, the file of the base branch, with the version in PYPROJECT. If the
versions are different and the tag ``v<version>`` is on the remote ``origin``, the exit code
is 1. The default PYPROJECT is ``pyproject.toml``.

The script needs only the standard library and git. It must also run on Python 3.12,
because ci.yml runs it with the Python of the Ubuntu runner.
"""

import argparse
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

REMOTE = "origin"


def read_version(path: Path) -> str:
    """Return the version in the ``pyproject.toml`` file ``path``."""
    with path.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def remote_tag_exists(tag: str, remote: str = REMOTE) -> bool:
    """Return True if the tag ``tag`` is on ``remote``.

    ``git ls-remote --exit-code`` stops with the exit code 2 when no ref agrees with the
    pattern. A different exit code that is not 0 is an error, for example no network.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise RuntimeError(
        f"git ls-remote stopped with exit code {result.returncode}: {result.stderr.strip()}"
    )


def pr_problems(
    base: str, head: str, tag_exists: Callable[[str], bool] = remote_tag_exists
) -> list[str]:
    """Return the problems of a pull request that changes the version ``base`` to ``head``.

    A pull request that does not change the version has no problems.
    """
    if base == head:
        return []
    tag = f"v{head}"
    if not tag_exists(tag):
        return []
    return [
        f"The pull request changes the version from {base} to {head}, but the tag {tag} "
        "exists already. After the merge, the workflow does not publish a release. Use a "
        "new version."
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="print the version")
    version.add_argument("pyproject", nargs="?", type=Path, default=Path("pyproject.toml"))
    check = commands.add_parser("check-pr", help="check a pull request that changes the version")
    check.add_argument("base", type=Path, help="pyproject.toml of the base branch")
    check.add_argument("pyproject", nargs="?", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    head = read_version(args.pyproject)
    if args.command == "version":
        print(head)
        return 0
    base = read_version(args.base)
    problems = pr_problems(base, head, remote_tag_exists)
    for problem in problems:
        print(f"::error::{problem}")
    if base == head:
        print(f"release_version: the pull request does not change the version {head}.")
    elif not problems:
        print(f"release_version: the tag v{head} does not exist. The merge publishes it.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
