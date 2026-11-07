//! Open an image, make it upright, and make it 8-bit RGB with the background under alpha.
//!
//! This covers the common formats only. `pixheet._image.normalize` also handles cases
//! that this spike does not port: read the plan for the list.

use std::path::Path;

use image::{DynamicImage, ImageDecoder, ImageReader};

use crate::resample::Rgb;

pub fn open(path: &Path) -> Result<DynamicImage, String> {
    let reader = ImageReader::open(path)
        .and_then(|r| r.with_guessed_format())
        .map_err(|e| e.to_string())?;
    #[cfg(feature = "turbo")]
    if reader.format() == Some(image::ImageFormat::Jpeg) {
        return open_turbo(path);
    }
    let mut decoder = reader.into_decoder().map_err(|_| {
        format!(
            "cannot identify image file {:?}",
            path.display().to_string()
        )
    })?;
    let orientation = decoder.orientation().map_err(|e| e.to_string())?;
    let mut im = DynamicImage::from_decoder(decoder).map_err(|e| e.to_string())?;
    im.apply_orientation(orientation);
    Ok(im)
}

/// JPEG through libjpeg-turbo, with the default (slow, exact) IDCT and fancy upsampling,
/// as Pillow decodes it.
#[cfg(feature = "turbo")]
fn open_turbo(path: &Path) -> Result<DynamicImage, String> {
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let out =
        turbojpeg::decompress(&bytes, turbojpeg::PixelFormat::RGB).map_err(|e| e.to_string())?;
    let mut data = Vec::with_capacity(out.width * out.height * 3);
    for row in out.pixels.chunks(out.pitch).take(out.height) {
        data.extend_from_slice(&row[..out.width * 3]);
    }
    let rgb =
        image::RgbImage::from_raw(out.width as u32, out.height as u32, data).ok_or("bad size")?;
    let mut im = DynamicImage::ImageRgb8(rgb);
    // Read only the EXIF orientation with the decoder of the image crate.
    let decoder = image::codecs::jpeg::JpegDecoder::new(std::io::Cursor::new(&bytes));
    if let Ok(mut d) = decoder
        && let Ok(o) = d.orientation()
    {
        im.apply_orientation(o);
    }
    Ok(im)
}

/// `BLEND` of Pillow's Paste.c: `DIV255(bg * (255 - a) + c * a)`.
#[inline]
fn blend(a: u8, bg: u8, c: u8) -> u8 {
    let (a, bg, c) = (u32::from(a), u32::from(bg), u32::from(c));
    let t = bg * (255 - a) + c * a + 128;
    (((t >> 8) + t) >> 8) as u8
}

pub fn normalize(im: DynamicImage, background: Option<[u8; 3]>) -> Rgb {
    let (w, h) = (im.width() as usize, im.height() as usize);
    let data = match im {
        DynamicImage::ImageRgb8(buf) => buf.into_raw(),
        // Pillow keeps the top 8 bits of 16-bit samples.
        DynamicImage::ImageRgb16(buf) => buf.into_raw().iter().map(|v| (v >> 8) as u8).collect(),
        DynamicImage::ImageLuma16(buf) => buf
            .into_raw()
            .iter()
            .flat_map(|v| [(v >> 8) as u8; 3])
            .collect(),
        DynamicImage::ImageLuma8(buf) => buf.into_raw().iter().flat_map(|&v| [v; 3]).collect(),
        im if im.color().has_alpha() => {
            let rgba = match im {
                DynamicImage::ImageRgba16(buf) => buf
                    .into_raw()
                    .iter()
                    .map(|v| (v >> 8) as u8)
                    .collect::<Vec<u8>>(),
                DynamicImage::ImageLumaA16(buf) => buf
                    .into_raw()
                    .chunks_exact(2)
                    .flat_map(|p| {
                        let (l, a) = ((p[0] >> 8) as u8, (p[1] >> 8) as u8);
                        [l, l, l, a]
                    })
                    .collect(),
                other => other.to_rgba8().into_raw(),
            };
            match background {
                None => rgba
                    .chunks_exact(4)
                    .flat_map(|p| [p[0], p[1], p[2]])
                    .collect(),
                Some(bg) => rgba
                    .chunks_exact(4)
                    .flat_map(|p| {
                        [
                            blend(p[3], bg[0], p[0]),
                            blend(p[3], bg[1], p[1]),
                            blend(p[3], bg[2], p[2]),
                        ]
                    })
                    .collect(),
            }
        }
        other => other.to_rgb8().into_raw(),
    };
    Rgb { w, h, data }
}

pub fn parse_background(text: &str) -> Result<Option<[u8; 3]>, String> {
    let t = text.trim().to_ascii_lowercase();
    let named = match t.as_str() {
        "none" => return Ok(None),
        "white" => Some([255, 255, 255]),
        "black" => Some([0, 0, 0]),
        _ => None,
    };
    if named.is_some() {
        return Ok(named);
    }
    let hex = t
        .strip_prefix('#')
        .filter(|h| h.len() == 6 && h.bytes().all(|b| b.is_ascii_hexdigit()));
    match hex {
        Some(h) => {
            let v = u32::from_str_radix(h, 16).unwrap();
            Ok(Some([(v >> 16) as u8, (v >> 8) as u8, v as u8]))
        }
        None => Err(format!(
            "invalid background color {text:?}: use white, black, #RRGGBB or none (the spike has no CSS names)"
        )),
    }
}
