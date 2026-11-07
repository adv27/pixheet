//! The public conversion API: a port of `pixheet.core`.

use std::fs::File;
use std::io::BufWriter;
use std::ops::ControlFlow;
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::decode::{normalize, open};
use crate::error::{Error, Step, quoted};
use crate::fit::fit;
use crate::resample::Pixels;
use crate::xlsx::{WriteError, write_xlsx};

/// The default cap on the longest side, in pixels.
pub const DEFAULT_MAX_SIDE: usize = 1000;

/// The start of the name of the temporary file next to the output.
pub const TEMP_PREFIX: &str = ".pixheet-";

/// The options of a conversion.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Options {
    /// The cap on the longest side in pixels. 0 means only the sheet limits apply.
    pub max_side: usize,
    /// The color under transparent areas. None drops the alpha without a blend.
    pub background: Option<[u8; 3]>,
    /// Leave out the optional `r="A1"` cell references.
    pub compact: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            max_side: DEFAULT_MAX_SIDE,
            background: Some([255, 255, 255]),
            compact: false,
        }
    }
}

/// What [`convert`] did.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ConvertResult {
    /// (width, height) of the source image, after the EXIF orientation.
    pub original_size: (usize, usize),
    /// (width, height) of the sheet: one cell for each pixel.
    pub size: (usize, usize),
    /// The number of distinct colors, that is the number of cell styles.
    pub colors: usize,
    /// The number of cells written (width * height).
    pub cells: usize,
    /// The wall-clock time of the whole conversion, in seconds.
    pub seconds: f64,
}

/// Open, normalize and fit `src`. Return the upright size and the image that
/// [`convert`] would write, one pixel for each cell.
pub fn prepare(src: &Path, options: &Options) -> Result<((usize, usize), Pixels), Error> {
    let upright = normalize(open(src)?, options.background);
    let size = (upright.w, upright.h);
    Ok((size, fit(upright, options.max_side)?))
}

/// True when `src` and `dst` name one file that exists.
fn same_file(src: &Path, dst: &Path) -> bool {
    same_file::is_same_file(src, dst).unwrap_or(false)
}

/// A name for the temporary file: 16 random hex digits, so the length is fixed.
fn temp_path(dst: &Path) -> PathBuf {
    let mut bytes = [0u8; 8];
    if getrandom::fill(&mut bytes).is_err() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        bytes = (nanos ^ u64::from(std::process::id())).to_le_bytes();
    }
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    dst.with_file_name(format!("{TEMP_PREFIX}{hex}.tmp"))
}

/// Convert an image into an .xlsx spreadsheet where every cell is one pixel.
///
/// The output is written next to `dst`, then renamed over it. On failure, an existing
/// file at `dst` does not change, and the temporary file goes away.
///
/// `progress` gets `(rows_done, rows_total)` while the sheet is written, on the calling
/// thread. The first call is `(0, rows_total)` and the last call is
/// `(rows_total, rows_total)`. When it returns `Break`, the conversion stops with
/// [`Error::Cancelled`].
///
/// The image is shrunk, with its aspect ratio, to the largest size that fits
/// `max_side`, the sheet grid and 63,999 distinct colors (the cell-format limit of Excel).
pub fn convert(
    src: &Path,
    dst: &Path,
    options: &Options,
    progress: Option<&mut dyn FnMut(usize, usize) -> ControlFlow<()>>,
) -> Result<ConvertResult, Error> {
    let start = Instant::now();
    if same_file(src, dst) {
        return Err(Error::Invalid(format!(
            "the output {} is the input image; choose another path",
            quoted(&dst.display().to_string())
        )));
    }
    let (original_size, image) = prepare(src, options)?;
    let mut ignore = |_: usize, _: usize| ControlFlow::Continue(());
    let progress = progress.unwrap_or(&mut ignore);
    let colors = write_path(&image, dst, options.compact, progress)?;
    Ok(ConvertResult {
        original_size,
        size: (image.w, image.h),
        colors,
        cells: image.w * image.h,
        seconds: start.elapsed().as_secs_f64(),
    })
}

/// Write `image` to a temporary file next to `dst`, then rename it over `dst`.
fn write_path(
    image: &Pixels,
    dst: &Path,
    compact: bool,
    progress: &mut dyn FnMut(usize, usize) -> ControlFlow<()>,
) -> Result<usize, Error> {
    let tmp = temp_path(dst);
    let io_error = |step: Step| {
        move |source| Error::Io {
            path: dst.to_path_buf(),
            step,
            source,
        }
    };
    let file = File::create_new(&tmp).map_err(io_error(Step::Write))?;
    let written = write_xlsx(
        image,
        BufWriter::with_capacity(1 << 20, file),
        compact,
        progress,
    );
    let result = match written {
        Ok(colors) => std::fs::rename(&tmp, dst)
            .map(|()| colors)
            .map_err(io_error(Step::Rename)),
        Err(WriteError::Io(source)) => Err(io_error(Step::Write)(source)),
        Err(WriteError::Invalid(message)) => Err(Error::Invalid(message)),
        Err(WriteError::Cancelled) => Err(Error::Cancelled),
    };
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png(path: &Path, w: u32, h: u32) {
        let im = image::RgbImage::from_fn(w, h, |x, y| image::Rgb([x as u8 * 40, y as u8 * 40, 7]));
        im.save(path).unwrap();
    }

    fn temp_files(dir: &Path) -> Vec<String> {
        std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with(TEMP_PREFIX))
            .collect()
    }

    #[test]
    fn convert_writes_the_sheet() {
        let dir = tempfile::tempdir().unwrap();
        let (src, dst) = (dir.path().join("a.png"), dir.path().join("a.xlsx"));
        png(&src, 4, 3);
        let result = convert(&src, &dst, &Options::default(), None).unwrap();
        assert_eq!(result.original_size, (4, 3));
        assert_eq!(result.size, (4, 3));
        assert_eq!(result.colors, 12);
        assert_eq!(result.cells, 12);
        assert!(dst.is_file());
        assert!(temp_files(dir.path()).is_empty());
    }

    #[test]
    fn the_output_cannot_be_the_input() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("a.png");
        png(&src, 2, 2);
        let before = std::fs::read(&src).unwrap();
        let err = convert(&src, &src, &Options::default(), None).unwrap_err();
        assert!(err.to_string().contains("is the input image"), "{err}");
        assert_eq!(std::fs::read(&src).unwrap(), before);
    }

    #[test]
    fn a_stop_keeps_the_old_output() {
        let dir = tempfile::tempdir().unwrap();
        let (src, dst) = (dir.path().join("a.png"), dir.path().join("a.xlsx"));
        png(&src, 4, 3);
        std::fs::write(&dst, b"old").unwrap();
        let mut stop = |done: usize, _: usize| {
            if done > 0 {
                ControlFlow::Break(())
            } else {
                ControlFlow::Continue(())
            }
        };
        let err = convert(&src, &dst, &Options::default(), Some(&mut stop)).unwrap_err();
        assert!(matches!(err, Error::Cancelled));
        assert_eq!(std::fs::read(&dst).unwrap(), b"old");
        assert!(temp_files(dir.path()).is_empty());
    }

    #[test]
    fn a_file_that_is_not_an_image_fails() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("a.png");
        std::fs::write(&src, b"not an image").unwrap();
        let err = convert(&src, &dir.path().join("a.xlsx"), &Options::default(), None);
        let err = err.unwrap_err();
        assert!(matches!(err, Error::NotAnImage { .. }));
        assert!(err.to_string().starts_with("cannot identify image file '"));
    }

    #[test]
    fn a_missing_file_names_the_source() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("missing.png");
        let err = convert(&src, &dir.path().join("a.xlsx"), &Options::default(), None);
        match err.unwrap_err() {
            Error::Io {
                path,
                step: Step::Read,
                ..
            } => assert_eq!(path, src),
            other => panic!("{other}"),
        }
    }

    #[test]
    fn a_missing_output_folder_names_the_output() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("a.png");
        png(&src, 2, 2);
        let dst = dir.path().join("no").join("a.xlsx");
        match convert(&src, &dst, &Options::default(), None).unwrap_err() {
            Error::Io {
                path,
                step: Step::Write,
                source,
            } => {
                assert_eq!(path, dst);
                assert_eq!(source.kind(), std::io::ErrorKind::NotFound);
            }
            other => panic!("{other}"),
        }
    }

    #[test]
    fn transparency_takes_the_background() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("t.png");
        image::RgbaImage::from_pixel(3, 2, image::Rgba([10, 20, 30, 0]))
            .save(&src)
            .unwrap();
        let red = Options {
            background: Some([255, 0, 0]),
            ..Options::default()
        };
        let (_, im) = prepare(&src, &red).unwrap();
        assert_eq!(im.get(0, 0), [255, 0, 0]);
        let none = Options {
            background: None,
            ..Options::default()
        };
        let (_, im) = prepare(&src, &none).unwrap();
        assert_eq!(im.get(2, 1), [10, 20, 30]);
    }
}
