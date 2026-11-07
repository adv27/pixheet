//! A port of `pixheet._image.fit`: the size cap and the search that the color counts guide.

use crate::resample::{Rgb, resize, resize_fir};
use crate::xlsx::{MAX_COLORS, MAX_COLUMNS, MAX_ROWS};

/// `math.isqrt(MAX_COLORS)`: a side that always fits MAX_COLORS.
const ALWAYS_FITS_SIDE: usize = 252;
const COUNT_CAP: usize = 4 * MAX_COLORS;
const GROWTH: f64 = 1.6;
const MIN_SLOPE: f64 = 0.2;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Resizer {
    /// The port of Pillow: the same pixels as the Python version.
    Pillow,
    /// fast_image_resize: SIMD, other pixels.
    Fir,
}

/// The number of probes of the last search, for the report.
pub static PROBES: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// The number of colors in `im` if it is at most `limit`, else None. As `getcolors(limit)`.
pub fn count_colors(im: &Rgb, limit: usize) -> Option<usize> {
    let mut seen = vec![0u64; 1 << 18]; // one bit for each 24-bit color: 2 MiB
    let mut count = 0;
    for p in im.data.chunks_exact(3) {
        let key = (usize::from(p[0]) << 16) | (usize::from(p[1]) << 8) | usize::from(p[2]);
        let (word, bit) = (key >> 6, 1u64 << (key & 63));
        if seen[word] & bit == 0 {
            seen[word] |= bit;
            count += 1;
            if count > limit {
                return None;
            }
        }
    }
    Some(count)
}

fn color_count(im: &Rgb) -> usize {
    count_colors(im, COUNT_CAP.min(im.w * im.h)).unwrap_or(COUNT_CAP + 1)
}

/// `round()` of Python: halves go to the even integer.
fn py_round(v: f64) -> usize {
    v.round_ties_even() as usize
}

pub fn cap_size(w: usize, h: usize, max_side: usize) -> (usize, usize) {
    let side = |limit: usize| {
        if max_side == 0 {
            limit
        } else {
            limit.min(max_side)
        }
    };
    let (max_w, max_h) = (side(MAX_COLUMNS), side(MAX_ROWS));
    let scale = 1.0f64
        .min(max_w as f64 / w as f64)
        .min(max_h as f64 / h as f64);
    if scale == 1.0 {
        return (w, h);
    }
    (
        max_w.min(py_round(w as f64 * scale).max(1)),
        max_h.min(py_round(h as f64 * scale).max(1)),
    )
}

/// The size that `thumbnail((side, side))` of Pillow 13 gives, for side < max(w, h).
fn thumbnail_size(w: usize, h: usize, side: usize) -> (usize, usize) {
    let aspect = w as f64 / h as f64;
    let s = side as f64;
    if aspect <= 1.0 {
        let (lo, hi) = ((s * aspect).floor(), (s * aspect).ceil());
        let key = |n: f64| (aspect - n / s).abs();
        let x = if key(lo) <= key(hi) { lo } else { hi };
        return ((x as usize).max(1), side);
    }
    let (lo, hi) = ((s / aspect).floor(), (s / aspect).ceil());
    let key = |n: f64| {
        if n == 0.0 {
            0.0
        } else {
            (aspect - s / n).abs()
        }
    };
    let y = if key(lo) <= key(hi) { lo } else { hi };
    (side, (y as usize).max(1))
}

fn thumbnail(im: &Rgb, side: usize, resizer: Resizer) -> Rgb {
    let size = if side < im.w.max(im.h) {
        thumbnail_size(im.w, im.h, side)
    } else {
        (im.w, im.h)
    };
    match resizer {
        Resizer::Pillow => resize(im, size.0, size.1, Some(2.0)),
        Resizer::Fir => resize_fir(im, size.0, size.1),
    }
}

/// `_largest_passing`: the largest side in `lo..hi` whose image has at most MAX_COLORS.
fn largest_passing(
    mut lo: i64,
    mut hi: i64,
    mut attempt: impl FnMut(i64) -> (Rgb, usize),
    first: i64,
) -> Option<Rgb> {
    let mut best = None;
    let mut passes: Vec<(f64, f64)> = Vec::new();
    let mut fails: Vec<(f64, f64)> = Vec::new();
    let mut widths = vec![hi - lo];
    let mut side = first;
    let mut last_passed: Option<bool> = None;
    let mut streak = 0u32;
    while hi - lo > 1 {
        let n = widths.len();
        let stalled =
            !passes.is_empty() && !fails.is_empty() && n > 4 && widths[n - 1] * 2 > widths[n - 5];
        if !(lo < side && side < hi) || stalled {
            side = (lo + hi).div_euclid(2);
            widths = vec![hi - lo];
        }
        let (image, colors) = attempt(side);
        let passed = colors <= MAX_COLORS;
        let point = ((side as f64).ln(), (colors as f64 / MAX_COLORS as f64).ln());
        if passed {
            best = Some(image);
            lo = side;
            passes.push(point);
        } else {
            hi = side;
            fails.push(point);
        }
        widths.push(hi - lo);
        streak = if Some(passed) == last_passed {
            streak + 1
        } else {
            1
        };
        last_passed = Some(passed);

        let (x0, f0, x1, f1);
        if !passes.is_empty() && !fails.is_empty() {
            (x0, f0) = *passes.last().unwrap();
            (x1, f1) = *fails.last().unwrap();
        } else {
            let known = if passes.is_empty() { &fails } else { &passes };
            let (a, b) = if known.len() > 1 {
                (known[known.len() - 2], known[known.len() - 1])
            } else {
                (known[0], known[0])
            };
            let slope = if b.0 != a.0 {
                (b.1 - a.1) / (b.0 - a.0)
            } else {
                0.0
            };
            (x0, f0) = b;
            x1 = b.0 + 1.0;
            f1 = f0 + if slope > MIN_SLOPE { slope } else { GROWTH };
        }
        let estimate = (x0 - f0 * (x1 - x0) / (f1 - f0)).exp().floor() as i64;
        let step = 1i64 << (streak - 1).min(62);
        side = if passed {
            (estimate + 1).max(lo + step)
        } else {
            estimate.min(hi - step)
        };
    }
    best
}

/// Shrink `im` until it fits the sheet grid, `max_side` and MAX_COLORS.
pub fn fit(im: Rgb, max_side: usize, resizer: Resizer) -> Rgb {
    PROBES.store(0, std::sync::atomic::Ordering::Relaxed);
    let (w, h) = cap_size(im.w, im.h, max_side);
    let im = if (w, h) != (im.w, im.h) {
        match resizer {
            Resizer::Pillow => resize(&im, w, h, None),
            Resizer::Fir => resize_fir(&im, w, h),
        }
    } else {
        im
    };
    if count_colors(&im, MAX_COLORS).is_some() {
        return im;
    }
    let attempt = |side: i64| {
        PROBES.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let candidate = thumbnail(&im, side as usize, resizer);
        let colors = color_count(&candidate);
        (candidate, colors)
    };
    let longest = w.max(h);
    let first = py_round(longest as f64 * (2.0 * MAX_COLORS as f64 / (w * h) as f64).sqrt());
    let best = largest_passing(
        ALWAYS_FITS_SIDE as i64,
        longest as i64,
        attempt,
        first as i64,
    );
    best.unwrap_or_else(|| thumbnail(&im, ALWAYS_FITS_SIDE, resizer))
}
