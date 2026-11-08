//! The tasks that read or write images: the samples, the benchmark, and the sample of
//! pixels for scripts/excel_check.ps1.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

use image::{Rgb, RgbImage};
use pixheet_core::{Options, parse_background, prepare};

/// A small, fast random generator (xorshift64*). The images need no better randomness.
struct Random(u64);

impl Random {
    fn new(seed: u64) -> Self {
        Self(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// A number in 0..1.
    fn unit(&mut self) -> f32 {
        (self.next() >> 40) as f32 / (1u64 << 24) as f32
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

/// Smooth noise: random values on a grid of `cell` pixels, with bilinear steps between.
fn noise(w: usize, h: usize, cell: usize, rng: &mut Random) -> Vec<f32> {
    let gw = w / cell + 2;
    let gh = h / cell + 2;
    let grid: Vec<f32> = (0..gw * gh).map(|_| rng.unit()).collect();
    let mut out = Vec::with_capacity(w * h);
    for y in 0..h {
        let fy = y as f32 / cell as f32;
        let (y0, ty) = (fy as usize, fy.fract());
        for x in 0..w {
            let fx = x as f32 / cell as f32;
            let (x0, tx) = (fx as usize, fx.fract());
            let at = |gx: usize, gy: usize| grid[gy * gw + gx];
            let top = at(x0, y0) * (1.0 - tx) + at(x0 + 1, y0) * tx;
            let bottom = at(x0, y0 + 1) * (1.0 - tx) + at(x0 + 1, y0 + 1) * tx;
            out.push(top * (1.0 - ty) + bottom * ty);
        }
    }
    out
}

/// A synthetic photo: noise in several scales on a gradient. It has far more than 63,999
/// colors, so the fit search runs, as for a real photo.
pub fn photo(w: usize, h: usize, seed: u64) -> RgbImage {
    let mut rng = Random::new(seed);
    let channels: Vec<Vec<f32>> = (0..3)
        .map(|_| {
            let mut acc = vec![0.5f32; w * h];
            for (cell, amount) in [(64, 0.5f32), (16, 0.3), (4, 0.15), (1, 0.05)] {
                for (a, n) in acc.iter_mut().zip(noise(w, h, cell, &mut rng)) {
                    *a = *a * (1.0 - amount) + n * amount;
                }
            }
            acc
        })
        .collect();
    RgbImage::from_fn(w as u32, h as u32, |x, y| {
        let i = y as usize * w + x as usize;
        let gradient = [x as f32 / w as f32, y as f32 / h as f32, 0.35];
        let mut px = [0u8; 3];
        for c in 0..3 {
            px[c] = ((channels[c][i] * 0.55 + gradient[c] * 0.45) * 255.0).clamp(0.0, 255.0) as u8;
        }
        Rgb(px)
    })
}

/// A flat image: 60 rectangles of solid colors, as a logo or a screenshot.
pub fn flat(w: u32, h: u32) -> RgbImage {
    let mut im = RgbImage::from_pixel(w, h, Rgb([240, 240, 240]));
    for i in 0..60u32 {
        let color = Rgb([
            (i * 37 % 256) as u8,
            (i * 91 % 256) as u8,
            (i * 53 % 256) as u8,
        ]);
        for y in (i * 10)..(i * 10 + 120).min(h) {
            for x in (i * 15)..(i * 15 + 200).min(w) {
                im.put_pixel(x, y, color);
            }
        }
    }
    im
}

/// Write the sample images of the benchmark to `dir`.
pub fn samples(dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let jpeg = |im: &RgbImage, name: &str| -> Result<(), String> {
        let file = std::fs::File::create(dir.join(name)).map_err(|e| e.to_string())?;
        let mut encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(file, 90);
        encoder.encode_image(im).map_err(|e| e.to_string())
    };
    let png = |im: &RgbImage, name: &str| im.save(dir.join(name)).map_err(|e| e.to_string());
    jpeg(&photo(4032, 3024, 1), "photo-4032x3024.jpg")?;
    jpeg(&photo(3000, 4000, 2), "photo-portrait-3000x4000.jpg")?;
    png(&photo(1920, 1080, 3), "photo-1920x1080.png")?;
    png(&flat(1000, 750), "flat-1000x750.png")?;
    let mut rng = Random::new(4);
    let palette: Vec<Rgb<u8>> = (0..16)
        .map(|_| Rgb([rng.next() as u8, rng.next() as u8, rng.next() as u8]))
        .collect();
    let pixel = RgbImage::from_fn(64, 64, |_, _| palette[rng.below(16)]);
    png(&pixel, "pixel-64x64.png")?;
    println!("Wrote 5 sample images to {}", dir.display());
    Ok(())
}

fn best(cmd: &mut dyn FnMut() -> Command, runs: usize) -> Result<f64, String> {
    let mut best = f64::MAX;
    for _ in 0..runs.max(1) {
        let start = Instant::now();
        let status = cmd().status().map_err(|e| e.to_string())?;
        if !status.success() {
            return Err(format!("the command stopped with {status}"));
        }
        best = best.min(start.elapsed().as_secs_f64());
    }
    Ok(best)
}

/// Time the command `binary` on each image of `dir`, one process for each conversion.
pub fn bench(dir: &Path, binary: &Path, runs: usize) -> Result<(), String> {
    let mut images: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| format!("Cannot read {}: {e}", dir.display()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|e| e == "png" || e == "jpg"))
        .collect();
    images.sort();
    if images.is_empty() {
        return Err(format!(
            "{} has no .png or .jpg files. Run cargo xtask samples first.",
            dir.display()
        ));
    }
    let tmp = std::env::temp_dir().join(format!("pixheet-bench-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    println!("{:<32}{:>14}{:>14}", "image", "1 thread", "all CPUs");
    let run = |inputs: &[PathBuf], single: bool| {
        let out = tmp.join("out.xlsx");
        let mut cmd = || {
            let mut c = Command::new(binary);
            c.arg("-q").args(inputs);
            if inputs.len() == 1 {
                c.arg("-o").arg(&out);
            }
            if single {
                c.env("RAYON_NUM_THREADS", "1");
            }
            c
        };
        best(&mut cmd, runs)
    };
    for image in &images {
        let name = image.file_name().unwrap_or_default().to_string_lossy();
        let one = run(std::slice::from_ref(image), true)?;
        let all = run(std::slice::from_ref(image), false)?;
        println!("{name:<32}{:>13.3}s{:>13.3}s", one, all);
    }
    // The batch: one process for all images. The outputs go next to copies.
    let copies: Vec<PathBuf> = images
        .iter()
        .map(|p| {
            let copy = tmp.join(p.file_name().unwrap_or_default());
            std::fs::copy(p, &copy).map(|_| copy)
        })
        .collect::<Result<_, _>>()
        .map_err(|e| e.to_string())?;
    let one = run(&copies, true)?;
    let all = run(&copies, false)?;
    println!(
        "{:<32}{:>13.3}s{:>13.3}s",
        format!("all {} in one process", images.len()),
        one,
        all
    );
    println!("best of {runs} runs; the time includes the start of the process");
    let _ = std::fs::remove_dir_all(&tmp);
    Ok(())
}

/// The fitted size and random pixel colors of an image, as JSON for excel_check.ps1.
///
/// Rows and columns are 1-based, as in a spreadsheet. The first two samples are the
/// corners at the top left and at the bottom right.
pub fn sample_pixels(
    src: &Path,
    max_side: usize,
    background: &str,
    count: usize,
    seed: u64,
) -> Result<String, String> {
    let options = Options {
        max_side,
        background: parse_background(background).map_err(|e| e.to_string())?,
        compact: false,
    };
    let (_, im) = prepare(src, &options).map_err(|e| e.to_string())?;
    let mut rng = Random::new(seed);
    let mut spots = vec![(0, 0), (im.w - 1, im.h - 1)];
    spots.extend((0..count.saturating_sub(2)).map(|_| (rng.below(im.w), rng.below(im.h))));
    let mut json = format!(
        "{{\"width\": {}, \"height\": {}, \"samples\": [",
        im.w, im.h
    );
    for (i, (x, y)) in spots.iter().enumerate() {
        let [r, g, b] = im.get(*x, *y);
        let sep = if i == 0 { "" } else { ", " };
        let _ = write!(
            json,
            "{sep}[{}, {}, \"{r:02X}{g:02X}{b:02X}\"]",
            y + 1,
            x + 1
        );
    }
    json.push_str("]}");
    Ok(json)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_synthetic_photo_has_many_colors() {
        let im = photo(400, 300, 1);
        let colors: std::collections::HashSet<_> = im.pixels().collect();
        assert!(colors.len() > 63_999, "{} colors", colors.len());
    }

    #[test]
    fn the_sample_of_pixels_starts_at_the_corners() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.png");
        flat(30, 20).save(&path).unwrap();
        let json = sample_pixels(&path, 1000, "white", 5, 0).unwrap();
        assert!(json.starts_with(
            "{\"width\": 30, \"height\": 20, \"samples\": [[1, 1, \"000000\"], [20, 30, "
        ));
        assert_eq!(json.matches('[').count(), 6);
    }
}
