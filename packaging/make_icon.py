"""Draw the Pixheet icon and write it to packaging/pixheet.ico.

The icon is a small pixel-art picture on a grid of 8 x 8 cells, one color for each
cell, as in a sheet that Pixheet makes. The script draws each size on its own, so that
the cells have sharp edges at each size. Run it from the repository root:

    uv run python packaging/make_icon.py
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)
"""The sizes in the .ico file, in pixels. Each size is a multiple of the grid size."""
ICON = Path(__file__).with_name("pixheet.ico")

PALETTE = {
    "s": (79, 163, 224),  # sky
    "l": (138, 198, 240),  # light sky
    "y": (255, 201, 60),  # sun
    "g": (61, 163, 93),  # grass
    "d": (36, 115, 64),  # dark grass
}
PICTURE = (
    "llllllll",
    "lllllyyl",
    "sssssyys",
    "sssgssss",
    "ssgggsgs",
    "gggggggg",
    "dggdggdg",
    "dddddddd",
)
"""The picture, one letter of ``PALETTE`` for each cell, from the top row down."""
GRID = len(PICTURE)
LINE = (255, 255, 255)
"""The color of the lines between the cells."""


def draw(size: int) -> Image.Image:
    """Draw the icon with a side of ``size`` pixels.

    From 48 pixels, white lines separate the cells, as the grid lines of a sheet.
    Below 48 pixels, the lines are not visible, so the function does not draw them.
    """
    if size % GRID:
        raise ValueError(f"The size must be a multiple of {GRID}. The value is {size}.")
    cell = size // GRID
    line = 0 if size < 48 else max(1, size // 64)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    for row, letters in enumerate(PICTURE):
        for column, letter in enumerate(letters):
            x0, y0 = column * cell, row * cell
            canvas.rectangle((x0, y0, x0 + cell - 1, y0 + cell - 1), fill=(*PALETTE[letter], 255))
    for k in range(1, GRID):
        for offset in range(line):
            p = k * cell - line // 2 + offset
            canvas.line((p, 0, p, size - 1), fill=(*LINE, 255))
            canvas.line((0, p, size - 1, p), fill=(*LINE, 255))
    return image


def write(path: Path = ICON) -> Path:
    """Write the icon, with all sizes in ``SIZES``, to ``path``. Return ``path``."""
    images = [draw(size) for size in SIZES]
    largest = images[-1]
    largest.save(path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", type=Path, default=ICON, help="the .ico file")
    args = parser.parse_args()
    print(write(args.output))


if __name__ == "__main__":
    main()
