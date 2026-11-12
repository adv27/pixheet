//! The XML parts of the package, and the text of the sheet rows.
//!
//! `sheet1.xml` and `styles.xml` are byte-identical to the output of the Python version.

use rustc_hash::FxHashMap;

use crate::resample::Pixels;

const XML_DECL: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n";
const MAIN_NS: &str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const DOC_REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const SHEET_CT: &str = "application/vnd.openxmlformats-officedocument.spreadsheetml";

pub(super) fn content_types_xml() -> String {
    format!(
        "{XML_DECL}<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">\
<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>\
<Default Extension=\"xml\" ContentType=\"application/xml\"/>\
<Override PartName=\"/xl/workbook.xml\" ContentType=\"{SHEET_CT}.sheet.main+xml\"/>\
<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"{SHEET_CT}.worksheet+xml\"/>\
<Override PartName=\"/xl/styles.xml\" ContentType=\"{SHEET_CT}.styles+xml\"/></Types>"
    )
}

pub(super) fn root_rels_xml() -> String {
    format!(
        "{XML_DECL}<Relationships xmlns=\"{PKG_REL}\">\
<Relationship Id=\"rId1\" Type=\"{DOC_REL}/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>"
    )
}

pub(super) fn workbook_xml() -> String {
    format!(
        "{XML_DECL}<workbook xmlns=\"{MAIN_NS}\" xmlns:r=\"{DOC_REL}\">\
<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"
    )
}

pub(super) fn workbook_rels_xml() -> String {
    format!(
        "{XML_DECL}<Relationships xmlns=\"{PKG_REL}\">\
<Relationship Id=\"rId1\" Type=\"{DOC_REL}/worksheet\" Target=\"worksheets/sheet1.xml\"/>\
<Relationship Id=\"rId2\" Type=\"{DOC_REL}/styles\" Target=\"styles.xml\"/></Relationships>"
    )
}

/// Columns and rows are about 1 px; rows without data are hidden.
pub(super) fn sheet_header_xml(reference: &str, width: usize) -> String {
    format!(
        "{XML_DECL}<worksheet xmlns=\"{MAIN_NS}\" xmlns:r=\"{DOC_REL}\">\
<dimension ref=\"{reference}\"/>\
<sheetViews><sheetView tabSelected=\"1\" workbookViewId=\"0\"/></sheetViews>\
<sheetFormatPr defaultRowHeight=\"0.75\" customHeight=\"1\" zeroHeight=\"1\"/>\
<cols><col min=\"1\" max=\"{width}\" width=\"0.140625\" customWidth=\"1\"/></cols><sheetData>"
    )
}

pub(super) const SHEET_FOOTER_XML: &str = "</sheetData>\
<pageMargins left=\"0.7\" right=\"0.7\" top=\"0.75\" bottom=\"0.75\" header=\"0.3\" footer=\"0.3\"/>\
</worksheet>";

/// styles.xml for the colors (0xRRGGBB) in the order of their style ids.
///
/// Fills 0 and 1 are reserved (`none` and `gray125`) and format 0 is the default style,
/// so color `i` (1-based) uses `fillId = i + 1` and is format `i`.
pub(super) fn styles_xml(colors: &[u32]) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let count = colors.len();
    let mut fills = String::with_capacity(count * 100);
    let mut xfs = String::with_capacity(count * 80);
    for (i, rgb) in colors.iter().enumerate() {
        fills.push_str("<fill><patternFill patternType=\"solid\"><fgColor rgb=\"FF");
        for shift in (0..24).step_by(4).rev() {
            fills.push(char::from(HEX[(rgb >> shift) as usize & 15]));
        }
        fills.push_str("\"/><bgColor indexed=\"64\"/></patternFill></fill>");
        xfs.push_str("<xf numFmtId=\"0\" fontId=\"0\" fillId=\"");
        xfs.push_str(itoa::Buffer::new().format(i + 2));
        xfs.push_str("\" borderId=\"0\" xfId=\"0\" applyFill=\"1\"/>");
    }
    format!(
        "{XML_DECL}<styleSheet xmlns=\"{MAIN_NS}\">\
<fonts count=\"1\"><font><sz val=\"11\"/><color theme=\"1\"/><name val=\"Calibri\"/>\
<family val=\"2\"/><scheme val=\"minor\"/></font></fonts>\
<fills count=\"{}\"><fill><patternFill patternType=\"none\"/></fill>\
<fill><patternFill patternType=\"gray125\"/></fill>{fills}</fills>\
<borders count=\"1\"><border><left/><right/><top/><bottom/><diagonal/></border></borders>\
<cellStyleXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellStyleXfs>\
<cellXfs count=\"{}\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/>\
{xfs}</cellXfs>\
<cellStyles count=\"1\"><cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\"/></cellStyles>\
<dxfs count=\"0\"/>\
<tableStyles count=\"0\" defaultTableStyle=\"TableStyleMedium9\" defaultPivotStyle=\"PivotStyleLight16\"/>\
</styleSheet>",
        count + 2,
        count + 1
    )
}

/// The column letters for a 0-based column index (0 gives "A", 16383 gives "XFD").
pub(super) fn column_name(index: usize) -> String {
    let mut name = Vec::new();
    let mut i = index + 1;
    while i > 0 {
        name.push(b'A' + ((i - 1) % 26) as u8);
        i = (i - 1) / 26;
    }
    name.reverse();
    String::from_utf8(name).unwrap_or_default()
}

/// The style id (1, 2, ...) of each pixel, by first appearance, and the colors in id order.
pub(super) fn style_ids(im: &Pixels) -> (Vec<u32>, Vec<u32>) {
    let mut map: FxHashMap<u32, u32> = FxHashMap::default();
    let mut order = Vec::new();
    let ids = im
        .data
        .as_chunks::<3>()
        .0
        .iter()
        .map(|p| {
            let key = (u32::from(p[0]) << 16) | (u32::from(p[1]) << 8) | u32::from(p[2]);
            *map.entry(key).or_insert_with(|| {
                order.push(key);
                order.len() as u32
            })
        })
        .collect();
    (ids, order)
}

/// Add the sheet XML of rows `y0..y1` to `out`.
pub(super) fn rows_xml(
    out: &mut Vec<u8>,
    ids: &[u32],
    w: usize,
    rows: std::ops::Range<usize>,
    cols: &[String],
    compact: bool,
) {
    let mut rbuf = itoa::Buffer::new();
    let mut sbuf = itoa::Buffer::new();
    for y in rows {
        let r = rbuf.format(y + 1);
        out.extend_from_slice(b"<row r=\"");
        out.extend_from_slice(r.as_bytes());
        out.extend_from_slice(b"\" ht=\"0.75\" customHeight=\"1\">");
        let row = &ids[y * w..(y + 1) * w];
        if compact {
            for &id in row {
                out.extend_from_slice(b"<c s=\"");
                out.extend_from_slice(sbuf.format(id).as_bytes());
                out.extend_from_slice(b"\"/>");
            }
        } else {
            for (col, &id) in cols.iter().zip(row) {
                out.extend_from_slice(b"<c r=\"");
                out.extend_from_slice(col.as_bytes());
                out.extend_from_slice(r.as_bytes());
                out.extend_from_slice(b"\" s=\"");
                out.extend_from_slice(sbuf.format(id).as_bytes());
                out.extend_from_slice(b"\"/>");
            }
        }
        out.extend_from_slice(b"</row>");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn column_names() {
        assert_eq!(column_name(0), "A");
        assert_eq!(column_name(25), "Z");
        assert_eq!(column_name(26), "AA");
        assert_eq!(column_name(16_383), "XFD");
    }
}
