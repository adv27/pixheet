//! The preview of a card: a small copy of the fitted image, as a data URL.

use base64::Engine;
use pixheet_core::Pixels;

/// The longest side of a preview, in pixels. A card is at most about 400 CSS pixels wide, and
/// the preview must stay sharp on a display with a scale above 100 %.
const PREVIEW_SIDE: usize = 512;

/// The largest PNG preview, in bytes. A larger preview, for example a photo, goes as a JPEG.
const PNG_BUDGET: usize = 96 * 1024;

/// The quality of a JPEG preview.
const JPEG_QUALITY: u8 = 88;

/// A copy of `image` for the card, as a PNG or JPEG data URL.
///
/// The longest side of the copy is near [`PREVIEW_SIDE`], so the page does not scale it up
/// much. A large image shrinks with the Catmull-Rom filter: thin lines and text stay clear
/// at each ratio. A small image grows by a whole number with the nearest pixel, so each cell stays
/// a sharp square.
pub fn preview_url(image: &Pixels) -> Option<String> {
    let preview = preview_pixels(image)?;
    let (w, h) = (preview.w as u32, preview.h as u32);
    let mut png = Vec::new();
    image::ImageEncoder::write_image(
        image::codecs::png::PngEncoder::new_with_quality(
            &mut png,
            image::codecs::png::CompressionType::Fast,
            image::codecs::png::FilterType::Adaptive,
        ),
        &preview.data,
        w,
        h,
        image::ExtendedColorType::Rgb8,
    )
    .ok()?;
    let (mime, bytes) = if png.len() <= PNG_BUDGET {
        ("png", png)
    } else {
        let mut jpeg = Vec::new();
        image::ImageEncoder::write_image(
            image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY),
            &preview.data,
            w,
            h,
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
        ("jpeg", jpeg)
    };
    Some(format!(
        "data:image/{mime};base64,{}",
        base64::engine::general_purpose::STANDARD.encode(bytes)
    ))
}

/// The pixels of the preview of `image`. Read [`preview_url`].
fn preview_pixels(image: &Pixels) -> Option<Pixels> {
    let longest = image.w.max(image.h).max(1);
    if longest > PREVIEW_SIDE {
        let scale = PREVIEW_SIDE as f64 / longest as f64;
        let w = ((image.w as f64 * scale).round() as u32).max(1);
        let h = ((image.h as f64 * scale).round() as u32).max(1);
        let view = image::ImageBuffer::<image::Rgb<u8>, &[u8]>::from_raw(
            image.w as u32,
            image.h as u32,
            &image.data,
        )?;
        let small = image::imageops::resize(&view, w, h, image::imageops::FilterType::CatmullRom);
        return Some(Pixels {
            w: w as usize,
            h: h as usize,
            data: small.into_raw(),
        });
    }
    // Round, so that the copy is between 2/3 and 3/2 of PREVIEW_SIDE.
    let k = ((PREVIEW_SIDE as f64 / longest as f64).round() as usize).max(1);
    if k == 1 {
        return Some(image.clone());
    }
    let mut out = Pixels::new(image.w * k, image.h * k);
    for (y, row) in out.data.chunks_exact_mut(image.w * k * 3).enumerate() {
        let src = &image.data[(y / k) * image.w * 3..][..image.w * 3];
        for (cell, rgb) in row.chunks_exact_mut(k * 3).zip(src.as_chunks::<3>().0) {
            cell.as_chunks_mut::<3>().0.fill(*rgb);
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode(url: &str) -> image::RgbImage {
        let data = url.split_once(";base64,").unwrap().1;
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(data)
            .unwrap();
        image::load_from_memory(&bytes).unwrap().to_rgb8()
    }

    fn pixels(w: usize, h: usize, mut color: impl FnMut(usize, usize) -> [u8; 3]) -> Pixels {
        let mut image = Pixels::new(w, h);
        for y in 0..h {
            for x in 0..w {
                let i = (y * w + x) * 3;
                image.data[i..i + 3].copy_from_slice(&color(x, y));
            }
        }
        image
    }

    #[test]
    fn a_large_image_shrinks_without_aliasing() {
        // A checkerboard of 1-pixel squares: the nearest pixel gives black or white.
        let image = pixels(2048, 1536, |x, y| {
            [if (x + y) % 2 == 0 { 255 } else { 0 }; 3]
        });
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/png;base64,"));
        let preview = decode(&url);
        assert_eq!(preview.dimensions(), (512, 384));
        assert!(
            preview
                .pixels()
                .all(|p| p.0.iter().all(|&c| (120..=135).contains(&c)))
        );
    }

    #[test]
    fn thin_lines_stay_even_at_a_ratio_that_is_not_whole() {
        // Black and white rows of 1 pixel, shrunk by 1.4. A filter that takes each source
        // row for one target row gives rows of pure black or white.
        let image = pixels(717, 538, |_, y| [if y % 2 == 0 { 255 } else { 0 }; 3]);
        let preview = preview_pixels(&image).unwrap();
        assert_eq!((preview.w, preview.h), (512, 384));
        assert!(preview.data.iter().all(|&c| (64..=192).contains(&c)));
    }

    #[test]
    fn a_small_image_grows_with_sharp_cells() {
        let image = pixels(4, 3, |x, y| [(x * 60) as u8, (y * 80) as u8, 7]);
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/png;base64,"));
        let preview = decode(&url);
        assert_eq!(preview.dimensions(), (512, 384));
        for (x, y, p) in preview.enumerate_pixels() {
            assert_eq!(p.0, image.get(x as usize / 128, y as usize / 128));
        }
    }

    #[test]
    fn a_preview_stays_near_the_preview_side() {
        for (w, h, side) in [
            (300, 200, 600),
            (400, 100, 400),
            (342, 10, 342),
            (1, 1, 512),
        ] {
            let preview = preview_pixels(&pixels(w, h, |_, _| [1, 2, 3])).unwrap();
            assert_eq!(preview.w.max(preview.h), side, "{w} x {h}");
            assert_eq!(preview.w * h, preview.h * w);
        }
    }

    #[test]
    fn a_photo_goes_as_a_jpeg() {
        // Noise does not compress, so the PNG is larger than the budget.
        let mut seed = 1u32;
        let image = pixels(800, 600, |_, _| {
            seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let [a, b, c, _] = seed.to_le_bytes();
            [a, b, c]
        });
        let url = preview_url(&image).unwrap();
        assert!(url.starts_with("data:image/jpeg;base64,"));
        assert_eq!(decode(&url).dimensions(), (512, 384));
    }
}
