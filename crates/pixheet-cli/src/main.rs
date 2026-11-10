//! The `pixheet` command: `pixheet INPUT [INPUT ...] [-o OUTPUT] ...`.
//!
//! A port of `pixheet.cli`. The report line, the exit status and the error texts are
//! the same as in the Python version.

use std::collections::HashMap;
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use clap::{CommandFactory, Parser, error::ErrorKind};
use pixheet_core::{
    ConvertResult, DEFAULT_MAX_SIDE, Error, Options, Pixels, check_output, convert_loaded, load,
    parse_background,
};

/// mimalloc keeps freed memory for the next allocation. The image buffers are large, and
/// the system allocator gives them back to the system after each use. Then each new
/// buffer costs page faults again.
#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The parsed --background value. None drops the alpha.
#[derive(Clone, Copy, Debug)]
struct Background(Option<[u8; 3]>);

fn max_side_arg(text: &str) -> Result<usize, String> {
    let value: i64 = text
        .parse()
        .map_err(|_| format!("not an integer: '{text}'"))?;
    if value < 0 {
        return Err(format!("must be >= 0, got {value}"));
    }
    usize::try_from(value).map_err(|_| format!("not an integer: '{text}'"))
}

fn background_arg(text: &str) -> Result<Background, String> {
    parse_background(text)
        .map(Background)
        .map_err(|e| e.to_string())
}

/// Turn images into .xlsx spreadsheets where every cell is one pixel. Open the file in a
/// spreadsheet app and zoom out to see the picture.
#[derive(Parser)]
#[command(name = "pixheet", version)]
struct Args {
    /// image file(s) to convert
    #[arg(required = true, value_name = "INPUT")]
    inputs: Vec<OsString>,

    /// output .xlsx path (single INPUT only; default: INPUT with an .xlsx suffix)
    #[arg(short, long, value_name = "OUTPUT")]
    output: Option<PathBuf>,

    /// cap on the longest side in pixels; 0 = only the sheet limits apply
    #[arg(long, value_name = "N", default_value_t = DEFAULT_MAX_SIDE,
          value_parser = max_side_arg, allow_negative_numbers = true)]
    max_side: usize,

    /// color for transparent areas: white, black, #RRGGBB, or none to drop alpha
    #[arg(long, value_name = "COLOR", default_value = "white", value_parser = background_arg)]
    background: Background,

    /// leave out cell references: smaller and faster, verified in Excel and openpyxl only
    #[arg(long)]
    compact: bool,

    /// only print errors
    #[arg(short, long)]
    quiet: bool,
}

/// True when the text has a wildcard of a file-name pattern.
fn has_wildcard(text: &str) -> bool {
    text.contains(['*', '?', '['])
}

/// Expand the wildcards that the shell left alone (cmd and PowerShell do not expand them).
///
/// The file-name part must hold the wildcard. In the folder part, "*" and "?" are
/// wildcards too, but brackets are literal, as in "shots [2024]/*.jpg". An existing file
/// such as "img[1].png" is taken as it is. A pattern without a match stays as it is.
fn expand(inputs: &[OsString]) -> Vec<PathBuf> {
    let options = glob::MatchOptions {
        case_sensitive: !cfg!(windows),
        require_literal_separator: true,
        require_literal_leading_dot: true,
    };
    let mut paths = Vec::new();
    for arg in inputs {
        let path = PathBuf::from(arg);
        let tail = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let Some(text) = arg.to_str() else {
            paths.push(path);
            continue;
        };
        if !has_wildcard(&tail) || path.exists() {
            paths.push(path);
            continue;
        }
        let head = path
            .parent()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        let pattern = if head.is_empty() {
            tail.clone()
        } else {
            let sep = &text[head.len()..text.len() - tail.len()];
            format!("{}{sep}{tail}", head.replace('[', "[[]"))
        };
        let mut found: Vec<PathBuf> = glob::glob_with(&pattern, options)
            .map(|matches| matches.filter_map(Result::ok).collect())
            .unwrap_or_default();
        found.sort();
        if found.is_empty() {
            paths.push(path);
        } else {
            paths.extend(found);
        }
    }
    paths
}

/// `os.path.normcase(path.resolve())`: one key for two spellings of one output.
fn output_key(path: &Path) -> PathBuf {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    let resolved = absolute.canonicalize().unwrap_or_else(|_| {
        match (absolute.parent(), absolute.file_name()) {
            (Some(parent), Some(name)) => parent
                .canonicalize()
                .map(|p| p.join(name))
                .unwrap_or_else(|_| absolute.clone()),
            _ => absolute.clone(),
        }
    });
    if cfg!(windows) {
        PathBuf::from(resolved.to_string_lossy().to_lowercase().replace('/', "\\"))
    } else {
        resolved
    }
}

/// `1234567` gives "1,234,567".
fn grouped(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// The report line of one converted file.
fn summary(name: &str, result: &ConvertResult) -> String {
    let ((ow, oh), (w, h)) = (result.original_size, result.size);
    format!(
        "{name}  {ow}x{oh} -> {w}x{h}  {} colors  {} cells  {:.2}s",
        grouped(result.colors),
        grouped(result.cells),
        result.seconds
    )
}

/// Use the CPUs of the system, at most 8, as the Python version does. The environment
/// variable RAYON_NUM_THREADS wins.
fn set_threads() {
    if std::env::var_os("RAYON_NUM_THREADS").is_some() {
        return;
    }
    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    let _ = rayon::ThreadPoolBuilder::new()
        .num_threads(cpus.min(8))
        .build_global();
}

/// Decode the images on a thread of their own, in the order of `inputs`.
///
/// The decode of a JPEG uses one CPU, and the other steps use all of them. Thus the
/// reader decodes the next image while the main thread converts the current one. The
/// channel has no buffer: the reader waits with one image until the main thread takes it.
fn reader(
    inputs: Vec<PathBuf>,
    background: Option<[u8; 3]>,
) -> mpsc::Receiver<(Duration, Result<Pixels, Error>)> {
    let (tx, rx) = mpsc::sync_channel(0);
    std::thread::Builder::new()
        .name("pixheet-reader".into())
        .spawn(move || {
            for src in inputs {
                let start = Instant::now();
                let image = load(&src, background);
                if tx.send((start.elapsed(), image)).is_err() {
                    return;
                }
            }
        })
        .expect("start the reader thread");
    rx
}

fn main() -> ExitCode {
    let args = Args::parse();
    let inputs = expand(&args.inputs);
    if args.output.is_some() && inputs.len() > 1 {
        Args::command()
            .error(
                ErrorKind::ArgumentConflict,
                "-o/--output can only be used with a single INPUT",
            )
            .exit();
    }
    set_threads();
    let options = Options {
        max_side: args.max_side,
        background: args.background.0,
        compact: args.compact,
    };
    let mut failed = 0;
    // The normalized output path, and the input that wrote it.
    let mut written: HashMap<PathBuf, String> = HashMap::new();
    let mut stdout = std::io::stdout().lock();
    let images = reader(inputs.clone(), options.background);
    for src in &inputs {
        let (seconds, loaded) = images
            .recv()
            .expect("the reader sends one image for each input");
        let name = src.display().to_string();
        let dst = args
            .output
            .clone()
            .unwrap_or_else(|| src.with_extension("xlsx"));
        let key = output_key(&dst);
        let result = match written.get(&key) {
            Some(first) => Err(format!(
                "{} was already written from {first}",
                dst.display()
            )),
            None => check_output(src, &dst)
                .and(loaded)
                .and_then(|upright| {
                    // The time of the result counts the decode on the reader thread too.
                    let start = Instant::now()
                        .checked_sub(seconds)
                        .unwrap_or_else(Instant::now);
                    convert_loaded(upright, &dst, &options, None, start)
                })
                .map_err(|e| e.to_string()),
        };
        match result {
            Ok(result) => {
                written.insert(key, name.clone());
                if !args.quiet {
                    let _ = writeln!(stdout, "{}", summary(&name, &result));
                    let _ = stdout.flush();
                }
            }
            Err(message) => {
                failed += 1;
                eprintln!("pixheet: {name}: {message}");
            }
        }
    }
    if failed > 0 {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grouped_numbers() {
        assert_eq!(grouped(0), "0");
        assert_eq!(grouped(999), "999");
        assert_eq!(grouped(1_000), "1,000");
        assert_eq!(grouped(63_999), "63,999");
        assert_eq!(grouped(1_234_567), "1,234,567");
    }

    #[test]
    fn wildcards_expand_in_order() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["img10.png", "img2.png", ".hidden.png", "other.jpg"] {
            std::fs::write(dir.path().join(name), b"").unwrap();
        }
        let pattern = dir.path().join("*.png").into_os_string();
        let found = expand(&[pattern]);
        let names: Vec<_> = found
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, ["img10.png", "img2.png"]);
    }

    #[test]
    fn brackets_in_the_folder_are_literal() {
        let dir = tempfile::tempdir().unwrap();
        let folder = dir.path().join("shots [2024]");
        std::fs::create_dir(&folder).unwrap();
        std::fs::write(folder.join("a.png"), b"").unwrap();
        let found = expand(&[folder.join("*.png").into_os_string()]);
        assert_eq!(found, [folder.join("a.png")]);
    }

    #[test]
    fn an_existing_file_with_brackets_is_not_a_pattern() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("img[1].png");
        std::fs::write(&file, b"").unwrap();
        assert_eq!(expand(&[file.clone().into_os_string()]), [file]);
    }

    #[test]
    fn two_spellings_give_one_key() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("x.xlsx");
        let b = dir.path().join(".").join("x.xlsx");
        assert_eq!(output_key(&a), output_key(&b));
    }
}
