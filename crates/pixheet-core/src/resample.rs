//! A port of the LANCZOS resize and of the reduce of Pillow 13 for 8-bit RGB images.
//!
//! The code follows `libImaging/Resample.c` and `libImaging/Reduce.c`. It gives the
//! same bytes as `Image.resize(size, LANCZOS, box, reducing_gap)` for mode RGB. Each
//! pass runs on the rayon pool, one strip of output rows for each job.

use rayon::prelude::*;

/// An 8-bit RGB image: 3 bytes for each pixel, rows from the top.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pixels {
    /// The width in pixels.
    pub w: usize,
    /// The height in pixels.
    pub h: usize,
    /// `w * h * 3` bytes: R, G, B for each pixel.
    pub data: Vec<u8>,
}

impl Pixels {
    /// A black image.
    pub fn new(w: usize, h: usize) -> Self {
        Self {
            w,
            h,
            data: vec![0; w * h * 3],
        }
    }

    /// The color of the pixel at (x, y).
    pub fn get(&self, x: usize, y: usize) -> [u8; 3] {
        let i = (y * self.w + x) * 3;
        [self.data[i], self.data[i + 1], self.data[i + 2]]
    }
}

const PRECISION_BITS: u32 = 32 - 8 - 2;
const LANCZOS_SUPPORT: f64 = 3.0;

fn sinc(x: f64) -> f64 {
    if x == 0.0 {
        return 1.0;
    }
    let x = x * std::f64::consts::PI;
    x.sin() / x
}

fn lanczos(x: f64) -> f64 {
    if (-3.0..3.0).contains(&x) {
        sinc(x) * sinc(x / 3.0)
    } else {
        0.0
    }
}

/// The coefficients of one pass: for each output pixel, the first input pixel, the
/// number of taps, and `ksize` fixed-point weights.
struct Coeffs {
    ksize: usize,
    bounds: Vec<(usize, usize)>,
    kk: Vec<i32>,
}

fn precompute_coeffs(in_size: usize, in0: f32, in1: f32, out_size: usize) -> Coeffs {
    // (double)(in1 - in0): the subtraction is in float, as in C.
    let scale = f64::from(in1 - in0) / out_size as f64;
    let filterscale = scale.max(1.0);
    let support = LANCZOS_SUPPORT * filterscale;
    let ksize = support.ceil() as usize * 2 + 1;
    let inv_filterscale = 1.0 / filterscale;
    let mut bounds = Vec::with_capacity(out_size);
    let mut kk = vec![0i32; out_size * ksize];
    let mut k = vec![0f64; ksize];
    for xx in 0..out_size {
        let center = f64::from(in0) + (xx as f64 + 0.5) * scale;
        // C casts to int: the cast truncates toward zero.
        let xmin = ((center - support + 0.5) as i64).max(0);
        let xmax = ((center + support + 0.5) as i64).min(in_size as i64) - xmin;
        let xmax = xmax.max(0) as usize;
        let mut ww = 0.0;
        for (x, slot) in k.iter_mut().enumerate().take(xmax) {
            let w = lanczos(((x as i64 + xmin) as f64 - center + 0.5) * inv_filterscale);
            *slot = w;
            ww += w;
        }
        for (x, out) in kk[xx * ksize..(xx + 1) * ksize].iter_mut().enumerate() {
            let mut w = if x < xmax { k[x] } else { 0.0 };
            if x < xmax && ww != 0.0 {
                w /= ww;
            }
            let scaled = w * f64::from(1u32 << PRECISION_BITS);
            *out = if w < 0.0 {
                (-0.5 + scaled) as i32
            } else {
                (0.5 + scaled) as i32
            };
        }
        bounds.push((xmin as usize, xmax));
    }
    Coeffs { ksize, bounds, kk }
}

#[inline]
fn clip8(v: i32) -> u8 {
    (v >> PRECISION_BITS).clamp(0, 255) as u8
}

/// The number of output rows in each job of a pass.
fn strip_rows(rows: usize) -> usize {
    rows.div_ceil(rayon::current_num_threads() * 4).max(1)
}

/// The number of rows that the horizontal pass makes together.
const BLOCK: usize = 16;

/// The horizontal pass: `rows` output rows from input rows `offset..offset + rows`.
///
/// The pass makes BLOCK rows together. It copies the block into columns first: column
/// `x` holds pixel `x` of each row of the block, `LANES` bytes. A block with fewer rows
/// gets zeros in the free places. Then each tap of the filter is one multiply-add of
/// `LANES` values with a fixed length, which the compiler keeps in vector registers. The
/// sums are integers, so the new order of the additions gives the same bytes as Pillow.
fn horizontal(src: &Pixels, out_w: usize, rows: usize, offset: usize, c: &Coeffs) -> Pixels {
    const LANES: usize = BLOCK * 3;
    let mut out = Pixels::new(out_w, rows);
    let in_stride = src.w * 3;
    let out_stride = out_w * 3;
    let strip = strip_rows(rows).next_multiple_of(BLOCK);
    let half = 1i32 << (PRECISION_BITS - 1);
    out.data
        .par_chunks_mut(out_stride * strip)
        .enumerate()
        .for_each(|(si, chunk)| {
            let mut columns = vec![[0u8; LANES]; src.w];
            for (bi, block) in chunk.chunks_mut(out_stride * BLOCK).enumerate() {
                let n = block.len() / out_stride;
                let y0 = si * strip + bi * BLOCK + offset;
                for r in 0..n {
                    let line = &src.data[(y0 + r) * in_stride..(y0 + r + 1) * in_stride];
                    for (column, p) in columns.iter_mut().zip(line.as_chunks::<3>().0) {
                        column[r * 3..r * 3 + 3].copy_from_slice(p);
                    }
                }
                for xx in 0..out_w {
                    let (xmin, xmax) = c.bounds[xx];
                    let k = &c.kk[xx * c.ksize..xx * c.ksize + xmax];
                    let mut acc = [half; LANES];
                    for (column, &w) in columns[xmin..xmin + xmax].iter().zip(k) {
                        for (a, &p) in acc.iter_mut().zip(column) {
                            *a = a.wrapping_add(i32::from(p).wrapping_mul(w));
                        }
                    }
                    for r in 0..n {
                        let o = r * out_stride + xx * 3;
                        block[o] = clip8(acc[r * 3]);
                        block[o + 1] = clip8(acc[r * 3 + 1]);
                        block[o + 2] = clip8(acc[r * 3 + 2]);
                    }
                }
            }
        });
    out
}

/// The horizontal pass of Pillow, one pixel at a time: the reference for the tests.
#[cfg(test)]
fn horizontal_reference(
    src: &Pixels,
    out_w: usize,
    rows: usize,
    offset: usize,
    c: &Coeffs,
) -> Pixels {
    let mut out = Pixels::new(out_w, rows);
    let in_stride = src.w * 3;
    let out_stride = out_w * 3;
    let strip = strip_rows(rows);
    out.data
        .par_chunks_mut(out_stride * strip)
        .enumerate()
        .for_each(|(si, chunk)| {
            for (ri, line_out) in chunk.chunks_exact_mut(out_stride).enumerate() {
                let yy = si * strip + ri + offset;
                let line_in = &src.data[yy * in_stride..(yy + 1) * in_stride];
                for (xx, px) in line_out.as_chunks_mut::<3>().0.iter_mut().enumerate() {
                    let (xmin, xmax) = c.bounds[xx];
                    let k = &c.kk[xx * c.ksize..xx * c.ksize + xmax];
                    let input = &line_in[xmin * 3..(xmin + xmax) * 3];
                    let half = 1i32 << (PRECISION_BITS - 1);
                    let (mut s0, mut s1, mut s2) = (half, half, half);
                    for (p, &w) in input.as_chunks::<3>().0.iter().zip(k) {
                        s0 = s0.wrapping_add(i32::from(p[0]).wrapping_mul(w));
                        s1 = s1.wrapping_add(i32::from(p[1]).wrapping_mul(w));
                        s2 = s2.wrapping_add(i32::from(p[2]).wrapping_mul(w));
                    }
                    px[0] = clip8(s0);
                    px[1] = clip8(s1);
                    px[2] = clip8(s2);
                }
            }
        });
    out
}

/// The vertical pass: `out_h` output rows, with the same width as `src`.
fn vertical(src: &Pixels, out_h: usize, c: &Coeffs) -> Pixels {
    let mut out = Pixels::new(src.w, out_h);
    let stride = src.w * 3;
    let strip = strip_rows(out_h);
    out.data
        .par_chunks_mut(stride * strip)
        .enumerate()
        .for_each(|(si, chunk)| {
            let mut acc = vec![0i32; stride];
            for (ri, line_out) in chunk.chunks_exact_mut(stride).enumerate() {
                let yy = si * strip + ri;
                let (ymin, ymax) = c.bounds[yy];
                let k = &c.kk[yy * c.ksize..yy * c.ksize + ymax];
                acc.fill(1 << (PRECISION_BITS - 1));
                // Add one input row at a time: the compiler vectorizes this loop.
                for (y, &w) in k.iter().enumerate() {
                    let line_in = &src.data[(ymin + y) * stride..(ymin + y + 1) * stride];
                    for (a, &p) in acc.iter_mut().zip(line_in) {
                        *a = a.wrapping_add(i32::from(p).wrapping_mul(w));
                    }
                }
                for (o, &a) in line_out.iter_mut().zip(&acc) {
                    *o = clip8(a);
                }
            }
        });
    out
}

/// `ImagingResampleInner` with the LANCZOS filter.
fn resample(src: &Pixels, xsize: usize, ysize: usize, bx: [f32; 4]) -> Pixels {
    let need_h = xsize != src.w || bx[0] != 0.0 || bx[2] != xsize as f32;
    let need_v = ysize != src.h || bx[1] != 0.0 || bx[3] != ysize as f32;
    let dy = src.h as i64 - ysize as i64;
    let dx = src.w as i64 - xsize as i64;
    let horizontal_first = !(dy > 0 && dy > dx * 2);

    let mut vert = None;
    if (need_h && horizontal_first) || need_v {
        vert = Some(precompute_coeffs(src.h, bx[1], bx[3], ysize));
    }
    let mut horiz = None;
    let (mut ybox_first, mut ybox_last) = (0, src.h);
    if need_h {
        if let (true, Some(v)) = (horizontal_first, vert.as_mut()) {
            ybox_first = v.bounds[0].0;
            let last = v.bounds[ysize - 1];
            ybox_last = last.0 + last.1;
            for b in &mut v.bounds {
                b.0 -= ybox_first;
            }
        }
        horiz = Some(precompute_coeffs(src.w, bx[0], bx[2], xsize));
    }
    let mut cur: Option<Pixels> = None;
    if horizontal_first {
        if let Some(h) = &horiz {
            cur = Some(horizontal(
                src,
                xsize,
                ybox_last - ybox_first,
                ybox_first,
                h,
            ));
        }
        if let (true, Some(v)) = (need_v, &vert) {
            cur = Some(vertical(cur.as_ref().unwrap_or(src), ysize, v));
        }
    } else {
        if let (true, Some(v)) = (need_v, &vert) {
            cur = Some(vertical(src, ysize, v));
        }
        if let Some(h) = &horiz {
            cur = Some(horizontal(cur.as_ref().unwrap_or(src), xsize, ysize, 0, h));
        }
    }
    cur.unwrap_or_else(|| src.clone())
}

/// `division_UINT32(divider, 8)` of Reduce.c. The division is in float, as in C.
fn reduce_multiplier(n: u32) -> u32 {
    (4_294_967_296.0f32 / (256 * n) as f32) as u32
}

/// `Image.reduce((fx, fy))` for the full image. Each output pixel is the mean of its
/// block. The last column and the last row can have smaller blocks.
fn reduce(src: &Pixels, fx: usize, fy: usize) -> Pixels {
    let ow = (src.w - 1) / fx + 1;
    let oh = (src.h - 1) / fy + 1;
    let mut out = Pixels::new(ow, oh);
    let stride = src.w * 3;
    out.data
        .par_chunks_mut(ow * 3)
        .enumerate()
        .for_each(|(oy, line_out)| {
            let y0 = oy * fy;
            let y1 = (y0 + fy).min(src.h);
            for (ox, px) in line_out.as_chunks_mut::<3>().0.iter_mut().enumerate() {
                let x0 = ox * fx;
                let x1 = (x0 + fx).min(src.w);
                let n = ((x1 - x0) * (y1 - y0)) as u32;
                let (amend, mult) = (n / 2, reduce_multiplier(n));
                let mut s = [amend; 3];
                for y in y0..y1 {
                    let row = &src.data[y * stride + x0 * 3..y * stride + x1 * 3];
                    for p in row.as_chunks::<3>().0 {
                        s[0] += u32::from(p[0]);
                        s[1] += u32::from(p[1]);
                        s[2] += u32::from(p[2]);
                    }
                }
                for (o, sum) in px.iter_mut().zip(s) {
                    *o = (sum.wrapping_mul(mult) >> 24) as u8;
                }
            }
        });
    out
}

/// `Image.resize((w, h), LANCZOS, reducing_gap=gap)` for the full image.
pub fn resize(src: &Pixels, w: usize, h: usize, reducing_gap: Option<f64>) -> Pixels {
    if (w, h) == (src.w, src.h) {
        return src.clone();
    }
    if let Some(gap) = reducing_gap {
        // Python: int(...) or 1.
        let fx = ((src.w as f64 / w as f64 / gap) as usize).max(1);
        let fy = ((src.h as f64 / h as f64 / gap) as usize).max(1);
        if fx > 1 || fy > 1 {
            // For the full box, _get_safe_box gives the full image.
            let reduced = reduce(src, fx, fy);
            let bx = [
                0.0,
                0.0,
                (src.w as f64 / fx as f64) as f32,
                (src.h as f64 / fy as f64) as f32,
            ];
            return resample(&reduced, w, h, bx);
        }
    }
    resample(src, w, h, [0.0, 0.0, src.w as f32, src.h as f32])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(w: usize, h: usize) -> Pixels {
        let mut im = Pixels::new(w, h);
        for (i, v) in im.data.iter_mut().enumerate() {
            *v = (i * 7 % 256) as u8;
        }
        im
    }

    #[test]
    fn the_block_pass_gives_the_bytes_of_the_reference() {
        // Odd sizes give blocks that are not full and strips of other lengths.
        for (w, h, out_w) in [(37, 23, 11), (100, 50, 33), (5, 40, 9), (300, 17, 97)] {
            let im = ramp(w, h);
            let c = precompute_coeffs(w, 0.0, w as f32, out_w);
            for (rows, offset) in [(h, 0), (h - 3, 2)] {
                let a = horizontal(&im, out_w, rows, offset, &c);
                let b = horizontal_reference(&im, out_w, rows, offset, &c);
                assert_eq!(a, b, "{w}x{h} -> {out_w}, {rows} rows from {offset}");
            }
        }
    }

    #[test]
    fn same_size_is_a_copy() {
        let im = ramp(5, 4);
        assert_eq!(resize(&im, 5, 4, None), im);
    }

    #[test]
    fn a_flat_image_stays_flat() {
        let mut im = Pixels::new(40, 30);
        for p in im.data.as_chunks_mut::<3>().0 {
            p.copy_from_slice(&[10, 200, 99]);
        }
        for (w, h, gap) in [(13, 9, None), (5, 4, Some(2.0)), (80, 61, None)] {
            let out = resize(&im, w, h, gap);
            assert_eq!((out.w, out.h), (w, h));
            assert!(
                out.data
                    .as_chunks::<3>()
                    .0
                    .iter()
                    .all(|p| *p == [10, 200, 99])
            );
        }
    }

    #[test]
    fn a_tall_image_resizes_vertically_first_as_pillow() {
        // The height shrinks more than 2 times as much as the width, so Pillow resizes the
        // columns first. The values come from Image.resize((3, 10), LANCZOS) of Pillow 13.
        let (w, h) = (4, 40);
        let mut im = Pixels::new(w, h);
        for (i, p) in im.data.as_chunks_mut::<3>().0.iter_mut().enumerate() {
            let (x, y) = (i % w, i / w);
            *p = [
                ((x * 37 + y * 11) % 256) as u8,
                ((x * 5 + y * 29) % 256) as u8,
                ((x * 91 + y * 3) % 256) as u8,
            ];
        }
        let expected = [
            23, 42, 15, 73, 49, 158, 122, 55, 70, 66, 170, 26, 116, 177, 169, 161, 183, 81, 111,
            82, 39, 158, 89, 182, 224, 95, 94, 151, 142, 52, 221, 149, 193, 160, 155, 105, 214,
            125, 64, 149, 132, 204, 26, 137, 117, 175, 107, 75, 22, 113, 222, 86, 125, 132, 41,
            165, 109, 78, 175, 122, 131, 130, 76, 71, 80, 129, 124, 93, 89, 172, 99, 60, 120, 193,
            140, 168, 170, 111, 223, 150, 79, 159, 85, 152, 223, 74, 121, 121, 62, 89,
        ];
        assert_eq!(resize(&im, 3, 10, None).data, expected);
    }

    #[test]
    fn reduce_rounds_as_pillow() {
        // The values come from Image.reduce((3, 1)) of Pillow 13. The multiplier is a
        // little too small, so a mean of 2/3 gives 0, and a mean of 254.67 gives 254.
        let im = Pixels {
            w: 3,
            h: 1,
            data: vec![0, 1, 1, 0, 1, 1, 1, 0, 0],
        };
        assert_eq!(reduce(&im, 3, 1).data, vec![0, 0, 0]);
        let im = Pixels {
            w: 3,
            h: 1,
            data: vec![0, 2, 255, 0, 2, 255, 1, 2, 254],
        };
        assert_eq!(reduce(&im, 3, 1).data, vec![0, 2, 254]);
        assert_eq!(reduce_multiplier(4), 1 << 22);
    }
}
