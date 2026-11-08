# Plan: port Pixheet to Rust

> **History.** This file records the port of Pixheet from Python to Rust in November 2026.
> The Python version and its scripts are gone now; the branch `python-legacy` keeps them.
> Today, `cargo xtask samples DIR` makes the sample images, `cargo xtask bench DIR` times
> the command, and the golden test of `pixheet-core` keeps the parity with the Python
> version. Read [AGENTS.md](../../AGENTS.md).

This plan uses the results of the spike in [README.md](README.md). It starts with the
command line, as the spike did. The desktop app comes after, because it needs a
decision first.

## Summary

- The Rust command is 2.4 to 4.6 times faster than the Python command on the sample
  photos, with 4 CPUs. It is about 15 times faster on small images, because the
  binary starts in less than 5 ms, and Python needs about 70 ms.
- The Rust command can give the same pixels and the same XML as the Python command. To
  do this, it must use libjpeg-turbo for JPEG and an exact port of the Pillow resize.
  The spike has both, and all its parity checks pass.
- The largest risk is not the speed. The risks are the breadth of formats that Pillow
  reads, and the desktop app: `gui.py` and `_plan.py` have about 4,300 lines of code,
  and `tests/test_gui.py` tests them with Tk.

## The decisions for you

These decisions change the plan. The recommendation of each decision is first.

1. **Parity with Pillow.**
   - (Recommended) Require the same pixels and the same XML as Pillow 13, for the
     formats in the test suite. Then the Python tests are the specification, and
     `parity.py` can run in CI. The cost is libjpeg-turbo, a C build with `cmake` and
     `nasm`.
   - Or accept small pixel differences. Then a pure Rust decoder (zune-jpeg) and a
     SIMD resize (`fast_image_resize`) are possible. The sheet size can then differ by
     one row or column from the Python version.
2. **The desktop app.** Decided: a new design in Tauri 2, not a port of the Tk app.
   `crates/pixheet-app` has it. It shows a card with a preview of the sheet for each
   image, and a settings sidebar without dialogs.
3. **The Python API** (`convert()`, `prepare()`).
   - (Recommended) Keep it, on the Rust core, as a wheel that `maturin` builds.
   - Or remove it, and keep only the command and the app.
4. **The DEFLATE code.**
   - (Recommended) Use zlib-rs at level 6 first. It is safe Rust, and the files are
     up to 12 % larger than the files of the Python version.
   - Or use libdeflate. The files of the flat sample are 24 % smaller and the time is
     35 % less. It is C code, and each chunk starts a new block.
5. **The distribution.** GitHub releases only, as now, or also crates.io and PyPI.

## Architecture

Use one Cargo workspace in the repository root:

| Crate | Content |
|---|---|
| `pixheet-core` | A library: decode, normalize, fit, resample, and the xlsx writer. It has no I/O except a `Read` and a `Write + Seek`. It sends progress through a callback. |
| `pixheet-cli` | The `pixheet` binary: arguments, wildcards, the report line, and the exit status. |
| `pixheet-py` | Later: the PyO3 module for the Python API and the Tk app. |
| `pixheet-app` | The desktop app: Tauri 2 and a page in plain HTML, CSS and JavaScript. |

The Python modules map to Rust modules as follows:

| Python | Rust | Note |
|---|---|---|
| `core.py` | `pixheet-core::convert` | `ConvertResult`, the check that the output is not the input, and the atomic write. |
| `_image.py` | `decode`, `normalize`, `fit` | Done, with the same pixels as Pillow. |
| `_threads.py` | `resample`, `deflate` | rayon replaces the thread pool. The strips of `_threads.py` are not necessary: the port splits each pass by output rows. |
| `_xlsx.py` | `xlsx` | Done: the same XML, and zip64. |
| `cli.py` | `pixheet-cli` | `clap`, and the wildcards for cmd and PowerShell. |
| `_desktop.py`, `_plan.py`, `gui.py` | None: `pixheet-app` is a new design | Decision 2. |

## Formats

Pillow reads many formats and modes. The spike checks only some of them. Each item
below must have a parity test before phase 1 is complete.

| Case | Pillow | Rust status |
|---|---|---|
| JPEG, EXIF orientation 1 to 8 | libjpeg-turbo, `exif_transpose` | Same pixels with `turbo`. The spike tests orientation 6 only. |
| PNG RGB, RGBA, L, LA, P | Yes | Same pixels in the spike. |
| PNG with a transparent color key (`tRNS`) | Blends onto the background | The `png` crate expands it to alpha. Not tested. |
| 16-bit gray (`I;16`, 16-bit PNG and PGM) | Keeps the top 8 bits | The spike uses `v >> 8`. Not tested against `point(v / 256)`. |
| 16-bit RGB PNG | Keeps the top 8 bits | The spike uses `v >> 8`. Not tested. |
| Mode `I` with values above 255, mode `1` | Yes | Not in the image crate. Add tests or reject these inputs. |
| CMYK JPEG and TIFF | Yes | The image crate does not read CMYK. Add a conversion that gives the Pillow values. |
| TIFF, BMP, GIF, WebP | Yes | The image crate reads them. Not tested. |
| ICO, PCX, TGA, PPM, IM, and the other Pillow formats | Yes | Some are in the image crate. Decide which formats to keep. |

## Phases

### Phase 0: the spike (done)

The spike is in this folder. It gives the numbers and the parity results.

### Phase 1: the command line in Rust, with parity

Status: most of the work is done. `crates/pixheet-core` and `crates/pixheet-cli` have
the code. `scripts/rust_parity.py` gives the same XML as the Python version for 23
synthetic cases and 16 EXIF cases, normal and compact, at `--max-side 1000` and `0`.
These items are still open: an Excel check of the Rust outputs, and parity runs on real
photos and on Windows.

1. Make the workspace from the spike. Move `spike/rust-cli/src` into `pixheet-core`
   and `pixheet-cli`.
2. Add all the features of `cli.py` and `core.py`:
   - all CSS color names for `--background`;
   - the wildcards that cmd and PowerShell do not expand;
   - the check of two inputs with the same output, and of an output that is the input;
   - the same error texts and the same exit status;
   - zip64 for a sheet part that can be larger than 2 GiB;
   - the local time in the zip entries.
3. Write the sheet row by row into the DEFLATE chunks, so the memory does not grow
   with the sheet.
4. Add the formats of the table above, each with a test.
5. Port the tests. Keep these tests:
   - the golden test against xlsxwriter, byte for byte;
   - the mode cases of `tests/images.py`;
   - the regular expression for the report line of `tests/test_cli.py`.
6. Run `parity.py` in CI against the Python version on the synthetic images.
7. Add CI jobs for `cargo test`, `cargo clippy` and `cargo fmt --check` on Windows and
   Ubuntu. Pin each action to a full commit SHA.

The exit criteria of phase 1:

- All parity checks pass on Windows and Linux.
- `scripts/excel_check.ps1` passes for the Rust outputs in Excel 16.
- The Rust command is faster than the Python command for each sample.

### Phase 2: speed

[PROFILE.md](PROFILE.md) gives the profile, the changes that are done, and the
ideas that remain.

Do these steps one at a time. Measure each step with `bench_cli.py` and `--stages`.

1. Put the exact Pillow arithmetic of the resize in SIMD (SSE4.1 and AVX2, and NEON for
   ARM). The target is the 22 ms of `fast_image_resize`, with the same pixels.
2. Count the colors on several threads for large images.
3. Make `styles.xml` faster. Its text and DEFLATE take 30 ms for a photo.
4. Measure libdeflate (decision 4).
5. Decode large JPEG files at a reduced scale (DCT scaling), if you accept that the
   pixels change. This can halve the largest stage of a photo. It needs a decision,
   because Python does not do it.
6. Measure on real photos on Windows.

### Phase 3: the Python package on the Rust core

1. Build a `pixheet` wheel with `maturin`. Keep `convert()`, `prepare()` and
   `ConvertResult`.
2. Run the progress callback on the calling thread, as now.
3. Keep the Tk app. Change only its calls to the conversion code.
4. Run the full Python test suite against the wheel.
5. Change `packaging/pixheet.spec` and the build scripts for the extension module.

### Phase 4: the desktop app

Status: a first version is in `crates/pixheet-app`. It is a new design: a grid of
cards with a preview of each sheet, a settings sidebar (size, background, output folder,
the policy for existing files, subfolders, automatic start, compact files, theme), and a
run bar. The settings are saved in `settings.json` in the config folder of the app.
These items are still open:

- a test of the app on Windows and on a Linux desktop. On Windows, test the policy
  Replace with an output that Excel has open: the card must say "File is in use". With
  Keep both (the default), the output never exists before the write, so this case
  does not occur;
- a change of the output folder or of the policy during a run applies to the waiting
  images. Only the next image, which the reader has decoded already, keeps its output;
  the worker then refuses to write over a file that appeared;
- screen-reader tests with Narrator and Orca;
- the build of installers and one-file apps (phase 5).

### Phase 5: the release

1. Build the binaries on GitHub-hosted runners: Windows x64, Linux x86_64 (static musl
   or an old glibc), and macOS, if you want it.
2. Keep the rules of the current release: the `.sha256` files, the notices of the
   third-party licenses, and `CHANGELOG.md` as the release notes.
3. Make the notices from `cargo about` or a similar tool. The notices of libjpeg-turbo
   are necessary with `turbo`.

## Risks

- **Linux baseline.** Tauri 2 needs WebKitGTK 4.1: Ubuntu 22.04, Debian 12 or later.
  The Tk app runs on glibc 2.27 (Ubuntu 18.04). The Rust app thus drops the old
  systems. The release notes and the README must say so.
- **Pillow changes.** The resize and reduce port follows Pillow 13. A new Pillow can
  change the rounding. Then the parity tests fail. Pin the Pillow version in the
  parity job.
- **libjpeg-turbo build.** The `turbo` feature needs `cmake` and `nasm` on each CI
  runner and on each development computer.
- **Windows.** All the numbers come from Linux. Windows has a different file system, a
  different start time of processes, and antivirus scans of new binaries.
- **Excel.** The zip writer is new code. `excel_check.ps1` must pass before a release.
