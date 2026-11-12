//! The reason of a failed conversion, for people.

use std::io;
use std::path::Path;

use pixheet_core::{Error, Step, io_reason};

/// Why a conversion failed: a short reason for the card and a full text.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Failure {
    pub reason: String,
    pub detail: String,
}

pub(super) fn name(path: &Path) -> String {
    path.file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

fn folder(path: &Path) -> String {
    path.parent().unwrap_or(Path::new("")).display().to_string()
}

/// The reason of a failed conversion, for people.
pub fn classify(error: &Error, dst: &Path) -> Failure {
    let plain = |reason: &str, detail: String| Failure {
        reason: reason.into(),
        detail,
    };
    match error {
        Error::NotAnImage { .. } => plain(
            "Not an image",
            "The app cannot read this file as an image.".into(),
        ),
        Error::Decode { message, .. } => plain(
            "Damaged image",
            format!("The image data has an error: {message}"),
        ),
        Error::Invalid(message) => plain("Cannot convert", message.clone()),
        Error::Cancelled => plain("Stopped", "The conversion stopped.".into()),
        Error::Io {
            step: Step::Read,
            source,
            ..
        } => match source.kind() {
            io::ErrorKind::NotFound => {
                plain("File not found", "The image is not there any more.".into())
            }
            _ => plain(
                "Cannot read",
                format!("The app cannot read the image: {}.", io_reason(source)),
            ),
        },
        Error::Io { step, source, .. } => {
            let denied = source.kind() == io::ErrorKind::PermissionDenied
                || source.raw_os_error() == Some(30); // EROFS: a read-only disk
            if cfg!(windows) && *step == Step::Rename && denied && dst.is_file() {
                // The app wrote the temporary file, so it can write in the folder. Windows
                // does not let it replace a file that another app has open.
                return plain(
                    "File is in use",
                    format!(
                        "Another app has {} open, for example Excel. Close the file, then retry.",
                        name(dst)
                    ),
                );
            }
            if denied {
                return plain(
                    "Cannot write here",
                    format!(
                        "The app cannot write in {}. Choose another output folder, then retry.",
                        folder(dst)
                    ),
                );
            }
            if source.kind() == io::ErrorKind::NotFound {
                return plain(
                    "Folder not found",
                    format!(
                        "The folder {} is not there. Choose another output folder, then retry.",
                        folder(dst)
                    ),
                );
            }
            plain(
                "Cannot write",
                format!("The app cannot write {}: {}.", name(dst), io_reason(source)),
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    /// The reason of `classify` for an error of `step` with the system error `source`.
    fn io_failure(step: Step, source: io::Error, dst: &Path) -> Failure {
        let error = Error::Io {
            path: dst.to_path_buf(),
            step,
            source,
        };
        classify(&error, dst)
    }

    #[test]
    fn each_error_has_a_reason_for_people() {
        let dst = Path::new("out").join("a.xlsx");
        let reason = |error: Error| classify(&error, &dst).reason;
        let path = PathBuf::from("a.png");
        assert_eq!(
            reason(Error::NotAnImage { path: path.clone() }),
            "Not an image"
        );
        let decode = Error::Decode {
            path,
            message: "bad data".into(),
        };
        let failure = classify(&decode, &dst);
        assert_eq!(failure.reason, "Damaged image");
        assert_eq!(failure.detail, "The image data has an error: bad data");
        let failure = classify(&Error::Invalid("too large".into()), &dst);
        assert_eq!(failure.reason, "Cannot convert");
        assert_eq!(failure.detail, "too large");
        assert_eq!(reason(Error::Cancelled), "Stopped");
    }

    #[test]
    fn a_system_error_tells_the_step_and_the_cause() {
        let dst = Path::new("out").join("a.xlsx");
        let kind = io::Error::from;
        let read = |e| io_failure(Step::Read, e, &dst);
        assert_eq!(read(kind(io::ErrorKind::NotFound)).reason, "File not found");
        let failure = read(io::Error::other("disk error"));
        assert_eq!(failure.reason, "Cannot read");
        assert_eq!(failure.detail, "The app cannot read the image: disk error.");

        let write = |e| io_failure(Step::Write, e, &dst);
        let failure = write(kind(io::ErrorKind::PermissionDenied));
        assert_eq!(failure.reason, "Cannot write here");
        assert!(failure.detail.contains(&folder(&dst)), "{}", failure.detail);
        let failure = write(kind(io::ErrorKind::NotFound));
        assert_eq!(failure.reason, "Folder not found");
        assert!(failure.detail.contains(&folder(&dst)), "{}", failure.detail);
        let failure = write(io::Error::other("disk full"));
        assert_eq!(failure.reason, "Cannot write");
        assert_eq!(failure.detail, "The app cannot write a.xlsx: disk full.");

        // The output does not exist, so the rename is not blocked by another app.
        let rename = io_failure(Step::Rename, kind(io::ErrorKind::PermissionDenied), &dst);
        assert_eq!(rename.reason, "Cannot write here");
    }

    #[test]
    fn a_rename_over_an_open_file_tells_to_close_it() {
        let dir = tempfile::tempdir().unwrap();
        let dst = dir.path().join("a.xlsx");
        std::fs::write(&dst, b"old").unwrap();
        let denied = io::Error::from(io::ErrorKind::PermissionDenied);
        let failure = io_failure(Step::Rename, denied, &dst);
        // Only Windows locks a file that another app has open.
        if cfg!(windows) {
            assert_eq!(failure.reason, "File is in use");
            assert!(failure.detail.contains("a.xlsx"), "{}", failure.detail);
        } else {
            assert_eq!(failure.reason, "Cannot write here");
        }
    }
}
