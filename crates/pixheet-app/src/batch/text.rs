//! The numbers and the times as people read them.

/// `3` and "image" give "3 images".
pub(super) fn plural(count: usize, word: &str) -> String {
    let n = grouped(count);
    if count == 1 {
        format!("{n} {word}")
    } else {
        format!("{n} {word}s")
    }
}

/// `1234567` gives "1,234,567".
pub(super) fn grouped(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// A file size for people, for example "712 KB" or "1.9 MB".
pub(super) fn size_text(bytes: u64) -> String {
    match bytes {
        b if b < 1024 => format!("{b} bytes"),
        b if b < 1024 * 1024 => format!("{} KB", (b as f64 / 1024.0).round()),
        b => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
    }
}

/// The time left, for example "About 40 s left", after 2 or more files.
pub(super) fn eta_text(seconds: f64, finished: usize, left: usize) -> String {
    if finished < 2 || left == 0 || seconds <= 0.0 {
        return String::new();
    }
    let rest = seconds / finished as f64 * left as f64;
    if rest < 57.5 {
        format!("About {} s left", ((rest / 5.0).round() as u64 * 5).max(5))
    } else if rest < 3570.0 {
        format!("About {} min left", ((rest / 60.0).round() as u64).max(1))
    } else {
        format!("About {} h left", (rest / 3600.0).round() as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_numbers_of_the_summary_are_for_people() {
        assert_eq!(grouped(999), "999");
        assert_eq!(grouped(1_234_567), "1,234,567");
        assert_eq!(size_text(1000), "1000 bytes");
        assert_eq!(size_text(712 * 1024), "712 KB");
        assert_eq!(size_text(2_000_000), "1.9 MB");
        // The time left comes only after 2 images, from the mean time of an image.
        assert_eq!(eta_text(10.0, 1, 5), "");
        assert_eq!(eta_text(10.0, 2, 0), "");
        assert_eq!(eta_text(1.0, 2, 1), "About 5 s left");
        assert_eq!(eta_text(10.0, 2, 4), "About 20 s left");
        assert_eq!(eta_text(60.0, 2, 4), "About 2 min left");
        assert_eq!(eta_text(3600.0, 2, 4), "About 2 h left");
    }
}
