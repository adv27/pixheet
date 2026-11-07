//! Open an image, make it upright, and make it 8-bit RGB with the background under alpha.
//!
//! This is a port of `pixheet._image.opened` and `normalize`. The rules follow Pillow 13:
//!
//! - The EXIF orientation is applied.
//! - 16-bit samples keep their top 8 bits.
//! - A CMYK JPEG is inverted ("CMYK;I", the Adobe convention), then converted with the
//!   formula of Pillow.
//! - Alpha is blended onto the background with the arithmetic of `Image.paste`.

use std::io::Cursor;
use std::path::Path;

use image::{DynamicImage, ImageDecoder, ImageFormat, ImageReader};

use crate::error::{Error, Step};
use crate::resample::Pixels;

/// The file at `path`, decoded and upright.
pub fn open(path: &Path) -> Result<DynamicImage, Error> {
    let bytes = std::fs::read(path).map_err(|source| Error::Io {
        path: path.to_path_buf(),
        step: Step::Read,
        source,
    })?;
    let mut reader = ImageReader::new(Cursor::new(bytes.as_slice()))
        .with_guessed_format()
        .map_err(|source| Error::Io {
            path: path.to_path_buf(),
            step: Step::Read,
            source,
        })?;
    // A TGA file has no magic bytes: then the suffix tells the format.
    if reader.format().is_none()
        && let Ok(format) = ImageFormat::from_path(path)
    {
        reader.set_format(format);
    }
    let Some(format) = reader.format() else {
        return Err(Error::NotAnImage {
            path: path.to_path_buf(),
        });
    };
    let decode_error = |e: image::ImageError| match e {
        image::ImageError::Unsupported(_) => Error::NotAnImage {
            path: path.to_path_buf(),
        },
        other => Error::Decode {
            path: path.to_path_buf(),
            message: other.to_string(),
        },
    };
    #[cfg(feature = "turbo")]
    if format == ImageFormat::Jpeg {
        return open_turbo(&bytes).map_err(|message| Error::Decode {
            path: path.to_path_buf(),
            message,
        });
    }
    let _ = format;
    let mut decoder = reader.into_decoder().map_err(decode_error)?;
    let orientation = decoder.orientation().map_err(decode_error)?;
    let mut im = DynamicImage::from_decoder(decoder).map_err(decode_error)?;
    im.apply_orientation(orientation);
    Ok(im)
}

/// JPEG through libjpeg-turbo, with the default (exact) IDCT and the fancy upsampling,
/// as Pillow decodes it.
#[cfg(feature = "turbo")]
fn open_turbo(bytes: &[u8]) -> Result<DynamicImage, String> {
    use turbojpeg::{Colorspace, PixelFormat};
    let header = turbojpeg::read_header(bytes).map_err(|e| e.to_string())?;
    let cmyk = matches!(header.colorspace, Colorspace::CMYK | Colorspace::YCCK);
    let format = if cmyk {
        PixelFormat::CMYK
    } else {
        PixelFormat::RGB
    };
    let out = turbojpeg::decompress(bytes, format).map_err(|e| e.to_string())?;
    let size = format.size();
    let mut data = Vec::with_capacity(out.width * out.height * 3);
    for row in out.pixels.chunks(out.pitch).take(out.height) {
        let row = &row[..out.width * size];
        if cmyk {
            data.extend(
                row.chunks_exact(4)
                    .flat_map(|p| inverted_cmyk_to_rgb([p[0], p[1], p[2], p[3]])),
            );
        } else {
            data.extend_from_slice(row);
        }
    }
    let rgb = image::RgbImage::from_raw(out.width as u32, out.height as u32, data)
        .ok_or("the decoder gave a wrong size")?;
    let mut im = DynamicImage::ImageRgb8(rgb);
    // Read only the EXIF orientation with the decoder of the image crate.
    if let Ok(mut d) = image::codecs::jpeg::JpegDecoder::new(Cursor::new(bytes))
        && let Ok(orientation) = d.orientation()
    {
        im.apply_orientation(orientation);
    }
    Ok(im)
}

/// `MULDIV255` of Pillow: `a * b / 255`, rounded.
#[inline]
fn muldiv255(a: u32, b: u32) -> u32 {
    let t = a * b + 128;
    ((t >> 8) + t) >> 8
}

/// The raw CMYK of a JPEG, inverted as "CMYK;I", then `cmyk2rgb` of Pillow.
#[cfg_attr(not(feature = "turbo"), allow(dead_code))]
fn inverted_cmyk_to_rgb(raw: [u8; 4]) -> [u8; 3] {
    let [c, m, y, k] = raw.map(|v| 255 - u32::from(v));
    let nk = 255 - k;
    let one = |v: u32| (nk as i32 - muldiv255(v, nk) as i32).clamp(0, 255) as u8;
    [one(c), one(m), one(y)]
}

/// `BLEND` of Pillow's Paste.c: `DIV255(bg * (255 - a) + c * a)`.
#[inline]
fn blend(a: u8, bg: u8, c: u8) -> u8 {
    let (a, bg, c) = (u32::from(a), u32::from(bg), u32::from(c));
    let t = bg * (255 - a) + c * a + 128;
    (((t >> 8) + t) >> 8) as u8
}

/// The top 8 bits of a 16-bit sample.
fn top(v: u16) -> u8 {
    (v >> 8) as u8
}

/// `im` in 8-bit RGB. Alpha is blended onto `background`, or dropped for None.
pub fn normalize(im: DynamicImage, background: Option<[u8; 3]>) -> Pixels {
    let (w, h) = (im.width() as usize, im.height() as usize);
    let data = match im {
        DynamicImage::ImageRgb8(buf) => buf.into_raw(),
        DynamicImage::ImageRgb16(buf) => buf.into_raw().into_iter().map(top).collect(),
        DynamicImage::ImageLuma8(buf) => buf.into_raw().into_iter().flat_map(|v| [v; 3]).collect(),
        DynamicImage::ImageLuma16(buf) => buf
            .into_raw()
            .into_iter()
            .flat_map(|v| [top(v); 3])
            .collect(),
        im if im.color().has_alpha() => {
            let rgba: Vec<u8> = match im {
                DynamicImage::ImageRgba16(buf) => buf.into_raw().into_iter().map(top).collect(),
                DynamicImage::ImageLumaA16(buf) => buf
                    .into_raw()
                    .chunks_exact(2)
                    .flat_map(|p| {
                        let l = top(p[0]);
                        [l, l, l, top(p[1])]
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
    Pixels { w, h, data }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blend_matches_pillow() {
        // Image.new("RGB", (1, 1), "white").paste(RGBA (10, 20, 30, a)) of Pillow 13.
        assert_eq!(blend(0, 255, 10), 255);
        assert_eq!(blend(255, 255, 10), 10);
        assert_eq!(blend(128, 255, 10), 132);
    }

    #[test]
    fn cmyk_matches_pillow() {
        // Image.new("CMYK", (1, 1), (0, 255, 0, 0)).convert("RGB") is (255, 0, 255). The
        // JPEG stores CMYK inverted, so the raw values are 255 - CMYK.
        assert_eq!(inverted_cmyk_to_rgb([255, 0, 255, 255]), [255, 0, 255]);
        // (10, 20, 30, 40) gives (207, 198, 190) in Pillow.
        assert_eq!(inverted_cmyk_to_rgb([245, 235, 225, 215]), [207, 198, 190]);
    }

    #[test]
    fn alpha_without_background_is_dropped() {
        let rgba = image::RgbaImage::from_raw(1, 1, vec![10, 20, 30, 0]).unwrap();
        let out = normalize(DynamicImage::ImageRgba8(rgba.clone()), None);
        assert_eq!(out.data, vec![10, 20, 30]);
        let out = normalize(DynamicImage::ImageRgba8(rgba), Some([255, 255, 255]));
        assert_eq!(out.data, vec![255, 255, 255]);
    }
}
