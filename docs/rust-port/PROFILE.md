# Profile of the Rust command

> **History.** This file records the port of Pixheet from Python to Rust in November 2026.
> The Python version and its scripts are gone now; the branch `python-legacy` keeps them.
> Today, `cargo xtask samples DIR` makes the sample images, `cargo xtask bench DIR` times
> the command, and the golden test of `pixheet-core` keeps the parity with the Python
> version. Read [AGENTS.md](../../AGENTS.md).

This file tells where the Rust command spends its time, what changed after the
profile, and what can still improve. Phase 2 of [PLAN.md](PLAN.md) uses it.

## Method

- One Linux VM: 4 vCPUs (Intel Xeon, 2.1 GHz, AVX2 and AVX-512), rustc 1.97.0.
- The synthetic samples of `spike/make_samples.py`. Nobody has measured real photos or
  Windows yet.
- `perf record -F 4000 -g --call-graph dwarf` on a build with debug symbols
  (`CARGO_PROFILE_RELEASE_DEBUG=1`), with `RAYON_NUM_THREADS=1` for a clean attribution.
- `perf stat -r 7` for the times, the CPUs in use and the page faults.
- `spike/bench_cli.py -n 7` for the table at the end: one process for each conversion,
  the best of 7 runs.

`scripts/rust_parity.py` checked each change: all 176 checks (the synthetic cases and
the samples, at `--max-side 1000` and `0`, normal and compact) give the same XML as the
Python version.

## The profile before the changes

The 12 MP JPEG on one thread (365 ms):

| Share | Code | Note |
|---:|---|---|
| 37 % | `resample::horizontal` | The first resize, 4032 to 1000 columns, and the probes. Scalar code: 3 bytes for each pixel block the vectors. |
| 13 % | zlib-rs DEFLATE | Mostly `styles.xml` (12 MB of text for 64,000 colors) and the sheet. |
| 10 % | Page faults | 32,663 faults: about 130 MB of new memory for each image. |
| 10 % | libjpeg-turbo | One thread. The Python version has the same decoder. |
| 7 % | `resample::vertical` | Vectorized, but SSE2 has no 32-bit multiply. |

With 4 threads, only 1.9 of the 4 CPUs worked: the decode runs on one thread.

## The changes

| Change | Why | Effect (12 MP JPEG, 4 threads) |
|---|---|---|
| The horizontal pass makes 16 rows together, in columns of fixed width. | Each tap of the filter is one multiply-add of 48 values, so the sums stay in vector registers. The sums are integers, so the bytes do not change. | 222 to 188 ms |
| The JPEG decoder keeps its buffer. | The code copied the 36 MB image row by row, also without padding. | 188 to 166 ms, 9,000 fewer page faults |
| mimalloc is the allocator of the command. | The system allocator gives large buffers back after each use. Then each new buffer costs page faults again. | Page faults: 24,000 to about 260. The time is noisy on this VM: in an A/B test, the 12 MP photo was 0 to 13 % faster, and the other images did not change. |
| DEFLATE level 3, not 6. | With zlib-rs, level 3 compresses about 2 times faster. The files are only 1.9 % (flat) and 2.9 % (photo) larger. Level 2 gives 10 % larger files for no more speed. | 158 to 123 ms. The flat image: 84 to 41 ms |
| The command decodes the next image during the current conversion. | The decode uses one CPU, and the other steps use all of them. A new API of `pixheet-core` (`load`, `fit_image`, `write`, `convert_loaded`) makes this possible. | A batch of the 5 samples: 513 to 269 ms |

## Before and after

`bench_cli.py`, the best of 7 runs. "before" is the commit before the changes.

| Image | python | before | after |
|---|---:|---:|---:|
| flat-1000x750.png | 0.234 s | 0.062 s | 0.045 s (5.2x) |
| photo-1920x1080.png | 0.356 s | 0.080 s | 0.071 s (5.0x) |
| photo-4032x3024.jpg | 0.479 s | 0.189 s | 0.126 s (3.8x) |
| photo-portrait-3000x4000.jpg | 0.426 s | 0.177 s | 0.124 s (3.4x) |
| pixel-64x64.png | 0.072 s | 0.005 s | 0.005 s (13.6x) |
| All 5 in one process | 1.189 s | 0.484 s | 0.248 s (4.8x) |

"Nx" is the time of `python` divided by the time of `after`. All columns use 4 CPUs.

## What can still improve

| Idea | Expected gain | Parity | Note |
|---|---|---|---|
| Build for x86-64-v3 (AVX2), or choose the code at run time. | 15 to 20 % of the resize and the counts. | Same bytes | A build for v3 does not start on a CPU without AVX2. Run-time selection needs `unsafe` for the call, and the crates forbid `unsafe` now. Your decision. |
| Decode a large JPEG at 1/2 or 1/4 scale (DCT scaling). | Up to half of the decode, the largest step of a photo that remains. | Other pixels | The Python version does not do this. It can be an option, off by default. |
| Count the colors of a large image on several threads. | Small: the counts take a few ms. | Same bytes | |
| Make the text of `styles.xml` on several threads. | A few ms for a photo. | Same bytes | |
| libdeflate | None now | Same XML | libdeflate ends each stream with a final block. Thus the chunks of the threads cannot join into one zip member. |
| Smaller files: DEFLATE level 6 or 9 again. | Files 2 to 3 % smaller, but 20 to 50 % slower. | Same XML | Change `LEVEL` in `xlsx/archive.rs`. |

The decode of the 12 MP photo takes about 80 ms on one thread (the spike measured it
with `--stages`, and the decoder did not change). That is most of the 126 ms of one
file now. For a batch, the reader thread hides most of it.
