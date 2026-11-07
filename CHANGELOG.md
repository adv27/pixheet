# Changelog

This file tells the changes in each version of Pixheet. The release job in
`.github/workflows/release.yml` uses the section of the version as the release notes.
The release page on GitHub shows the date of each release.

To add a version, do these steps:

1. Rename the section `## Unreleased` to `## <version>`, or add a section `## <version>`
   at the top, below this text.
2. Write the changes in the subsections Added, Changed, Fixed and Removed. Leave out a
   subsection that has no items.
3. Change `version` in `[workspace.package]` of `Cargo.toml`, and run `cargo update -w`.

The CI job "Check the version tag" stops a pull request when `CHANGELOG.md` has no
section for the version in `Cargo.toml`. Write the text in Simplified Technical English
(ASD-STE100). Read [AGENTS.md](AGENTS.md).

## Unreleased

Pixheet is now written in Rust. The output is the same as the output of version 0.1.0:
the same pixels and the same sheet XML.

### Added

- The command `pixheet` in Rust, with the options of version 0.1.0. It is 3 to 5 times
  faster, and it starts in less than 5 ms.
- A new desktop app (Tauri 2) for Windows, Linux and macOS. Each image gets a card with
  a preview of the sheet. A sidebar holds the settings: the size, the background, the
  output folder, the policy for existing files, subfolders, and the theme.
- The library `pixheet-core`, with `convert`, `prepare`, `load`, `fit_image` and
  `write`.
- Release builds for Windows x64, Linux x64 and macOS arm64.

### Changed

- The `.xlsx` files are 2 to 3% larger: they use the DEFLATE level 3, which is about 2
  times faster.
- The Linux app needs WebKitGTK 4.1, for example Ubuntu 22.04 or Debian 12.

### Removed

- The Python package, its API (`convert()`, `prepare()`), and the Tk app. The branch
  `python-legacy` keeps them.

## 0.1.0

This is the first release of Pixheet.

### Added

- The command `pixheet` converts images to `.xlsx` files. Each cell is one pixel and has
  the color of that pixel.
- The command has the options `--output`, `--max-side`, `--background`, `--compact`
  and `--quiet`.
- The Python API has `convert()`, `prepare()` and `ConvertResult`.
- When an image has more than 63,999 colors, Pixheet makes the image smaller. A search
  finds the largest size whose colors fit in one sheet.
- Pixheet uses up to 8 threads to resize the image and to compress the sheet. The cells
  are the same as with one thread.
- The `gui` extra adds the desktop app `pixheet-gui`. Drop images or folders on the
  window, or click **Add files...** or **Add folder...**.
- The desktop app shows a list of the images, with the progress, the time left and the
  status of each image. **Stop** and **Retry failed** control the run. **Failed only**
  shows only the failed images.
- The desktop app asks before it replaces an output file. When two images have the same
  output name, each output gets a tag, for example `photo (png).xlsx`.
- The desktop app has the Sun Valley theme, with the settings **System**, **Light**
  and **Dark**.
- `Pixheet.exe` for Windows and `Pixheet-linux-x86_64` for Linux are the desktop app in
  one file.

### Known limits

- On Linux, drag and drop needs libX11 1.7 or later and libxcb 1.14 or later. Ubuntu
  18.04, Debian 10 and RHEL 8 have an older libxcb. On these systems, use **Add
  files...** and **Add folder...**.
- On Linux, the text uses the core X fonts, without antialiasing.
- Only Excel 16 and openpyxl have tests of compact mode.
