"""Print the fitted size and random pixel colors of an image as JSON.

Used by excel_check.ps1 to compare cells in Excel with the source image:

    uv run python scripts/sample_pixels.py photo.jpg --count 80

Rows and columns in the output are 1-based, like spreadsheet cells.
"""

import argparse
import json
import random
import sys

from pixheet import prepare
from pixheet.cli import background_arg, max_side_arg
from pixheet.core import DEFAULT_BACKGROUND, DEFAULT_MAX_SIDE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("--max-side", type=max_side_arg, default=DEFAULT_MAX_SIDE)
    parser.add_argument("--background", type=background_arg, default=DEFAULT_BACKGROUND)
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    im = prepare(args.image, max_side=args.max_side, background=args.background)
    w, h = im.size
    rng = random.Random(args.seed)
    spots = [(0, 0), (w - 1, h - 1)]
    spots += [(rng.randrange(w), rng.randrange(h)) for _ in range(max(0, args.count - 2))]
    samples = [[y + 1, x + 1, bytes(im.getpixel((x, y))).hex().upper()] for x, y in spots]
    json.dump({"width": w, "height": h, "samples": samples}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
