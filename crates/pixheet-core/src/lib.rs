//! Pixheet turns an image into an .xlsx spreadsheet where every cell is one pixel.
//!
//! This crate is the Rust port of the Python package `pixheet`. With the `turbo` feature
//! (the default), it gives the same pixels and the same sheet XML as the Python version.
//!
//! ```no_run
//! use std::path::Path;
//! use pixheet_core::{Options, convert};
//!
//! let result = convert(Path::new("photo.jpg"), Path::new("photo.xlsx"), &Options::default(), None)?;
//! println!("{}x{} {} colors", result.size.0, result.size.1, result.colors);
//! # Ok::<(), pixheet_core::Error>(())
//! ```

mod color;
mod color_names;
mod convert;
mod decode;
mod error;
mod fit;
mod resample;
mod xlsx;

pub use color::parse_background;
pub use convert::{
    ConvertResult, DEFAULT_MAX_SIDE, Options, TEMP_PREFIX, check_output, convert, convert_loaded,
    fit_image, load, prepare, write,
};
pub use error::{Error, Step, io_reason};
pub use resample::Pixels;
pub use xlsx::{MAX_COLORS, MAX_COLUMNS, MAX_ROWS};
