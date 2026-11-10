//! The background color option: a port of `ImageColor.getrgb` of Pillow 13.

use crate::color_names::COLOR_NAMES;
use crate::error::{Error, quoted};

/// Parse a background color: a color name, `#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa`,
/// `rgb(r, g, b)`, `rgb(r%, g%, b%)`, `rgba(r, g, b, a)`, `hsl(h, s%, l%)`,
/// `hsv(h, s%, v%)` or `hsb(h, s%, b%)`. The alpha of a color has no effect.
///
/// Returns None for "none": then the alpha of the image goes away without a blend.
pub fn parse_background(text: &str) -> Result<Option<[u8; 3]>, Error> {
    let stripped = text.trim();
    if stripped.eq_ignore_ascii_case("none") {
        return Ok(None);
    }
    getrgb(stripped).map(Some).ok_or_else(|| {
        Error::Invalid(format!(
            "invalid background color {}: use white, black, #RRGGBB or none",
            quoted(text)
        ))
    })
}

/// Pillow clips a channel to 0..255 when it fills an image with the color.
fn channel(v: i64) -> u8 {
    v.clamp(0, 255) as u8
}

/// `int(x * 255 + 0.5)` of Python, then the clip.
fn scaled(x: f64) -> u8 {
    channel((x * 255.0 + 0.5).trunc() as i64)
}

fn getrgb(text: &str) -> Option<[u8; 3]> {
    if text.len() > 100 {
        return None;
    }
    let color = text.to_lowercase();
    if let Ok(i) = COLOR_NAMES.binary_search_by(|(name, _)| (*name).cmp(color.as_str())) {
        return Some(COLOR_NAMES[i].1);
    }
    if let Some(hex) = color.strip_prefix('#') {
        return parse_hex(hex);
    }
    if let Some(args) = call(&color, "rgb") {
        if let Some(v) = ints(&args, 3, false) {
            return Some([channel(v[0]), channel(v[1]), channel(v[2])]);
        }
        let v = ints(&args, 3, true)?;
        // int((value * 255) / 100.0 + 0.5)
        let pct = |p: i64| channel(((p * 255) as f64 / 100.0 + 0.5).trunc() as i64);
        return Some([pct(v[0]), pct(v[1]), pct(v[2])]);
    }
    if let Some(args) = call(&color, "rgba") {
        let v = ints(&args, 4, false)?;
        return Some([channel(v[0]), channel(v[1]), channel(v[2])]);
    }
    if let Some(args) = call(&color, "hsl") {
        let [h, s, l] = hue_args(&args)?;
        let (r, g, b) = hls_to_rgb(h / 360.0, l / 100.0, s / 100.0);
        return Some([scaled(r), scaled(g), scaled(b)]);
    }
    let hsv = call(&color, "hsv").or_else(|| call(&color, "hsb"));
    if let Some(args) = hsv {
        let [h, s, v] = hue_args(&args)?;
        let (r, g, b) = hsv_to_rgb(h / 360.0, s / 100.0, v / 100.0);
        return Some([scaled(r), scaled(g), scaled(b)]);
    }
    None
}

fn parse_hex(hex: &str) -> Option<[u8; 3]> {
    if !hex
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    let digit = |i: usize| u8::from_str_radix(&hex[i..=i], 16).ok();
    let pair = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok();
    match hex.len() {
        3 | 4 => Some([digit(0)? * 17, digit(1)? * 17, digit(2)? * 17]),
        6 | 8 => Some([pair(0)?, pair(2)?, pair(4)?]),
        _ => None,
    }
}

/// The text between "name(" and ")", split at the commas, each part trimmed.
fn call(color: &str, name: &str) -> Option<Vec<String>> {
    let inner = color
        .strip_prefix(name)?
        .strip_prefix('(')?
        .strip_suffix(')')?;
    Some(
        inner
            .split(',')
            .map(|part| part.trim().to_string())
            .collect(),
    )
}

/// `count` whole numbers, each with a "%" when `percent` is true.
fn ints(args: &[String], count: usize, percent: bool) -> Option<Vec<i64>> {
    if args.len() != count {
        return None;
    }
    args.iter()
        .map(|a| {
            let digits = if percent {
                a.strip_suffix('%')?
            } else {
                a.as_str()
            };
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            digits.parse().ok()
        })
        .collect()
}

/// A number as `\d+\.?\d*` of the regular expressions of Pillow.
fn number(text: &str) -> Option<f64> {
    let (int, frac) = text.split_once('.').unwrap_or((text, ""));
    let digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if int.is_empty() || !digits(int) || !digits(frac) {
        return None;
    }
    text.parse().ok()
}

/// "h, s%, x%": the hue and two percentages.
fn hue_args(args: &[String]) -> Option<[f64; 3]> {
    if args.len() != 3 {
        return None;
    }
    Some([
        number(&args[0])?,
        number(args[1].strip_suffix('%')?)?,
        number(args[2].strip_suffix('%')?)?,
    ])
}

const ONE_THIRD: f64 = 1.0 / 3.0;
const ONE_SIXTH: f64 = 1.0 / 6.0;
const TWO_THIRD: f64 = 2.0 / 3.0;

/// `colorsys.hls_to_rgb` of Python.
fn hls_to_rgb(h: f64, l: f64, s: f64) -> (f64, f64, f64) {
    if s == 0.0 {
        return (l, l, l);
    }
    let m2 = if l <= 0.5 {
        l * (1.0 + s)
    } else {
        l + s - (l * s)
    };
    let m1 = 2.0 * l - m2;
    (
        hue_value(m1, m2, h + ONE_THIRD),
        hue_value(m1, m2, h),
        hue_value(m1, m2, h - ONE_THIRD),
    )
}

fn hue_value(m1: f64, m2: f64, hue: f64) -> f64 {
    let hue = hue.rem_euclid(1.0); // % of Python has the sign of the divisor
    if hue < ONE_SIXTH {
        m1 + (m2 - m1) * hue * 6.0
    } else if hue < 0.5 {
        m2
    } else if hue < TWO_THIRD {
        m1 + (m2 - m1) * (TWO_THIRD - hue) * 6.0
    } else {
        m1
    }
}

/// `colorsys.hsv_to_rgb` of Python.
fn hsv_to_rgb(h: f64, s: f64, v: f64) -> (f64, f64, f64) {
    if s == 0.0 {
        return (v, v, v);
    }
    let i = (h * 6.0).trunc();
    let f = h * 6.0 - i;
    let (p, q, t) = (v * (1.0 - s), v * (1.0 - s * f), v * (1.0 - s * (1.0 - f)));
    match (i as i64).rem_euclid(6) {
        0 => (v, t, p),
        1 => (q, v, p),
        2 => (p, v, t),
        3 => (p, q, v),
        4 => (t, p, v),
        _ => (v, p, q),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The values come from `pixheet._image.parse_background` with Pillow 13, after the
    /// clip to 0..255 that `Image.new` does. For example, Python gives 300 for
    /// "rgb(300,0,0)", and the image then gets 255.
    #[test]
    fn colors_match_pillow() {
        let cases: &[(&str, Option<[u8; 3]>)] = &[
            ("white", Some([255, 255, 255])),
            ("black", Some([0, 0, 0])),
            ("#12AbEf", Some([0x12, 0xAB, 0xEF])),
            ("  NONE ", None),
            ("none", None),
            ("#11223344", Some([0x11, 0x22, 0x33])),
            ("#abc", Some([0xAA, 0xBB, 0xCC])),
            ("#abcd", Some([0xAA, 0xBB, 0xCC])),
            ("RebeccaPurple", Some([102, 51, 153])),
            ("lightgoldenrodyellow", Some([250, 250, 210])),
            ("rgb(1, 2,3)", Some([1, 2, 3])),
            ("rgb(300,0,0)", Some([255, 0, 0])),
            ("rgb(50%, 100%, 0%)", Some([128, 255, 0])),
            ("rgba(9,8,7,0)", Some([9, 8, 7])),
            ("hsl(120, 100%, 25%)", Some([0, 128, 0])),
            ("hsl(30.5, 40%, 60.25%)", Some([194, 154, 113])),
            ("hsv(200, 50%, 80%)", Some([102, 170, 204])),
            ("hsb(359, 100%, 100%)", Some([255, 0, 4])),
            // Gray, then each sixth of the hue circle.
            ("hsl(0, 0%, 50%)", Some([128, 128, 128])),
            ("hsv(0, 0%, 50%)", Some([128, 128, 128])),
            ("hsl(240, 100%, 50%)", Some([0, 0, 255])),
            ("hsl(300, 60%, 70%)", Some([224, 133, 224])),
            ("hsv(30, 100%, 100%)", Some([255, 128, 0])),
            ("hsv(60, 100%, 100%)", Some([255, 255, 0])),
            ("hsv(120, 100%, 100%)", Some([0, 255, 0])),
            ("hsv(180, 100%, 100%)", Some([0, 255, 255])),
            ("hsv(240, 100%, 100%)", Some([0, 0, 255])),
            ("hsv(300, 50%, 100%)", Some([255, 128, 255])),
        ];
        for (text, expected) in cases {
            assert_eq!(parse_background(text).unwrap(), *expected, "{text}");
        }
    }

    #[test]
    fn bad_colors_fail() {
        for text in [
            "",
            "nope",
            "#12345",
            "#GGGGGG",
            "rgb(1,2)",
            "rgb(1%,2,3)",
            "hsl(1,2,3)",
            "hsl(1, 2%)",
            "hsl(x, 1%, 1%)",
            "hsl(-5, 10%, 10%)",
        ] {
            let err = parse_background(text).unwrap_err().to_string();
            assert!(err.starts_with("invalid background color"), "{text}: {err}");
        }
        // Pillow refuses a color of more than 100 characters, also a correct one.
        let color = |zeros: usize| format!("rgb({}1,2,3)", "0".repeat(zeros));
        assert_eq!(parse_background(&color(90)).unwrap(), Some([1, 2, 3]));
        assert!(parse_background(&color(91)).is_err());
        assert_eq!(
            parse_background("nope").unwrap_err().to_string(),
            "invalid background color 'nope': use white, black, #RRGGBB or none"
        );
    }

    #[test]
    fn the_names_are_sorted() {
        assert!(COLOR_NAMES.windows(2).all(|w| w[0].0 < w[1].0));
        assert_eq!(COLOR_NAMES.len(), 148);
    }
}
