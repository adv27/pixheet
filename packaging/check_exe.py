"""Test a built Pixheet.exe: it must convert an image, and each cell must have its color.

The script makes a test image in a temporary folder and runs the exe with ``--smoke``
and the image path. The exe converts the image and closes. Then the script reads the
.xlsx file with openpyxl, compares each cell with its pixel, and deletes the folder.
Run it from the repository root:

    uv run python packaging/check_exe.py dist/Pixheet.exe
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import openpyxl
from PIL import Image

WIDTH, HEIGHT = 48, 32
TIMEOUT = 120
"""The maximum time for the exe, in seconds. The first start of a one-file exe is slow."""


def make_image(path: Path) -> Image.Image:
    """Save a test image with transparent pixels and a few colors. Return its RGB pixels."""
    image = Image.new("RGBA", (WIDTH, HEIGHT))
    for y in range(HEIGHT):
        for x in range(WIDTH):
            alpha = 0 if (x + y) % 7 == 0 else 255
            image.putpixel((x, y), (x * 5, y * 7, (x * y) % 256, alpha))
    image.save(path)
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, image).convert("RGB")


def check(exe: Path) -> list[str]:
    """Run ``exe`` on a test image. Return the problems, or an empty list."""
    with tempfile.TemporaryDirectory(prefix="pixheet-check-") as folder:
        src = Path(folder) / "test image.png"
        expected = make_image(src)
        start = time.monotonic()
        result = subprocess.run(
            [str(exe), "--smoke", str(src)], timeout=TIMEOUT, capture_output=True, check=False
        )
        seconds = time.monotonic() - start
        if result.returncode != 0:
            return [f"The exe stopped with exit code {result.returncode}."]
        out = src.with_suffix(".xlsx")
        if not out.is_file():
            return [f"The exe did not write {out.name}."]
        print(f"The exe converted the test image in {seconds:.1f} s.")
        workbook = openpyxl.load_workbook(out)
        try:
            sheet = workbook.active
            size = (sheet.max_column, sheet.max_row)
            if size != (WIDTH, HEIGHT):
                return [f"The sheet has {size[0]} columns and {size[1]} rows."]
            problems = []
            for y in range(HEIGHT):
                for x in range(WIDTH):
                    rgb = sheet.cell(row=y + 1, column=x + 1).fill.fgColor.rgb
                    want = "FF" + bytes(expected.getpixel((x, y))).hex().upper()
                    if rgb != want:
                        problems.append(f"The cell ({x}, {y}) has {rgb}, not {want}.")
            return problems[:10]
        finally:
            workbook.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exe", type=Path, help="the path of Pixheet.exe")
    args = parser.parse_args()
    if not args.exe.is_file():
        print(f"check_exe: {args.exe} does not exist.", file=sys.stderr)
        return 1
    problems = check(args.exe.resolve())
    for problem in problems:
        print(f"check_exe: {problem}", file=sys.stderr)
    if not problems:
        print(f"check_exe: {args.exe} converts images correctly.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
