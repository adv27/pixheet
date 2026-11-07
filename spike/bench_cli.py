"""Time the Python command and the Rust spike, one process for each conversion.

    uv run python spike/bench_cli.py --bin NAME=PATH [--bin NAME=PATH ...] DIR [-n 5]

The time is the wall-clock time of the whole process, the best of N runs. Thus it
includes the start of the interpreter or of the binary. The last row is one process
that converts all images of DIR.
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def best(cmd: list[str], n: int) -> float:
    times = []
    for _ in range(n):
        t = time.perf_counter()
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        times.append(time.perf_counter() - t)
    return min(times)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--bin", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--max-side", default="1000")
    args = parser.parse_args()

    py = Path(sys.executable)
    commands = {
        "python 1 thread": [str(py), "-X", "cpu_count=1", "-m", "pixheet"],
        "python": [str(py), "-m", "pixheet"],
    }
    for item in args.bin:
        name, _, path = item.partition("=")
        commands[f"{name} 1 thread"] = [path, "-j", "1"]
        commands[name] = [path]

    images = sorted(p for p in args.folder.iterdir() if p.suffix in {".png", ".jpg"})
    names = list(commands)
    print(f"{'image':<30}" + "".join(f"{n:>18}" for n in names))
    with tempfile.TemporaryDirectory() as t:
        out = Path(t)
        rows = [(p.name, [p], ["-o", str(out / "out.xlsx")]) for p in images]
        # The batch: one process for all images. The outputs go next to copies.
        copies = []
        for p in images:
            copy = out / p.name
            copy.write_bytes(p.read_bytes())
            copies.append(copy)
        rows.append((f"all {len(images)} in one process", copies, []))
        for label, inputs, extra in rows:
            cells = []
            for name in names:
                cmd = [
                    *commands[name],
                    "-q",
                    "--max-side",
                    args.max_side,
                    *map(str, inputs),
                    *extra,
                ]
                cells.append(best(cmd, args.n))
            base = cells[names.index("python")]
            print(
                f"{label[:30]:<30}" + "".join(f"{f'{c:.3f}s {base / c:4.1f}x':>18}" for c in cells),
                flush=True,
            )
    print(f"best of {args.n}; Nx = the time of 'python' (all CPUs) / the time of the column")
    return 0


if __name__ == "__main__":
    sys.exit(main())
