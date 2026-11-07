//! Spike: the `pixheet` command in Rust. It measures the speed and the parity with the
//! Python version. It is not a product: read `spike/README.md`.

mod decode;
mod fit;
mod resample;
mod xlsx;

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use clap::{Parser, ValueEnum};

use crate::fit::Resizer;

#[derive(Clone, Copy, ValueEnum)]
enum ResizerArg {
    /// The port of Pillow's LANCZOS: the same pixels as the Python version.
    Pillow,
    /// fast_image_resize: SIMD, but other pixels.
    Fir,
}

/// Turn images into .xlsx spreadsheets where every cell is one pixel (Rust spike).
#[derive(Parser)]
#[command(name = "pixheet-rs", version)]
struct Args {
    /// Image file(s) to convert.
    #[arg(required = true, value_name = "INPUT")]
    inputs: Vec<PathBuf>,
    /// Output .xlsx path (single INPUT only; default: INPUT with an .xlsx suffix).
    #[arg(short, long, value_name = "OUTPUT")]
    output: Option<PathBuf>,
    /// Cap on the longest side in pixels; 0 = only the sheet limits apply.
    #[arg(long, value_name = "N", default_value_t = 1000)]
    max_side: usize,
    /// Color for transparent areas: white, black, #RRGGBB, or none to drop alpha.
    #[arg(long, value_name = "COLOR", default_value = "white")]
    background: String,
    /// Leave out cell references.
    #[arg(long)]
    compact: bool,
    /// Only print errors.
    #[arg(short, long)]
    quiet: bool,
    /// Spike: the number of threads (default: the CPUs, at most 8).
    #[arg(short = 'j', long)]
    threads: Option<usize>,
    /// Spike: print the time of each stage to stderr.
    #[arg(long)]
    stages: bool,
    /// Spike: the resize code.
    #[arg(long, value_enum, default_value = "pillow")]
    resizer: ResizerArg,
    /// Spike: also save the fitted image as PNG, to compare it with prepare().
    #[arg(long, value_name = "PNG")]
    dump_fitted: Option<PathBuf>,
    /// Spike: write the input as it is, without the fit (the input must fit already).
    #[arg(long)]
    no_fit: bool,
}

fn group(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn ms(d: std::time::Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn main() -> ExitCode {
    let start_process = Instant::now();
    let args = Args::parse();
    let threads = args.threads.unwrap_or_else(|| {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
            .min(8)
    });
    rayon::ThreadPoolBuilder::new()
        .num_threads(threads)
        .build_global()
        .expect("thread pool");
    let background = match decode::parse_background(&args.background) {
        Ok(bg) => bg,
        Err(e) => {
            eprintln!("pixheet-rs: error: argument --background: {e}");
            return ExitCode::from(2);
        }
    };
    if args.output.is_some() && args.inputs.len() > 1 {
        eprintln!("pixheet-rs: error: -o/--output can only be used with a single INPUT");
        return ExitCode::from(2);
    }
    let resizer = match args.resizer {
        ResizerArg::Pillow => Resizer::Pillow,
        ResizerArg::Fir => Resizer::Fir,
    };
    let mut failed = 0;
    for src in &args.inputs {
        let name = src.display().to_string();
        let start = Instant::now();
        let result = (|| -> Result<_, String> {
            let dst = args
                .output
                .clone()
                .unwrap_or_else(|| src.with_extension("xlsx"));
            let t = Instant::now();
            let im = decode::open(src)?;
            let t_open = t.elapsed();
            let t = Instant::now();
            let upright = decode::normalize(im, background);
            let t_norm = t.elapsed();
            let original = (upright.w, upright.h);
            let t = Instant::now();
            let fitted = if args.no_fit {
                upright
            } else {
                fit::fit(upright, args.max_side, resizer)
            };
            let t_fit = t.elapsed();
            if let Some(png) = &args.dump_fitted {
                image::save_buffer(
                    png,
                    &fitted.data,
                    fitted.w as u32,
                    fitted.h as u32,
                    image::ExtendedColorType::Rgb8,
                )
                .map_err(|e| e.to_string())?;
            }
            let t = Instant::now();
            let (colors, wt) =
                xlsx::write_path(&fitted, &dst, args.compact).map_err(|e| format!("{e}"))?;
            let t_write = t.elapsed();
            if args.stages {
                let t = Instant::now();
                xlsx::sheet_text_only(&fitted, args.compact);
                let t_text = t.elapsed();
                eprintln!(
                    "stages {name}: open {:.1} norm {:.1} fit {:.1} (probes {}) write {:.1} \
                     [ids {:.1} sheet {:.1} rest {:.1}; text alone, 1 thread {:.1}] ms",
                    ms(t_open),
                    ms(t_norm),
                    ms(t_fit),
                    fit::PROBES.load(std::sync::atomic::Ordering::Relaxed),
                    ms(t_write),
                    ms(wt.ids),
                    ms(wt.sheet),
                    ms(wt.rest),
                    ms(t_text),
                );
            }
            Ok((original, (fitted.w, fitted.h), colors))
        })();
        match result {
            Ok(((ow, oh), (w, h), colors)) => {
                if !args.quiet {
                    println!(
                        "{name}  {ow}x{oh} -> {w}x{h}  {} colors  {} cells  {:.2}s",
                        group(colors),
                        group(w * h),
                        start.elapsed().as_secs_f64()
                    );
                }
            }
            Err(e) => {
                failed += 1;
                eprintln!("pixheet-rs: {name}: {e}");
            }
        }
    }
    if args.stages {
        eprintln!(
            "process {:.1} ms, {threads} threads",
            ms(start_process.elapsed())
        );
    }
    if failed > 0 {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}
