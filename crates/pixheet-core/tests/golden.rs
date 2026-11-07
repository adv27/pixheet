//! The golden test: the output of the Rust version must be the output of the Python
//! version, which is gone now.
//!
//! `fixtures/expected.tsv` holds, for each image and set of options, the sheet size, the
//! number of colors, and the SHA-256 of `sheet1.xml` and `styles.xml` that the Python
//! version (Pillow 13.0.0) wrote. The parts hold each pixel, so the same digests also mean
//! the same pixels. The compressed bytes can differ.

use std::io::Read;
use std::path::Path;

use pixheet_core::{Options, convert, parse_background};
use sha2::{Digest, Sha256};

fn sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn part(path: &Path, name: &str) -> Vec<u8> {
    let mut zip = zip::ZipArchive::new(std::fs::File::open(path).unwrap()).unwrap();
    let mut bytes = Vec::new();
    zip.by_name(name).unwrap().read_to_end(&mut bytes).unwrap();
    bytes
}

#[test]
fn the_output_is_the_output_of_the_python_version() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures");
    let expected = std::fs::read_to_string(fixtures.join("expected.tsv")).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let dst = dir.path().join("out.xlsx");
    let mut checked = 0;
    let mut problems = Vec::new();
    for line in expected
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
    {
        let f: Vec<&str> = line.split('\t').collect();
        let [
            file,
            max_side,
            compact,
            background,
            size,
            colors,
            sheet,
            styles,
        ] = f[..]
        else {
            panic!("a bad line in expected.tsv: {line}");
        };
        let options = Options {
            max_side: max_side.parse().unwrap(),
            background: parse_background(background).unwrap(),
            compact: compact == "1",
        };
        let case = format!("{file} max_side={max_side} compact={compact} background={background}");
        let result = match convert(&fixtures.join(file), &dst, &options, None) {
            Ok(result) => result,
            Err(e) => {
                problems.push(format!("{case}: {e}"));
                continue;
            }
        };
        let got_size = format!("{}x{}", result.size.0, result.size.1);
        if got_size != size || result.colors.to_string() != colors {
            problems.push(format!(
                "{case}: {got_size} {} colors, not {size} {colors}",
                result.colors
            ));
        }
        if sha256(&part(&dst, "xl/worksheets/sheet1.xml")) != sheet {
            problems.push(format!("{case}: sheet1.xml differs"));
        }
        if sha256(&part(&dst, "xl/styles.xml")) != styles {
            problems.push(format!("{case}: styles.xml differs"));
        }
        checked += 1;
    }
    assert!(checked >= 139, "only {checked} cases");
    assert!(
        problems.is_empty(),
        "{} problems:\n{}",
        problems.len(),
        problems.join("\n")
    );
}
