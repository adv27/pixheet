//! A port of the LANCZOS resize and of the reduce of Pillow 13 for 8-bit RGB images.
//!
//! The code follows `libImaging/Resample.c` and `libImaging/Reduce.c`. It gives the
//! same bytes as `Image.resize(size, LANCZOS, box, reducing_gap)` for mode RGB. Each
//! pass runs on the rayon pool, one strip of output rows for each job.

use rayon::prelude::*;

/// An 8-bit RGB image, 3 bytes for each pixel, rows from the top.
#[derive(Clone)]
pub struct Rgb {
    pub w: usize,
    pub h: usize,
    pub data: Vec<u8>,
}

impl Rgb {
    pub fn new(w: usize, h: usize) -> Self {
        Self {
            w,
            h,
            data: vec![0; w * h * 3],
        }
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

/// The horizontal pass: `rows` output rows from input rows `offset..offset + rows`.
fn horizontal(src: &Rgb, out_w: usize, rows: usize, offset: usize, c: &Coeffs) -> Rgb {
    let mut out = Rgb::new(out_w, rows);
    let in_stride = src.w * 3;
    let out_stride = out_w * 3;
    let strip = rows.div_ceil(rayon::current_num_threads() * 4).max(1);
    out.data
        .par_chunks_mut(out_stride * strip)
        .enumerate()
        .for_each(|(si, chunk)| {
            for (ri, line_out) in chunk.chunks_exact_mut(out_stride).enumerate() {
                let yy = si * strip + ri + offset;
                let line_in = &src.data[yy * in_stride..(yy + 1) * in_stride];
                for (xx, px) in line_out.chunks_exact_mut(3).enumerate() {
                    let (xmin, xmax) = c.bounds[xx];
                    let k = &c.kk[xx * c.ksize..xx * c.ksize + xmax];
                    let input = &line_in[xmin * 3..(xmin + xmax) * 3];
                    let half = 1i32 << (PRECISION_BITS - 1);
                    let (mut s0, mut s1, mut s2) = (half, half, half);
                    for (p, &w) in input.chunks_exact(3).zip(k) {
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
fn vertical(src: &Rgb, out_h: usize, c: &Coeffs) -> Rgb {
    let mut out = Rgb::new(src.w, out_h);
    let stride = src.w * 3;
    let strip = out_h.div_ceil(rayon::current_num_threads() * 4).max(1);
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
fn resample(src: &Rgb, xsize: usize, ysize: usize, bx: [f32; 4]) -> Rgb {
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
        if horizontal_first {
            let v = vert.as_mut().unwrap();
            ybox_first = v.bounds[0].0;
            let last = v.bounds[ysize - 1];
            ybox_last = last.0 + last.1;
            for b in &mut v.bounds {
                b.0 -= ybox_first;
            }
        }
        horiz = Some(precompute_coeffs(src.w, bx[0], bx[2], xsize));
    }
    let mut cur: Option<Rgb> = None;
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
        if need_v {
            let input = cur.as_ref().unwrap_or(src);
            cur = Some(vertical(input, ysize, vert.as_ref().unwrap()));
        }
    } else {
        if need_v {
            cur = Some(vertical(src, ysize, vert.as_ref().unwrap()));
        }
        if let Some(h) = &horiz {
            let input = cur.as_ref().unwrap_or(src);
            cur = Some(horizontal(input, xsize, ysize, 0, h));
        }
    }
    cur.unwrap_or_else(|| src.clone())
}

/// `division_UINT32(divider, 8)` of Reduce.c: the division is in float.
fn reduce_multiplier(n: u32) -> u32 {
    (4_294_967_296.0f32 / (256 * n) as f32) as u32
}

/// `Image.reduce((fx, fy))` for the full image. Each output pixel is the mean of its
/// block. The last column and row can have smaller blocks.
fn reduce(src: &Rgb, fx: usize, fy: usize) -> Rgb {
    let ow = (src.w - 1) / fx + 1;
    let oh = (src.h - 1) / fy + 1;
    let mut out = Rgb::new(ow, oh);
    let stride = src.w * 3;
    out.data
        .par_chunks_mut(ow * 3)
        .enumerate()
        .for_each(|(oy, line_out)| {
            let y0 = oy * fy;
            let y1 = (y0 + fy).min(src.h);
            for (ox, px) in line_out.chunks_exact_mut(3).enumerate() {
                let x0 = ox * fx;
                let x1 = (x0 + fx).min(src.w);
                let n = ((x1 - x0) * (y1 - y0)) as u32;
                let (amend, mult) = (n / 2, reduce_multiplier(n));
                let mut s = [amend; 3];
                for y in y0..y1 {
                    let row = &src.data[y * stride + x0 * 3..y * stride + x1 * 3];
                    for p in row.chunks_exact(3) {
                        s[0] += u32::from(p[0]);
                        s[1] += u32::from(p[1]);
                        s[2] += u32::from(p[2]);
                    }
                }
                for c in 0..3 {
                    px[c] = (s[c].wrapping_mul(mult) >> 24) as u8;
                }
            }
        });
    out
}

/// `Image.resize(size, LANCZOS, reducing_gap=gap)` for the full image.
pub fn resize(src: &Rgb, w: usize, h: usize, reducing_gap: Option<f64>) -> Rgb {
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

/// The same resize with `fast_image_resize` (SIMD). Its pixels can differ from Pillow.
pub fn resize_fir(src: &Rgb, w: usize, h: usize) -> Rgb {
    use fast_image_resize as fr;
    if (w, h) == (src.w, src.h) {
        return src.clone();
    }
    let input =
        fr::images::ImageRef::new(src.w as u32, src.h as u32, &src.data, fr::PixelType::U8x3)
            .expect("valid image");
    let mut out = fr::images::Image::new(w as u32, h as u32, fr::PixelType::U8x3);
    let options =
        fr::ResizeOptions::new().resize_alg(fr::ResizeAlg::Convolution(fr::FilterType::Lanczos3));
    fr::Resizer::new()
        .resize(&input, &mut out, &options)
        .expect("resize");
    Rgb {
        w,
        h,
        data: out.into_vec(),
    }
}
