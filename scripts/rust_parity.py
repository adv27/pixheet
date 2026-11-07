"""Compare the Rust command with the Python command on synthetic images.

    uv run python scripts/rust_parity.py --bin target/release/pixheet [--samples DIR]

For each image, both commands write an .xlsx file, normal and compact. Then sheet1.xml
and styles.xml must be byte-identical. The XML holds each pixel, so identical XML also
means identical pixels and the same sheet size. The compressed bytes can differ.

The images are synthetic: each mode that Pillow reads, each EXIF orientation, and each
file format of the desktop app that the Rust decoders read. --samples adds the images
of a folder, for example the output of spike/make_samples.py.

The exit status is 1 when one image gives different XML or a different result.
"""

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import ExifTags, Image

PARTS = ("xl/worksheets/sheet1.xml", "xl/styles.xml")


def gradient(w: int, h: int) -> Image.Image:
    ramp = Image.linear_gradient("L")
    noise = Image.effect_noise((w, h), 40)
    return Image.merge("RGB", (ramp.resize((w, h)), ramp.rotate(90).resize((w, h)), noise))


def make_cases(folder: Path) -> list[Path]:
    """Write the synthetic images. Return their paths."""
    base = gradient(64, 48)
    alpha = Image.linear_gradient("L").resize(base.size)
    rgba = base.copy()
    rgba.putalpha(alpha)
    gray16 = Image.new("I;16", (40, 30))
    gray16.putdata([(x * 1601 + y * 977) % 65536 for y in range(30) for x in range(40)])
    palette = base.convert("P", palette=Image.Palette.ADAPTIVE, colors=50)
    palette_t = palette.copy()
    palette_t.info["transparency"] = 3
    rgb_key = Image.new("RGB", (8, 8), (1, 2, 3))
    rgb_key.paste((200, 100, 50), (2, 2, 6, 6))
    cases: dict[str, tuple[Image.Image, dict]] = {
        "rgb.png": (base, {}),
        "rgba.png": (rgba, {}),
        "l.png": (base.convert("L"), {}),
        "la.png": (Image.merge("LA", (base.convert("L"), alpha)), {}),
        "1.png": (base.convert("1"), {}),
        "p.png": (palette, {}),
        "p-transparent.png": (palette_t, {"transparency": 3}),
        "rgb-key.png": (rgb_key, {"transparency": (1, 2, 3)}),
        "i16.png": (gray16, {}),
        "rgb.jpg": (base, {"quality": 90}),
        "gray.jpg": (base.convert("L"), {"quality": 90}),
        "cmyk.jpg": (base.convert("CMYK"), {"quality": 90}),
        "rgb.gif": (palette, {}),
        "rgb.bmp": (base, {}),
        "rgb.tif": (base, {}),
        "rgba.tif": (rgba, {}),
        "rgb.webp": (base, {"lossless": True}),
        "rgba.webp": (rgba, {"lossless": True}),
        "rgb.ppm": (base, {}),
        "l.pgm": (base.convert("L"), {}),
        "rgb.tga": (base, {}),
        "rgba.ico": (rgba.resize((32, 32)), {"sizes": [(32, 32)]}),
        "wide.png": (gradient(1500, 20), {}),
    }
    paths = []
    for name, (im, params) in cases.items():
        im.save(folder / name, **params)
        paths.append(folder / name)
    for orientation in range(1, 9):
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = orientation
        for suffix, params in ((".jpg", {"quality": 92}), (".png", {})):
            path = folder / f"exif{orientation}{suffix}"
            gradient(30, 20).save(path, exif=exif, **params)
            paths.append(path)
    return paths


def parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        if zf.testzip() is not None:
            raise ValueError(f"{path} has a bad member")
        return {p: zf.read(p) for p in PARTS}


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def result_fields(line: str) -> str:
    """The report line without the name and the time."""
    return line.split("  ", 1)[-1].rsplit("  ", 1)[0]


def check(binary: Path, src: Path, tmp: Path, args: list[str]) -> str | None:
    """Return None when both commands agree, else the difference."""
    py_out, rs_out = tmp / "py.xlsx", tmp / "rs.xlsx"
    for out in (py_out, rs_out):
        out.unlink(missing_ok=True)
    py = run([sys.executable, "-m", "pixheet", str(src), "-o", str(py_out), *args])
    rs = run([str(binary), str(src), "-o", str(rs_out), *args])
    if py.returncode != rs.returncode:
        return f"exit {py.returncode} != {rs.returncode}: {py.stderr.strip()} | {rs.stderr.strip()}"
    if py.returncode != 0:
        return None
    if result_fields(py.stdout.strip()) != result_fields(rs.stdout.strip()):
        return f"{py.stdout.strip()} != {rs.stdout.strip()}"
    a, b = parts(py_out), parts(rs_out)
    different = [name for name in PARTS if a[name] != b[name]]
    return f"different {', '.join(different)}" if different else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bin", type=Path, required=True, help="the Rust pixheet binary")
    parser.add_argument("--samples", type=Path, action="append", default=[], metavar="DIR")
    parser.add_argument("--max-side", action="append", default=[], metavar="N")
    args = parser.parse_args()
    failures = 0
    checks = 0
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cases = tmp / "cases"
        cases.mkdir()
        images = make_cases(cases)
        for folder in args.samples:
            images += sorted(p for p in folder.iterdir() if p.is_file())
        for max_side in args.max_side or ["1000"]:
            for compact in (False, True):
                flags = ["--max-side", max_side, *(["--compact"] if compact else [])]
                for src in images:
                    checks += 1
                    problem = check(args.bin, src, tmp, flags)
                    if problem:
                        failures += 1
                        print(f"FAIL {src.name} {' '.join(flags)}: {problem}", flush=True)
    print(f"{checks - failures} of {checks} checks give the same output")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
