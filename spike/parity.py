"""Compare the Rust spike with the Python version, stage by stage.

    uv run python spike/parity.py --bin spike/rust-cli/target/release/pixheet-rs DIR

For each image in DIR, the script does these checks:

1. writer: the Rust writer gets the image that prepare() gives. Then sheet1.xml and
   styles.xml must be byte-identical to the output of the Python writer.
2. resize: the Rust fit gets the image that Pillow decodes and normalizes, as a PNG.
   Then the fitted image must have the same size and the same pixels.
3. pipeline: the Rust command gets the original file. The script reports the size, the
   colors, and the pixel difference. A JPEG decoder that is not libjpeg-turbo can change
   some pixels by a small value.
4. openpyxl: openpyxl must open each Rust output.

It also adds cases for the modes and for EXIF rotation.
"""

import argparse
import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import openpyxl
from PIL import ExifTags, Image, ImageChops

from pixheet import prepare
from pixheet._image import normalize, opened
from pixheet._xlsx import write_xlsx

PARTS = ("xl/worksheets/sheet1.xml", "xl/styles.xml")


def run(binary: Path, *args: str) -> None:
    subprocess.run([str(binary), "-q", *args], check=True)


def python_parts(im: Image.Image, compact: bool) -> dict[str, bytes]:
    buf = io.BytesIO()
    write_xlsx(im, buf, compact=compact)
    with zipfile.ZipFile(buf) as zf:
        return {p: zf.read(p) for p in PARTS}


def rust_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        return {p: zf.read(p) for p in PARTS}


def diff(a: Image.Image, b: Image.Image) -> str:
    if a.size != b.size:
        return f"size {a.size} != {b.size}"
    d = ImageChops.difference(a, b)
    hist = d.convert("L").histogram()
    changed = a.width * a.height - hist[0]
    worst = max(hi for _, hi in d.getextrema())
    return "identical" if changed == 0 else f"{changed} pixels differ, max {worst}"


def colors(im: Image.Image) -> int:
    return len(im.getcolors(im.width * im.height))


def extra_cases(folder: Path) -> list[Path]:
    """Images for the modes and the EXIF rotation. They are synthetic, as in tests/."""
    base = Image.effect_noise((300, 200), 60).convert("RGB")
    grad = Image.linear_gradient("L").resize((300, 200))
    rgba = base.copy()
    rgba.putalpha(grad)
    cases = {
        "rgba.png": rgba,
        "gray.png": grad,
        "palette.png": base.convert("P", palette=Image.Palette.ADAPTIVE, colors=40),
        "la.png": Image.merge("LA", (grad, grad.rotate(180))),
    }
    paths = []
    for name, im in cases.items():
        im.save(folder / name)
        paths.append(folder / name)
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    base.save(folder / "exif6.jpg", exif=exif, quality=92)
    paths.append(folder / "exif6.jpg")
    return paths


def check(binary: Path, src: Path, tmp: Path, max_side: int) -> list[str]:
    report = []
    out = tmp / "out.xlsx"
    # 1. writer
    expected = prepare(src, max_side=max_side)
    fitted_png = tmp / "fitted.png"
    expected.save(fitted_png)
    for compact in (False, True):
        run(
            binary, str(fitted_png), "-o", str(out), "--no-fit", *(["--compact"] if compact else [])
        )
        same = rust_parts(out) == python_parts(expected, compact)
        report.append(
            f"writer{' compact' if compact else ''}: {'identical' if same else 'DIFFERENT'}"
        )
    openpyxl.load_workbook(out, read_only=True).close()
    # 2. resize: start from the pixels that Pillow decodes
    with opened(src) as im:
        normal_png = tmp / "normal.png"
        normalize(im, (255, 255, 255)).save(normal_png)
    rust_fit = tmp / "rust-fit.png"
    run(
        binary,
        str(normal_png),
        "-o",
        str(out),
        "--max-side",
        str(max_side),
        "--dump-fitted",
        str(rust_fit),
    )
    with Image.open(rust_fit) as r:
        report.append(f"resize: {diff(expected, r.convert('RGB'))}")
    # 3. pipeline
    run(
        binary,
        str(src),
        "-o",
        str(out),
        "--max-side",
        str(max_side),
        "--dump-fitted",
        str(rust_fit),
    )
    with Image.open(rust_fit) as r:
        r = r.convert("RGB")
        report.append(
            f"pipeline: python {expected.size} {colors(expected)} colors, "
            f"rust {r.size} {colors(r)} colors, {diff(expected, r)}"
        )
    # 4. openpyxl
    wb = openpyxl.load_workbook(out, read_only=True)
    report.append(f"openpyxl: {wb.active.max_column}x{wb.active.max_row}")
    wb.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--max-side", type=int, action="append")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        images = sorted(p for p in args.folder.iterdir() if p.suffix in {".png", ".jpg"})
        images += extra_cases(tmp)
        for max_side in args.max_side or [1000]:
            for src in images:
                lines = check(args.bin, src, tmp, max_side)
                print(f"{src.name} (max_side={max_side})")
                for line in lines:
                    print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
