//! A port of `pixheet._xlsx`: a streaming .xlsx writer, one blank cell for each pixel.
//!
//! `sheet1.xml` and `styles.xml` are byte-identical to the output of the Python version,
//! and thus to the output of xlsxwriter. This module writes the zip by hand, as Python's
//! `zipfile` does for a file that can seek: no data descriptors, and zip64 only for a
//! member that can be larger than 2 GiB.
//!
//! The sheet XML goes out in chunks of about CHUNK bytes. The rayon pool makes the text
//! of the chunks, then compresses them. Each chunk has its own DEFLATE compressor, with
//! the 32 KiB before the chunk as its preset dictionary (the pigz method, as in
//! `pixheet._threads`). The members decompress to the same bytes as one stream.

use std::io::{self, Seek, SeekFrom, Write};
use std::ops::ControlFlow;

use flate2::{Compress, Compression, FlushCompress, Status};
use rayon::prelude::*;
use rustc_hash::FxHashMap;

use crate::resample::Pixels;

/// Excel's column limit (A to XFD).
pub const MAX_COLUMNS: usize = 16_384;
/// Excel's row limit.
pub const MAX_ROWS: usize = 1_048_576;
/// Excel allows 64,000 cell formats, and format 0 is the default style.
pub const MAX_COLORS: usize = 63_999;
/// `zipfile.ZIP64_LIMIT`: a larger member or offset needs zip64.
pub(crate) const ZIP64_LIMIT: u64 = (1 << 31) - 1;
const ZIP_FILECOUNT_LIMIT: usize = (1 << 16) - 1;

const CHUNK: usize = 256 * 1024;
const WINDOW: usize = 32 * 1024;
/// The DEFLATE level. zipfile uses 6. With zlib-rs, level 3 compresses about 2 times
/// faster, and the files are only 2 to 3 % larger. Read docs/rust-port/PROFILE.md.
const LEVEL: u32 = 3;

const XML_DECL: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n";
const MAIN_NS: &str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const DOC_REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const SHEET_CT: &str = "application/vnd.openxmlformats-officedocument.spreadsheetml";

/// The longest cell and row markup. They tell if the sheet part can be larger than 2 GiB.
const MAX_CELL_BYTES: usize = "<c r=\"XFD1048576\" s=\"63999\"/>".len();
const MAX_ROW_BYTES: usize = "<row r=\"1048576\" ht=\"0.75\" customHeight=\"1\"></row>".len();

fn content_types_xml() -> String {
    format!(
        "{XML_DECL}<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">\
<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>\
<Default Extension=\"xml\" ContentType=\"application/xml\"/>\
<Override PartName=\"/xl/workbook.xml\" ContentType=\"{SHEET_CT}.sheet.main+xml\"/>\
<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"{SHEET_CT}.worksheet+xml\"/>\
<Override PartName=\"/xl/styles.xml\" ContentType=\"{SHEET_CT}.styles+xml\"/></Types>"
    )
}

fn root_rels_xml() -> String {
    format!(
        "{XML_DECL}<Relationships xmlns=\"{PKG_REL}\">\
<Relationship Id=\"rId1\" Type=\"{DOC_REL}/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>"
    )
}

fn workbook_xml() -> String {
    format!(
        "{XML_DECL}<workbook xmlns=\"{MAIN_NS}\" xmlns:r=\"{DOC_REL}\">\
<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"
    )
}

fn workbook_rels_xml() -> String {
    format!(
        "{XML_DECL}<Relationships xmlns=\"{PKG_REL}\">\
<Relationship Id=\"rId1\" Type=\"{DOC_REL}/worksheet\" Target=\"worksheets/sheet1.xml\"/>\
<Relationship Id=\"rId2\" Type=\"{DOC_REL}/styles\" Target=\"styles.xml\"/></Relationships>"
    )
}

/// Columns and rows are about 1 px; rows without data are hidden.
fn sheet_header_xml(reference: &str, width: usize) -> String {
    format!(
        "{XML_DECL}<worksheet xmlns=\"{MAIN_NS}\" xmlns:r=\"{DOC_REL}\">\
<dimension ref=\"{reference}\"/>\
<sheetViews><sheetView tabSelected=\"1\" workbookViewId=\"0\"/></sheetViews>\
<sheetFormatPr defaultRowHeight=\"0.75\" customHeight=\"1\" zeroHeight=\"1\"/>\
<cols><col min=\"1\" max=\"{width}\" width=\"0.140625\" customWidth=\"1\"/></cols><sheetData>"
    )
}

const SHEET_FOOTER_XML: &str = "</sheetData>\
<pageMargins left=\"0.7\" right=\"0.7\" top=\"0.75\" bottom=\"0.75\" header=\"0.3\" footer=\"0.3\"/>\
</worksheet>";

/// styles.xml for the colors (0xRRGGBB) in the order of their style ids.
///
/// Fills 0 and 1 are reserved (`none` and `gray125`) and format 0 is the default style,
/// so color `i` (1-based) uses `fillId = i + 1` and is format `i`.
pub(crate) fn styles_xml(colors: &[u32]) -> String {
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
pub fn column_name(index: usize) -> String {
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
fn style_ids(im: &Pixels) -> (Vec<u32>, Vec<u32>) {
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
fn rows_xml(
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

/// Raw DEFLATE of `data` with `dict` as the preset dictionary. A sync flush ends the
/// chunk on a byte boundary; with `last`, the chunk ends the stream.
fn deflate(data: &[u8], dict: &[u8], last: bool) -> Vec<u8> {
    let mut c = Compress::new(Compression::new(LEVEL), false);
    if !dict.is_empty() {
        // zlib-rs takes any dictionary for a new raw stream.
        c.set_dictionary(dict)
            .expect("a new compressor takes a dictionary");
    }
    let mut out = Vec::with_capacity(data.len() / 4 + 1024);
    let flush = if last {
        FlushCompress::Finish
    } else {
        FlushCompress::Sync
    };
    loop {
        if out.capacity() - out.len() < 64 * 1024 {
            out.reserve(out.capacity().max(64 * 1024));
        }
        let consumed = c.total_in() as usize;
        let status = c
            .compress_vec(&data[consumed..], &mut out, flush)
            .expect("deflate");
        let done = c.total_in() as usize == data.len();
        // The flush is complete when the compressor leaves space in the output.
        if status == Status::StreamEnd || (done && !last && out.capacity() > out.len()) {
            return out;
        }
    }
}

/// Compress consecutive chunks of one member on the pool. `tail` is the text before the
/// first chunk. With `last`, the last chunk ends the DEFLATE stream.
fn compress_chunks(chunks: &[&[u8]], tail: &[u8], last: bool) -> Vec<(Vec<u8>, crc32fast::Hasher)> {
    chunks
        .par_iter()
        .enumerate()
        .map(|(i, text)| {
            let dict: &[u8] = if i == 0 {
                tail
            } else {
                let prev = chunks[i - 1];
                &prev[prev.len().saturating_sub(WINDOW)..]
            };
            let mut hasher = crc32fast::Hasher::new();
            hasher.update(text);
            (deflate(text, dict, last && i == chunks.len() - 1), hasher)
        })
        .collect()
}

struct Entry {
    name: &'static str,
    crc: u32,
    csize: u64,
    usize: u64,
    offset: u64,
}

/// The "version made by" system: 0 for Windows, 3 for Unix, as zipfile writes it.
const CREATE_SYSTEM: u16 = if cfg!(windows) { 0 } else { 3 };

struct ZipOut<W: Write + Seek> {
    out: W,
    entries: Vec<Entry>,
    pos: u64,
    time: u16,
    date: u16,
    limit: u64,
}

/// The DOS time and date of now, in local time, as zipfile writes them.
fn dos_now() -> (u16, u16) {
    use chrono::{Datelike, Timelike};
    let now = chrono::Local::now();
    let time = (now.hour() << 11) | (now.minute() << 5) | (now.second() / 2);
    let year = (now.year() - 1980).clamp(0, 127) as u32;
    let date = (year << 9) | (now.month() << 5) | now.day();
    (time as u16, date as u16)
}

impl<W: Write + Seek> ZipOut<W> {
    fn new(out: W, limit: u64) -> Self {
        let (time, date) = dos_now();
        Self {
            out,
            entries: Vec::new(),
            pos: 0,
            time,
            date,
            limit,
        }
    }

    fn put(&mut self, b: &[u8]) -> io::Result<()> {
        self.out.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }

    /// The local file header. With `zip64`, the sizes are in a zip64 extra field.
    fn local_header(
        &mut self,
        name: &str,
        crc: u32,
        csize: u64,
        usize: u64,
        zip64: bool,
    ) -> io::Result<()> {
        let mut h = Vec::with_capacity(50 + name.len());
        h.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        h.extend_from_slice(&(if zip64 { 45u16 } else { 20 }).to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes()); // flags
        h.extend_from_slice(&8u16.to_le_bytes()); // deflate
        h.extend_from_slice(&self.time.to_le_bytes());
        h.extend_from_slice(&self.date.to_le_bytes());
        h.extend_from_slice(&crc.to_le_bytes());
        let (cs, us) = if zip64 {
            (u32::MAX, u32::MAX)
        } else {
            (csize as u32, usize as u32)
        };
        h.extend_from_slice(&cs.to_le_bytes());
        h.extend_from_slice(&us.to_le_bytes());
        h.extend_from_slice(&(name.len() as u16).to_le_bytes());
        h.extend_from_slice(&(if zip64 { 20u16 } else { 0 }).to_le_bytes());
        h.extend_from_slice(name.as_bytes());
        if zip64 {
            h.extend_from_slice(&1u16.to_le_bytes());
            h.extend_from_slice(&16u16.to_le_bytes());
            h.extend_from_slice(&usize.to_le_bytes());
            h.extend_from_slice(&csize.to_le_bytes());
        }
        self.put(&h)
    }

    /// A member that is in memory. A large member is compressed in chunks on the pool.
    fn member(&mut self, name: &'static str, data: &[u8]) -> io::Result<()> {
        let chunks: Vec<&[u8]> = data.chunks(CHUNK).collect();
        let packed = compress_chunks(&chunks, &[], true);
        let mut crc = crc32fast::Hasher::new();
        let mut csize = 0;
        for (p, hasher) in &packed {
            crc.combine(hasher);
            csize += p.len() as u64;
        }
        let crc = crc.finalize();
        let offset = self.pos;
        self.local_header(name, crc, csize, data.len() as u64, false)?;
        for (p, _) in &packed {
            self.put(p)?;
        }
        self.entries.push(Entry {
            name,
            crc,
            csize,
            usize: data.len() as u64,
            offset,
        });
        Ok(())
    }

    /// The central directory and its end record, with the zip64 records when necessary.
    fn finish(mut self) -> io::Result<W> {
        let cd_start = self.pos;
        let entries = std::mem::take(&mut self.entries);
        for e in &entries {
            let mut extra: Vec<u64> = Vec::new();
            let (mut cs, mut us, mut off) = (e.csize as u32, e.usize as u32, e.offset as u32);
            if e.usize > self.limit || e.csize > self.limit {
                extra.extend([e.usize, e.csize]);
                (cs, us) = (u32::MAX, u32::MAX);
            }
            if e.offset > self.limit {
                extra.push(e.offset);
                off = u32::MAX;
            }
            let version: u16 = if extra.is_empty() { 20 } else { 45 };
            let mut h = Vec::with_capacity(46 + e.name.len() + 28);
            h.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
            h.extend_from_slice(&(version | (CREATE_SYSTEM << 8)).to_le_bytes());
            h.extend_from_slice(&version.to_le_bytes());
            h.extend_from_slice(&0u16.to_le_bytes());
            h.extend_from_slice(&8u16.to_le_bytes());
            h.extend_from_slice(&self.time.to_le_bytes());
            h.extend_from_slice(&self.date.to_le_bytes());
            h.extend_from_slice(&e.crc.to_le_bytes());
            h.extend_from_slice(&cs.to_le_bytes());
            h.extend_from_slice(&us.to_le_bytes());
            h.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
            let extra_len = if extra.is_empty() {
                0
            } else {
                4 + 8 * extra.len()
            };
            h.extend_from_slice(&(extra_len as u16).to_le_bytes());
            h.extend_from_slice(&[0; 6]); // comment length, disk, internal attributes
            h.extend_from_slice(&(0o600u32 << 16).to_le_bytes()); // as zipfile writes
            h.extend_from_slice(&off.to_le_bytes());
            h.extend_from_slice(e.name.as_bytes());
            if !extra.is_empty() {
                h.extend_from_slice(&1u16.to_le_bytes());
                h.extend_from_slice(&((8 * extra.len()) as u16).to_le_bytes());
                for v in extra {
                    h.extend_from_slice(&v.to_le_bytes());
                }
            }
            self.put(&h)?;
        }
        let cd_size = self.pos - cd_start;
        let count = entries.len();
        if count > ZIP_FILECOUNT_LIMIT || cd_start > self.limit || cd_size > self.limit {
            let record = self.pos;
            let mut z = Vec::with_capacity(76);
            z.extend_from_slice(&0x0606_4b50u32.to_le_bytes());
            z.extend_from_slice(&44u64.to_le_bytes());
            z.extend_from_slice(&45u16.to_le_bytes());
            z.extend_from_slice(&45u16.to_le_bytes());
            z.extend_from_slice(&[0; 8]); // this disk, the disk of the directory
            z.extend_from_slice(&(count as u64).to_le_bytes());
            z.extend_from_slice(&(count as u64).to_le_bytes());
            z.extend_from_slice(&cd_size.to_le_bytes());
            z.extend_from_slice(&cd_start.to_le_bytes());
            z.extend_from_slice(&0x0706_4b50u32.to_le_bytes());
            z.extend_from_slice(&0u32.to_le_bytes());
            z.extend_from_slice(&record.to_le_bytes());
            z.extend_from_slice(&1u32.to_le_bytes());
            self.put(&z)?;
        }
        let mut end = Vec::with_capacity(22);
        end.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        end.extend_from_slice(&[0; 4]);
        let short = count.min(0xFFFF) as u16;
        end.extend_from_slice(&short.to_le_bytes());
        end.extend_from_slice(&short.to_le_bytes());
        end.extend_from_slice(&(cd_size.min(0xFFFF_FFFF) as u32).to_le_bytes());
        end.extend_from_slice(&(cd_start.min(0xFFFF_FFFF) as u32).to_le_bytes());
        end.extend_from_slice(&0u16.to_le_bytes());
        self.put(&end)?;
        self.out.flush()?;
        Ok(self.out)
    }
}

/// Why the writer stopped.
#[derive(Debug)]
pub(crate) enum WriteError {
    Io(io::Error),
    Invalid(String),
    Cancelled,
}

impl From<io::Error> for WriteError {
    fn from(e: io::Error) -> Self {
        WriteError::Io(e)
    }
}

/// The progress function of the writer: `(rows_done, rows_total)`. `Break` stops it.
pub type Progress<'a> = &'a mut dyn FnMut(usize, usize) -> ControlFlow<()>;

/// Write `im` as an .xlsx to `dst`, one cell for each pixel. Return the number of colors.
///
/// `progress` gets `(0, h)` before the first row, then one call for each row, and the
/// last call is `(h, h)`. The calls come in groups: the pool makes a group of rows,
/// then the writer reports each row of the group.
pub(crate) fn write_xlsx<W: Write + Seek>(
    im: &Pixels,
    dst: W,
    compact: bool,
    progress: Progress<'_>,
) -> Result<usize, WriteError> {
    write_with_limit(im, dst, compact, progress, ZIP64_LIMIT)
}

fn write_with_limit<W: Write + Seek>(
    im: &Pixels,
    dst: W,
    compact: bool,
    progress: Progress<'_>,
    limit: u64,
) -> Result<usize, WriteError> {
    let (w, h) = (im.w, im.h);
    if !(w > 0 && w <= MAX_COLUMNS && h > 0 && h <= MAX_ROWS) {
        return Err(WriteError::Invalid(format!(
            "{w}x{h} does not fit a sheet ({MAX_COLUMNS}x{MAX_ROWS} at most)"
        )));
    }
    let zip64 = (w * h * MAX_CELL_BYTES + h * MAX_ROW_BYTES) as u64 > limit;
    let (ids, order) = style_ids(im);

    let mut zip = ZipOut::new(dst, limit);
    zip.member("[Content_Types].xml", content_types_xml().as_bytes())?;
    zip.member("_rels/.rels", root_rels_xml().as_bytes())?;
    zip.member("xl/workbook.xml", workbook_xml().as_bytes())?;
    zip.member("xl/_rels/workbook.xml.rels", workbook_rels_xml().as_bytes())?;

    let cols: Vec<String> = (0..w).map(column_name).collect();
    let reference = if w == 1 && h == 1 {
        "A1".to_string()
    } else {
        format!("A1:{}{}", cols[w - 1], h)
    };
    let header = sheet_header_xml(&reference, w);
    let row_bytes = 45 + w * if compact { 12 } else { 20 };
    let rows_per_chunk = (CHUNK / row_bytes).max(1);
    let rows_per_window = rows_per_chunk * rayon::current_num_threads() * 2;

    let name = "xl/worksheets/sheet1.xml";
    let offset = zip.pos;
    zip.local_header(name, 0, 0, 0, zip64)?;
    if progress(0, h).is_break() {
        return Err(WriteError::Cancelled);
    }
    let mut crc = crc32fast::Hasher::new();
    let (mut csize, mut usize_) = (0u64, 0u64);
    let mut tail: Vec<u8> = Vec::new(); // the last WINDOW bytes of text before this window
    let mut y = 0;
    while y < h {
        let y_end = (y + rows_per_window).min(h);
        let spans: Vec<(usize, usize)> = (y..y_end)
            .step_by(rows_per_chunk)
            .map(|a| (a, (a + rows_per_chunk).min(y_end)))
            .collect();
        let last_window = y_end == h;
        let texts: Vec<Vec<u8>> = spans
            .par_iter()
            .enumerate()
            .map(|(i, &(a, b))| {
                let mut out = Vec::with_capacity(CHUNK + row_bytes);
                if y == 0 && i == 0 {
                    out.extend_from_slice(header.as_bytes());
                }
                rows_xml(&mut out, &ids, w, a..b, &cols, compact);
                if last_window && i == spans.len() - 1 {
                    out.extend_from_slice(SHEET_FOOTER_XML.as_bytes());
                }
                out
            })
            .collect();
        let views: Vec<&[u8]> = texts.iter().map(Vec::as_slice).collect();
        for ((data, hasher), text) in compress_chunks(&views, &tail, last_window)
            .iter()
            .zip(&texts)
        {
            zip.put(data)?;
            crc.combine(hasher);
            csize += data.len() as u64;
            usize_ += text.len() as u64;
        }
        if let Some(last) = texts.last() {
            tail = last[last.len().saturating_sub(WINDOW)..].to_vec();
        }
        for row in y..y_end {
            if progress(row + 1, h).is_break() {
                return Err(WriteError::Cancelled);
            }
        }
        y = y_end;
    }
    // Write the CRC and the sizes into the local header, as zipfile does.
    let crc = crc.finalize();
    let end = zip.pos;
    zip.out.seek(SeekFrom::Start(offset + 14))?;
    zip.out.write_all(&crc.to_le_bytes())?;
    if zip64 {
        zip.out
            .seek(SeekFrom::Start(offset + 30 + name.len() as u64 + 4))?;
        zip.out.write_all(&usize_.to_le_bytes())?;
        zip.out.write_all(&csize.to_le_bytes())?;
    } else {
        zip.out.write_all(&(csize as u32).to_le_bytes())?;
        zip.out.write_all(&(usize_ as u32).to_le_bytes())?;
    }
    zip.out.seek(SeekFrom::Start(end))?;
    zip.entries.push(Entry {
        name,
        crc,
        csize,
        usize: usize_,
        offset,
    });

    // Written after the sheet, once every color is known.
    zip.member("xl/styles.xml", styles_xml(&order).as_bytes())?;
    zip.finish()?;
    Ok(order.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Read};

    fn image(w: usize, h: usize, colors: &[[u8; 3]]) -> Pixels {
        let mut im = Pixels::new(w, h);
        for (i, p) in im.data.as_chunks_mut::<3>().0.iter_mut().enumerate() {
            p.copy_from_slice(&colors[i % colors.len()]);
        }
        im
    }

    fn write(im: &Pixels, compact: bool, limit: u64) -> zip::ZipArchive<Cursor<Vec<u8>>> {
        let mut buf = Cursor::new(Vec::new());
        write_with_limit(
            im,
            &mut buf,
            compact,
            &mut |_, _| ControlFlow::Continue(()),
            limit,
        )
        .unwrap();
        zip::ZipArchive::new(Cursor::new(buf.into_inner())).unwrap()
    }

    fn part(zip: &mut zip::ZipArchive<Cursor<Vec<u8>>>, name: &str) -> String {
        let mut text = String::new();
        zip.by_name(name)
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        text
    }

    /// The sheet XML of a 2x2 image, as `pixheet._xlsx.write_xlsx` writes it.
    const SHEET_2X2: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n\
<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" \
xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">\
<dimension ref=\"A1:B2\"/><sheetViews><sheetView tabSelected=\"1\" workbookViewId=\"0\"/>\
</sheetViews><sheetFormatPr defaultRowHeight=\"0.75\" customHeight=\"1\" zeroHeight=\"1\"/>\
<cols><col min=\"1\" max=\"2\" width=\"0.140625\" customWidth=\"1\"/></cols><sheetData>\
<row r=\"1\" ht=\"0.75\" customHeight=\"1\"><c r=\"A1\" s=\"1\"/><c r=\"B1\" s=\"2\"/></row>\
<row r=\"2\" ht=\"0.75\" customHeight=\"1\"><c r=\"A2\" s=\"3\"/><c r=\"B2\" s=\"1\"/></row>\
</sheetData><pageMargins left=\"0.7\" right=\"0.7\" top=\"0.75\" bottom=\"0.75\" \
header=\"0.3\" footer=\"0.3\"/></worksheet>";

    #[test]
    fn the_sheet_matches_the_python_writer() {
        let im = image(2, 2, &[[255, 0, 0], [0, 255, 0], [0, 0, 255]]);
        let mut zip = write(&im, false, ZIP64_LIMIT);
        assert_eq!(part(&mut zip, "xl/worksheets/sheet1.xml"), SHEET_2X2);
        let styles = part(&mut zip, "xl/styles.xml");
        assert!(styles.contains("<fills count=\"5\">"));
        assert!(styles.contains("<cellXfs count=\"4\">"));
        let red = styles.find("rgb=\"FFFF0000\"").unwrap();
        let blue = styles.find("rgb=\"FF0000FF\"").unwrap();
        assert!(red < blue);
    }

    #[test]
    fn compact_leaves_out_the_references() {
        let im = image(2, 2, &[[255, 0, 0], [0, 255, 0], [0, 0, 255]]);
        let mut zip = write(&im, true, ZIP64_LIMIT);
        let sheet = part(&mut zip, "xl/worksheets/sheet1.xml");
        let expected = SHEET_2X2.replace(" r=\"A1\"", "").replace(" r=\"B1\"", "");
        let expected = expected.replace(" r=\"A2\"", "").replace(" r=\"B2\"", "");
        assert_eq!(sheet, expected);
    }

    #[test]
    fn one_pixel_has_the_reference_a1() {
        let mut zip = write(&image(1, 1, &[[1, 2, 3]]), false, ZIP64_LIMIT);
        assert!(part(&mut zip, "xl/worksheets/sheet1.xml").contains("<dimension ref=\"A1\"/>"));
    }

    #[test]
    fn a_large_sheet_spans_many_chunks() {
        // About 1.5 MB of text: several windows of chunks with a preset dictionary.
        let im = image(700, 100, &[[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]);
        let mut zip = write(&im, false, ZIP64_LIMIT);
        let sheet = part(&mut zip, "xl/worksheets/sheet1.xml");
        assert!(sheet.ends_with("</worksheet>"));
        assert_eq!(sheet.matches("<row ").count(), 100);
        assert!(sheet.contains("<c r=\"ZX100\" s=\"4\"/>"));
    }

    #[test]
    fn zip64_members_can_be_read() {
        // A limit of 100 bytes makes each record use zip64, as for a sheet above 2 GiB.
        let im = image(50, 40, &[[1, 2, 3], [4, 5, 6]]);
        let mut zip = write(&im, false, 100);
        let sheet = part(&mut zip, "xl/worksheets/sheet1.xml");
        assert!(sheet.contains("<c r=\"AX40\""));
        assert_eq!(zip.len(), 6);
    }

    #[test]
    fn the_writer_reports_each_row() {
        let im = image(3, 5, &[[1, 2, 3]]);
        let mut calls = Vec::new();
        let mut progress = |done, total| {
            calls.push((done, total));
            ControlFlow::Continue(())
        };
        write_xlsx(&im, Cursor::new(Vec::new()), false, &mut progress).unwrap();
        assert_eq!(calls, (0..=5).map(|i| (i, 5)).collect::<Vec<_>>());
    }

    #[test]
    fn the_progress_function_can_stop_the_writer() {
        let im = image(3, 5, &[[1, 2, 3]]);
        let result = write_xlsx(&im, Cursor::new(Vec::new()), false, &mut |done, _| {
            if done == 0 {
                ControlFlow::Continue(())
            } else {
                ControlFlow::Break(())
            }
        });
        assert!(matches!(result, Err(WriteError::Cancelled)));
    }

    #[test]
    fn a_sheet_larger_than_the_grid_fails() {
        let im = Pixels::new(MAX_COLUMNS + 1, 1);
        let result = write_xlsx(&im, Cursor::new(Vec::new()), false, &mut |_, _| {
            ControlFlow::Continue(())
        });
        assert!(matches!(result, Err(WriteError::Invalid(_))));
    }

    #[test]
    fn column_names() {
        assert_eq!(column_name(0), "A");
        assert_eq!(column_name(25), "Z");
        assert_eq!(column_name(26), "AA");
        assert_eq!(column_name(16_383), "XFD");
    }
}
