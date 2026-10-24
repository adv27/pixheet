# Changelog

This file tells the changes in each version of Pixheet. The release job in
`.github/workflows/release.yml` uses the section of the version as the release notes.
The release page on GitHub shows the date of each release.

To add a version, do these steps:

1. Add a section `## <version>` at the top, below this text.
2. Write the changes in the subsections Added, Changed, Fixed and Removed. Leave out a
   subsection that has no items.
3. Change `version` in `pyproject.toml`, and run `uv lock`.

The CI job "Check the version tag" stops a pull request when `CHANGELOG.md` has no
section for the version in `pyproject.toml`. Write the text in Simplified Technical
English (ASD-STE100). Read [AGENTS.md](AGENTS.md).

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
  status of each image. **Stop**, **Retry failed** and **Failed only** control the run.
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
