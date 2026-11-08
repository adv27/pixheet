# Spike: the CLI in Rust

> **History.** This file records the port of Pixheet from Python to Rust in November 2026.
> The Python version and its scripts are gone now; the branch `python-legacy` keeps them.
> Today, `cargo xtask samples DIR` makes the sample images, `cargo xtask bench DIR` times
> the command, and the golden test of `pixheet-core` keeps the parity with the Python
> version. Read [AGENTS.md](../../AGENTS.md).

This folder keeps the results of a spike: a first Rust version of the `pixheet`
command. The spike answered two questions:

1. How much faster is the command in Rust?
2. Can the Rust version give the same output as the Python version?

The code of the spike is now in `crates/pixheet-core` and `crates/pixheet-cli`. Read
[PLAN.md](PLAN.md) for the plan of the full port.

## Contents

| Path | Content |
|---|---|
| `README.md` | The results of the spike: the speed and the parity. |
| `PLAN.md` | The plan of the port, the decisions, and the status of each phase. |
| `PROFILE.md` | The profile of the Rust command and the changes after it. |

## Results

The data comes from one Linux VM: 4 vCPUs (Intel Xeon, 2.1 GHz), Python 3.15.2,
Pillow 13.0.0, and rustc 1.97.0. The samples are synthetic: `make_samples.py` makes
them. Nobody has measured real photos or Windows yet.

### Parity

`parity.py` checks 10 images at `--max-side 1000` and at `--max-side 0`. The 10 images
are the 5 samples and 5 extra cases: RGBA, gray, palette, LA, and a JPEG with EXIF
orientation 6.

| Check | Result |
|---|---|
| `sheet1.xml` and `styles.xml`, normal and compact, from the same fitted image | Byte-identical in all 40 checks. |
| The fitted image, from the same decoded pixels | Byte-identical in all 20 checks. This includes the reduce step of `--max-side 0`. |
| The full command, JPEG with libjpeg-turbo (`turbo`) | Byte-identical in all 20 checks. |
| The full command, JPEG with zune-jpeg (default) | The PNG files are identical. The JPEG files differ by 1 or 2 levels in some pixels. One JPEG got a different sheet size: 223x298, not 223x297. |
| openpyxl | Opens each Rust output. |

The compressed bytes are different, as with the threads of the Python version. The
Rust files are a little larger, because zlib-rs at level 6 compresses repeated text
less than zlib does:

| Image | Python | Rust |
|---|---:|---:|
| flat-1000x750.png | 1,709,628 bytes | 1,908,406 bytes (+11.6 %) |
| photo-4032x3024.jpg | 718,173 bytes | 722,753 bytes (+0.6 %) |

### End to end

`bench_cli.py` starts one process for each conversion. The time is the best of 7
runs, so it includes the start of Python or of the binary. "Nx" is the time of
`python` divided by the time of the column. Thus 2.0x is two times faster than Python
with all CPUs.

| Image | python, 1 thread | python | rust-turbo, 1 thread | rust-turbo | rust-zune |
|---|---:|---:|---:|---:|---:|
| flat-1000x750.png | 0.380 s | 0.283 s | 0.204 s (1.4x) | 0.073 s (3.9x) | 0.081 s (3.5x) |
| photo-1920x1080.png | 0.421 s | 0.361 s | 0.151 s (2.4x) | 0.079 s (4.6x) | 0.077 s (4.7x) |
| photo-4032x3024.jpg | 0.565 s | 0.461 s | 0.343 s (1.3x) | 0.190 s (2.4x) | 0.178 s (2.6x) |
| photo-portrait-3000x4000.jpg | 0.536 s | 0.455 s | 0.305 s (1.5x) | 0.178 s (2.6x) | 0.152 s (3.0x) |
| pixel-64x64.png | 0.075 s | 0.073 s | 0.005 s (15.4x) | 0.005 s (15.5x) | 0.004 s (17.3x) |
| All 5 in one process | 1.654 s | 1.275 s | 0.990 s (1.3x) | 0.523 s (2.4x) | 0.442 s (2.9x) |

The start of the Python command takes about 70 ms. The start of the Rust binary takes
less than 5 ms. The binary is 3.4 MB (3.8 MB with `turbo`). On Linux, it needs only
`libc`, `libm` and `libgcc_s`. libjpeg-turbo is in the binary.

### Stages

The table gives the time of each stage in `pixheet-rs --stages`, with `turbo`, in ms.
"write" includes "sheet" (the text of the sheet and its DEFLATE) and "styles and rest".

| Image | Threads | open | fit (probes) | write | sheet | styles and rest |
|---|---:|---:|---:|---:|---:|---:|
| flat-1000x750.png | 4 | 2 | 1 (0) | 62 | 57 | 0.4 |
| flat-1000x750.png | 1 | 2 | 1 (0) | 182 | 176 | 0.4 |
| photo-4032x3024.jpg | 4 | 81 | 68 (6) | 44 | 10 | 30 |
| photo-4032x3024.jpg | 1 | 79 | 157 (6) | 78 | 28 | 47 |

The Python stages for the same photo, with 4 threads, are open 67 ms, fit 218 ms, and
write 103 ms (`bench/bench.py --stages`).

These are the main points:

- **Startup.** For a small image, the start of Python is all of the time. Rust is
  about 15 times faster.
- **Fit.** The exact port of the Pillow resize is 3 times faster than Pillow with 4
  threads. `--resizer fir` takes 22 ms, not 68 ms, but its pixels are different. A
  SIMD version of the exact Pillow arithmetic can close this gap and keep the pixels.
- **Open.** The JPEG decode takes 80 to 100 ms for 12 MP. It is now the largest
  stage of a photo. libjpeg-turbo and zune-jpeg have about the same speed. The decode
  runs on one thread, as in Pillow.
- **DEFLATE.** For the flat image, the compression takes 90 % of the time. The text of
  the sheet takes 11 to 18 ms on one thread. A test with `libdeflate` on the same XML
  gave a file 24 % smaller than zlib, in 35 % less time. But libdeflate has no preset
  dictionary, so each chunk must start a new block.
- **styles.xml.** A photo has about 64,000 colors, so `styles.xml` has about 12 MB of
  text. Its text and its DEFLATE take 30 ms with 4 threads.

### Limits of the spike

- The samples are synthetic, and the data comes from one Linux VM. Nobody has measured
  Windows, the primary system of the project.
- The spike does not write zip64 members. It stops for a sheet part that can be larger
  than 2 GiB.
- The spike keeps the full sheet text of one window in memory, not one row.
- The spike writes the zip time in UTC. Python writes the local time.
- The spike does not check 16-bit gray images, CMYK, TIFF, and transparency from a
  color key. Read "Formats" in [PLAN.md](PLAN.md).
