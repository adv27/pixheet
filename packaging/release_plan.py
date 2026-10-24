"""Decide what the release job does for a push to master or for a re-run of the job.

The workflow release.yml asks GitHub for the state of the tag and of the release. This
script decides the next step from that state. It does not call GitHub. The script has two
commands:

    python packaging/release_plan.py tag SHA [TAG_COMMIT]
    python packaging/release_plan.py upload RELEASE FILE... < ASSETS

``tag`` prints "create", "publish" or "skip":

- "create": the tag does not exist. The job makes the tag at SHA, then publishes.
- "publish": the tag is at SHA. An earlier run made it, but the run can have stopped
  before the release was complete. The job publishes the files that are missing.
- "skip": the tag is at a different commit. That version has a release from a different
  commit. The job does nothing.

``upload`` reads the names of the files of the release from stdin, one name on each line.
RELEASE is "none", "draft" or "published": the state of the release. FILE is a file that
the release must have. The first line of the output is "create", "upload", "publish" or
"done". Each next line is a file to upload. "publish" is for a draft: the job uploads the
files, then publishes the draft. The job never replaces a file of a release, because
users can have downloaded it already. If the release has a file but not its checksum
file, or the checksum file but not the file, the exit code is 1. A new build can give a
different file, and then the checksum does not agree with the file.

``gh release create`` makes a draft, uploads the files, and then publishes the draft. A
job that stops during the upload, for example after a cancel or at the time limit, leaves
the draft. Users cannot see a draft. Thus a new run of the job must publish it.

The script needs only the standard library. It must also run on Python 3.12, because the
release job runs it with the Python of the Ubuntu runner.
"""

import argparse
import sys
from collections.abc import Iterable, Sequence

CHECKSUM_SUFFIX = ".sha256"
RELEASE_STATES = ("none", "draft", "published")
"""The states of a release: it does not exist, it is a draft, or users can see it."""


class PlanError(Exception):
    """The release is in a state that the job cannot repair. A person must repair it."""


def tag_action(tag_commit: str | None, sha: str) -> str:
    """Return "create", "publish" or "skip" for the tag at ``tag_commit``.

    ``tag_commit`` is the commit of the tag, or None (or "") when the tag does not exist.
    ``sha`` is the commit of the push.
    """
    if not tag_commit:
        return "create"
    if tag_commit == sha:
        return "publish"
    return "skip"


def partner(name: str) -> str:
    """Return the name of the other file of the pair: the file or its checksum file."""
    if name.endswith(CHECKSUM_SUFFIX):
        return name.removesuffix(CHECKSUM_SUFFIX)
    return name + CHECKSUM_SUFFIX


def upload_plan(
    release: str, existing: Iterable[str], wanted: Sequence[str]
) -> tuple[str, list[str]]:
    """Return the action for the release, and the files to upload, in the order of ``wanted``.

    ``release`` is a value of :data:`RELEASE_STATES`. The action is "create" when the
    release does not exist, and "publish" when it is a draft. For a published release, the
    action is "upload" when the release does not have all files of ``wanted``, and "done"
    when it has them all. ``existing`` is the list of the files of the release. A file
    that is not in ``wanted`` has no effect. Raise ``PlanError`` when the release has only
    one file of a pair in ``wanted``. Raise ``ValueError`` for an unknown ``release``.
    """
    if release not in RELEASE_STATES:
        raise ValueError(f"unknown state of the release: {release!r}")
    if release == "none":
        return "create", list(wanted)
    have = set(existing)
    missing = [name for name in wanted if name not in have]
    for name in missing:
        other = partner(name)
        if other in wanted and other in have:
            raise PlanError(
                f"The release has {other}, but not {name}. A new build can be different, "
                f"and then the checksum is not correct. Delete {other} in the release by "
                "hand, then run the job again."
            )
    if release == "draft":
        return "publish", missing
    if not missing:
        return "done", []
    return "upload", missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    tag = commands.add_parser("tag", help="decide what to do with the tag")
    tag.add_argument("sha", help="the commit of the push")
    tag.add_argument("tag_commit", nargs="?", default="", help="the commit of the tag")
    upload = commands.add_parser("upload", help="decide which files to publish")
    upload.add_argument("release", choices=RELEASE_STATES, help="the state of the release")
    upload.add_argument("files", nargs="+", help="the files that the release must have")
    args = parser.parse_args(argv)

    if args.command == "tag":
        print(tag_action(args.tag_commit, args.sha))
        return 0
    existing = [line.strip() for line in sys.stdin if line.strip()]
    try:
        action, files = upload_plan(args.release, existing, args.files)
    except PlanError as e:
        print(f"::error::{e}")
        return 1
    print(action)
    for name in files:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
