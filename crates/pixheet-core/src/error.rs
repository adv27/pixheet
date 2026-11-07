//! The errors of a conversion.

use std::fmt;
use std::io;
use std::path::PathBuf;

/// The step of the conversion that had a system error.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    /// Read the source image.
    Read,
    /// Write the temporary file next to the output.
    Write,
    /// Rename the temporary file over the output.
    Rename,
}

/// An error of [`crate::convert`].
#[derive(Debug)]
pub enum Error {
    /// The decoders cannot identify the file as an image.
    NotAnImage { path: PathBuf },
    /// The decoder found an error in the image data.
    Decode { path: PathBuf, message: String },
    /// A system error. `path` is the file of the error: the source for [`Step::Read`],
    /// the output for [`Step::Write`] and [`Step::Rename`], not the temporary file.
    Io {
        path: PathBuf,
        step: Step,
        source: io::Error,
    },
    /// A value that is not valid, for example a background color.
    Invalid(String),
    /// The progress function stopped the conversion.
    Cancelled,
}

/// A path in quotes, as `repr()` of Python shows a simple string.
pub(crate) fn quoted(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// The text of a system error without the error number, for example "Permission denied".
pub fn io_reason(error: &io::Error) -> String {
    let text = error.to_string();
    match text.rfind(" (os error ") {
        Some(i) if text.ends_with(')') => text[..i].to_string(),
        _ => text,
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::NotAnImage { path } => {
                write!(
                    f,
                    "cannot identify image file {}",
                    quoted(&path.display().to_string())
                )
            }
            Error::Decode { message, .. } => f.write_str(message),
            Error::Io { path, source, .. } => {
                write!(
                    f,
                    "{}: {}",
                    io_reason(source),
                    quoted(&path.display().to_string())
                )
            }
            Error::Invalid(message) => f.write_str(message),
            Error::Cancelled => f.write_str("the conversion stopped"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoted_is_like_python_repr() {
        assert_eq!(quoted("a.png"), "'a.png'");
        assert_eq!(quoted(r"C:\a.png"), r"'C:\\a.png'");
        assert_eq!(quoted("it's.png"), "\"it's.png\"");
    }

    #[test]
    fn io_errors_name_the_path_without_the_number() {
        let e = Error::Io {
            path: "out.xlsx".into(),
            step: Step::Write,
            source: io::Error::from_raw_os_error(13),
        };
        assert!(e.to_string().ends_with(": 'out.xlsx'"));
        assert!(!e.to_string().contains("os error"));
    }
}
