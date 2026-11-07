"""Make the synthetic sample images of the Rust spike.

    uv run python spike/make_samples.py DIR

The photos are smooth noise in several scales on a gradient. They have far more than
63,999 colors, so the fit search runs, as for a real photo. They are not real photos:
real JPEG noise and EXIF data can change the number of probes. Do not commit DIR.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def photo(w: int, h: int, seed: int) -> Image.Image:
    layers = []
    for channel in range(3):
        acc = Image.new("L", (w, h), 128)
        for scale, amount in ((64, 0.5), (16, 0.3), (4, 0.15), (1, 0.05)):
            size = (max(1, w // scale), max(1, h // scale))
            layer = Image.effect_noise(size, 80 + 10 * channel + seed)
            acc = Image.blend(acc, layer.resize((w, h), Image.Resampling.BICUBIC), amount)
        layers.append(acc)
    ramp = Image.linear_gradient("L")
    gradient = Image.merge(
        "RGB", (ramp.resize((w, h)), ramp.rotate(90).resize((w, h)), Image.new("L", (w, h), 90))
    )
    return Image.blend(Image.merge("RGB", layers), gradient, 0.45)


def flat(w: int, h: int) -> Image.Image:
    im = Image.new("RGB", (w, h), (240, 240, 240))
    draw = ImageDraw.Draw(im)
    for i in range(60):
        color = ((i * 37) % 256, (i * 91) % 256, (i * 53) % 256)
        draw.rectangle((i * 15, i * 10, i * 15 + 200, i * 10 + 120), fill=color)
    return im


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    out = parser.parse_args().folder
    out.mkdir(parents=True, exist_ok=True)
    photo(4032, 3024, 0).save(out / "photo-4032x3024.jpg", quality=90)
    photo(3000, 4000, 7).save(out / "photo-portrait-3000x4000.jpg", quality=90)
    photo(1920, 1080, 3).save(out / "photo-1920x1080.png")
    flat(1000, 750).save(out / "flat-1000x750.png")
    pixels = Image.effect_noise((64, 64), 100)
    pixels.convert("P", palette=Image.Palette.ADAPTIVE, colors=16).save(out / "pixel-64x64.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
