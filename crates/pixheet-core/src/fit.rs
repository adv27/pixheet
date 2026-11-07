//! A port of `pixheet._image.fit`: the size cap and the search that the color counts guide.

use crate::error::Error;
use crate::resample::{Pixels, resize};
use crate::xlsx::{MAX_COLORS, MAX_COLUMNS, MAX_ROWS};

/// `math.isqrt(MAX_COLORS)`: an image with this longest side always fits MAX_COLORS.
const ALWAYS_FITS_SIDE: usize = 252;
/// The color count of a probe is exact up to this value.
const COUNT_CAP: usize = 4 * MAX_COLORS;
/// With one tested end and no slope above MIN_SLOPE, the search guesses that colors
/// grow as side ** GROWTH.
const GROWTH: f64 = 1.6;
const MIN_SLOPE: f64 = 0.2;

/// The number of colors in `im` if it is at most `limit`, else None. As `getcolors(limit)`.
pub fn count_colors(im: &Pixels, limit: usize) -> Option<usize> {
    let mut seen = vec![0u64; 1 << 18]; // one bit for each 24-bit color: 2 MiB
    let mut count = 0;
    for p in im.data.as_chunks::<3>().0 {
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

/// The number of colors: exact up to COUNT_CAP, COUNT_CAP + 1 above it.
fn color_count(im: &Pixels) -> usize {
    count_colors(im, COUNT_CAP.min(im.w * im.h)).unwrap_or(COUNT_CAP + 1)
}

/// `round()` of Python: a half goes to the even integer.
fn py_round(v: f64) -> usize {
    v.round_ties_even() as usize
}

/// The largest size with the same aspect ratio that fits `max_side` and the sheet grid.
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
pub fn thumbnail_size(w: usize, h: usize, side: usize) -> (usize, usize) {
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

/// A copy of `im` after `thumbnail((side, side), LANCZOS)`, with `reducing_gap=2.0`.
fn thumbnail(im: &Pixels, side: usize) -> Pixels {
    let (w, h) = if side < im.w.max(im.h) {
        thumbnail_size(im.w, im.h, side)
    } else {
        (im.w, im.h)
    };
    resize(im, w, h, Some(2.0))
}

/// `_largest_passing`: the largest side in `lo..hi` whose image has at most MAX_COLORS.
///
/// `lo` passes and `hi` fails without a test. Read the docstring of the Python function
/// for the method. The result is the image of the largest side that passed.
fn largest_passing(
    mut lo: i64,
    mut hi: i64,
    mut attempt: impl FnMut(i64) -> (Pixels, usize),
    first: i64,
) -> Option<Pixels> {
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
        if let (Some(&p), Some(&f)) = (passes.last(), fails.last()) {
            (x0, f0) = p;
            (x1, f1) = f;
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

/// Shrink an RGB image until it fits the sheet grid, `max_side` and MAX_COLORS.
///
/// First one LANCZOS resize caps the size. Then a search that the color counts guide
/// finds the largest longest side, to the pixel, whose color count is within MAX_COLORS.
pub fn fit(im: Pixels, max_side: usize) -> Result<Pixels, Error> {
    if im.w == 0 || im.h == 0 {
        return Err(Error::Invalid(format!(
            "the image has no pixels ({}x{})",
            im.w, im.h
        )));
    }
    let (w, h) = cap_size(im.w, im.h, max_side);
    let im = if (w, h) != (im.w, im.h) {
        resize(&im, w, h, None)
    } else {
        im
    };
    if count_colors(&im, MAX_COLORS).is_some() {
        return Ok(im);
    }
    let attempt = |side: i64| {
        let candidate = thumbnail(&im, side as usize);
        let colors = color_count(&candidate);
        (candidate, colors)
    };
    // Start at the side that gives about 2 * MAX_COLORS pixels: most photos fit there.
    let longest = w.max(h);
    let first = py_round(longest as f64 * (2.0 * MAX_COLORS as f64 / (w * h) as f64).sqrt());
    let best = largest_passing(
        ALWAYS_FITS_SIDE as i64,
        longest as i64,
        attempt,
        first as i64,
    );
    Ok(best.unwrap_or_else(|| thumbnail(&im, ALWAYS_FITS_SIDE)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cap_size_keeps_small_images() {
        assert_eq!(cap_size(640, 480, 1000), (640, 480));
        assert_eq!(cap_size(640, 480, 0), (640, 480));
    }

    #[test]
    fn cap_size_rounds_half_to_even() {
        // 1000 * 0.5 = 500 and 5 * 0.5 = 2.5: Python round() gives 2.
        assert_eq!(cap_size(2000, 5, 1000), (1000, 2));
        assert_eq!(cap_size(2000, 7, 1000), (1000, 4));
    }

    #[test]
    fn cap_size_obeys_the_sheet_grid() {
        assert_eq!(cap_size(20_000, 100, 0), (MAX_COLUMNS, 82));
        assert_eq!(cap_size(10, 2_000_000, 0), (5, MAX_ROWS));
        assert_eq!(cap_size(100_000, 1, 0), (MAX_COLUMNS, 1));
    }

    #[test]
    fn thumbnail_size_matches_pillow() {
        // The values come from Image.thumbnail of Pillow 13.
        assert_eq!(thumbnail_size(1000, 750, 297), (297, 223));
        assert_eq!(thumbnail_size(750, 1000, 297), (223, 297));
        assert_eq!(thumbnail_size(1000, 3, 252), (252, 1));
        assert_eq!(thumbnail_size(3, 1000, 252), (1, 252));
    }

    #[test]
    fn count_colors_stops_above_the_limit() {
        let mut im = Pixels::new(4, 1);
        im.data = vec![1, 0, 0, 2, 0, 0, 3, 0, 0, 1, 0, 0];
        assert_eq!(count_colors(&im, 3), Some(3));
        assert_eq!(count_colors(&im, 2), None);
    }

    #[test]
    fn an_image_without_pixels_fails() {
        let err = fit(Pixels::new(0, 3), 1000).unwrap_err();
        assert_eq!(err.to_string(), "the image has no pixels (0x3)");
    }

    #[test]
    fn many_colors_shrink_below_the_limit() {
        // 256x256 with R = x and G = y: 65,536 colors, more than a sheet allows.
        let mut im = Pixels::new(256, 256);
        for y in 0..256 {
            for x in 0..256 {
                let i = (y * 256 + x) * 3;
                im.data[i] = x as u8;
                im.data[i + 1] = y as u8;
            }
        }
        let out = fit(im, 1000).unwrap();
        // pixheet.prepare(many_colors()) gives the same size.
        assert_eq!((out.w, out.h), (252, 252));
        assert!(count_colors(&out, MAX_COLORS).is_some());
    }
}
