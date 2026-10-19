"""Command-line tool: ``pixheet INPUT [INPUT ...] [-o OUTPUT] ...``."""

import argparse
import glob
import os
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pixheet._image import parse_background
from pixheet.core import ConvertResult, convert


def _max_side(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _background(text: str) -> str:
    try:
        parse_background(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return text


def _version() -> str:
    try:
        return version("pixheet")
    except PackageNotFoundError:  # running from a source tree that is not installed
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixheet",
        description=(
            "Turn images into .xlsx spreadsheets where every cell is one pixel. "
            "Open the file in a spreadsheet app and zoom out to see the picture."
        ),
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT", help="image file(s) to convert")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="OUTPUT",
        help="output .xlsx path (single INPUT only; default: INPUT with an .xlsx suffix)",
    )
    parser.add_argument(
        "--max-side",
        type=_max_side,
        default=1000,
        metavar="N",
        help="cap on the longest side in pixels; 0 = only the sheet limits apply (default: 1000)",
    )
    parser.add_argument(
        "--background",
        type=_background,
        default="white",
        metavar="COLOR",
        help="color for transparent areas: white, black, #RRGGBB, or none to drop alpha "
        "(default: white)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="leave out cell references: smaller and faster, verified in Excel and openpyxl only",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def _expand(inputs: Sequence[str]) -> list[str]:
    """Expand wildcards the shell left alone (cmd and PowerShell don't expand them).

    Only the file-name part is a pattern: brackets in folder names are literal, as in
    "shots [2024]/*.jpg". An existing file such as "img[1].png" is taken as is.
    """
    paths = []
    for arg in inputs:
        head, tail = os.path.split(arg)
        if any(c in tail for c in "*?[") and not Path(arg).exists():
            pattern = str(Path(head.replace("[", "[[]")) / tail)
            # Path.glob() does not take absolute patterns such as C:\photos\*.jpg.
            paths.extend(sorted(glob.glob(pattern)) or [arg])  # noqa: PTH207
        else:
            paths.append(arg)
    return paths


def _summary(name: str, result: ConvertResult) -> str:
    (ow, oh), (w, h) = result.original_size, result.size
    return (
        f"{name}  {ow}x{oh} -> {w}x{h}  {result.colors:,} colors  "
        f"{result.cells:,} cells  {result.seconds:.2f}s"
    )


def main(argv: Sequence[str] | None = None) -> int:
    # A file name the console code page cannot show must not stop the batch.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = _build_parser()
    args = parser.parse_args(argv)
    inputs = _expand(args.inputs)
    if args.output is not None and len(inputs) > 1:
        parser.error("-o/--output can only be used with a single INPUT")

    failed = 0
    written: dict[str, str] = {}  # normalized output path -> the input that wrote it
    for name in inputs:
        try:
            src = Path(name)
            dst = args.output or src.with_suffix(".xlsx")
            key = os.path.normcase(Path(dst).resolve())
            if key in written:
                raise ValueError(f"{dst} was already written from {written[key]}")
            result = convert(
                src,
                dst,
                max_side=args.max_side,
                background=args.background,
                compact=args.compact,
            )
        except Exception as e:
            failed += 1
            print(f"pixheet: {name}: {e}", file=sys.stderr)
            continue
        written[key] = name
        if not args.quiet:
            print(_summary(name, result), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
