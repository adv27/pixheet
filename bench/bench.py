"""Benchmark pixheet on every image in one or more folders.

    uv run python bench/bench.py --samples DIR [--samples DIR ...] [-n 3] [--stages]

Folders come from --samples, else from PIXHEET_SAMPLES (several separated by
os.pathsep), else ./samples. Sample images stay local: never commit them.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

from pixheet import convert
from pixheet._image import fit, normalize, opened
from pixheet.cli import background_arg, max_side_arg
from pixheet.core import DEFAULT_BACKGROUND, DEFAULT_MAX_SIDE, _write


def find_images(folders: list[Path]) -> list[Path]:
    extensions = {ext.lower() for ext in Image.registered_extensions()}
    return sorted(
        p for folder in folders for p in folder.iterdir() if p.suffix.lower() in extensions
    )


def stages(path: Path, out: Path, max_side: int, background, compact: bool) -> dict[str, float]:
    """One conversion, timed per stage. It does the steps of pixheet.convert one by one."""
    times = {}
    t = time.perf_counter()
    with opened(path) as im:
        times["open"] = time.perf_counter() - t
        t = time.perf_counter()
        upright = normalize(im, background)
        times["normalize"] = time.perf_counter() - t
        t = time.perf_counter()
        fitted = fit(upright, max_side)
        times["fit"] = time.perf_counter() - t
        t = time.perf_counter()
        _write(fitted, out, compact)
        times["write"] = time.perf_counter() - t
    return times


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", type=Path, action="append", metavar="DIR")
    parser.add_argument("-n", "--repeat", type=int, default=3, help="runs per image (best of N)")
    parser.add_argument("--max-side", type=max_side_arg, default=DEFAULT_MAX_SIDE)
    parser.add_argument("--background", type=background_arg, default=DEFAULT_BACKGROUND)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--stages", action="store_true", help="also time each stage")
    args = parser.parse_args()

    folders = args.samples
    if not folders and os.environ.get("PIXHEET_SAMPLES"):
        folders = [Path(p) for p in os.environ["PIXHEET_SAMPLES"].split(os.pathsep) if p]
    if not folders:
        folders = [Path("samples")]
    missing = [str(f) for f in folders if not f.is_dir()]
    if missing:
        parser.error(f"no such folder: {', '.join(missing)} (use --samples or PIXHEET_SAMPLES)")
    images = find_images(folders)
    if not images:
        parser.error("no images found")

    background = args.background
    header = (
        f"{'image':<28} {'original':>11} {'sheet':>9} {'colors':>7} {'cells':>9} "
        f"{'best s':>7} {'cells/s':>10} {'xlsx MB':>8}"
    )
    if args.stages:
        header += f"  {'open':>6} {'norm':>6} {'fit':>6} {'write':>6}"
    print(header)
    total_seconds = total_cells = 0
    stage_totals: dict[str, float] = {}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.xlsx"
        for path in images:
            runs = [
                convert(
                    path, out, max_side=args.max_side, background=background, compact=args.compact
                )
                for _ in range(args.repeat)
            ]
            best = min(runs, key=lambda r: r.seconds)
            total_seconds += best.seconds
            total_cells += best.cells
            (ow, oh), (w, h) = best.original_size, best.size
            line = (
                f"{path.name[:28]:<28} {f'{ow}x{oh}':>11} {f'{w}x{h}':>9} {best.colors:>7,} "
                f"{best.cells:>9,} {best.seconds:>7.3f} {best.cells / best.seconds:>10,.0f} "
                f"{out.stat().st_size / 1e6:>8.2f}"
            )
            if args.stages:
                timed = [
                    stages(path, out, args.max_side, background, args.compact)
                    for _ in range(args.repeat)
                ]
                best_stages = {k: min(t[k] for t in timed) for k in timed[0]}
                for k, v in best_stages.items():
                    stage_totals[k] = stage_totals.get(k, 0.0) + v
                line += "  " + " ".join(f"{v:>6.3f}" for v in best_stages.values())
            print(line, flush=True)

    summary = (
        f"{'TOTAL':<28} {len(images):>11} images {'':>18} {total_cells:>9,} "
        f"{total_seconds:>7.3f} {total_cells / total_seconds:>10,.0f}"
    )
    if stage_totals:
        summary += " " * 10 + " ".join(f"{v:>6.3f}" for v in stage_totals.values())
    print(summary)
    print(
        f"Python {sys.version.split()[0]}, Pillow {Image.__version__}, "
        f"max_side={args.max_side}, compact={args.compact}, best of {args.repeat}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
