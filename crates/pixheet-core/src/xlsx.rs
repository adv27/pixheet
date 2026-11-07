//! A port of `pixheet._xlsx`: the same `sheet1.xml` and `styles.xml` bytes, in a zip
//! that this module writes by hand.
//!
//! The sheet XML goes out in chunks of about CHUNK bytes. The rayon pool makes the text
//! of the chunks, then compresses them. Each chunk has its own DEFLATE compressor, with
//! the 32 KiB before the chunk as its preset dictionary (the pigz method, as in
//! `pixheet._threads`).

use std::fs::File;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::{Duration, Instant};

use flate2::{Compress, Compression, FlushCompress, Status};
use rayon::prelude::*;
use rustc_hash::FxHashMap;

use crate::resample::Rgb;

pub const MAX_COLUMNS: usize = 16_384;
pub const MAX_ROWS: usize = 1_048_576;
pub const MAX_COLORS: usize = 63_999;

const CHUNK: usize = 256 * 1024;
const WINDOW: usize = 32 * 1024;

const XML_DECL: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n";
const MAIN_NS: &str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const DOC_REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const SHEET_CT: &str = "application/vnd.openxmlformats-officedocument.spreadsheetml";

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
pub fn styles_xml(colors: &[u32]) -> String {
    let count = colors.len();
    let mut fills = String::with_capacity(count * 100);
    let mut xfs = String::with_capacity(count * 80);
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
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

pub fn column_name(index: usize) -> String {
    let mut name = Vec::new();
    let mut i = index + 1;
    while i > 0 {
        let rem = (i - 1) % 26;
        name.push(b'A' + rem as u8);
        i = (i - 1) / 26;
    }
    name.reverse();
    String::from_utf8(name).unwrap()
}

/// The time of each stage of the writer.
#[derive(Default, Debug, Clone, Copy)]
pub struct WriteTimes {
    /// The style id of each pixel (one thread).
    pub ids: Duration,
    /// The text of the sheet and its compression, on the pool.
    pub sheet: Duration,
    /// The other parts, the zip directory and the file.
    pub rest: Duration,
}

/// The style id (1, 2, ...) of each pixel, by first appearance, and the colors in id order.
fn style_ids(im: &Rgb) -> (Vec<u32>, Vec<u32>) {
    let mut map: FxHashMap<u32, u32> = FxHashMap::default();
    let mut order = Vec::new();
    let ids = im
        .data
        .chunks_exact(3)
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

/// The sheet XML of rows `y0..y1`.
fn rows_xml(
    out: &mut Vec<u8>,
    ids: &[u32],
    w: usize,
    y0: usize,
    y1: usize,
    cols: &[String],
    compact: bool,
) {
    let mut rbuf = itoa::Buffer::new();
    let mut sbuf = itoa::Buffer::new();
    for y in y0..y1 {
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
/// chunk on a byte boundary; the last chunk ends the stream.
fn deflate(data: &[u8], dict: &[u8], last: bool) -> Vec<u8> {
    let mut c = Compress::new(Compression::new(6), false);
    if !dict.is_empty() {
        c.set_dictionary(dict).expect("set the dictionary");
    }
    let mut out = Vec::with_capacity(data.len() / 4 + 1024);
    let flush = if last {
        FlushCompress::Finish
    } else {
        FlushCompress::Sync
    };
    loop {
        let consumed = c.total_in() as usize;
        if out.capacity() - out.len() < 64 * 1024 {
            out.reserve(out.capacity().max(64 * 1024));
        }
        let status = c
            .compress_vec(&data[consumed..], &mut out, flush)
            .expect("deflate");
        let done = c.total_in() as usize == data.len();
        match status {
            Status::StreamEnd => break,
            _ if done && !last && out.capacity() > out.len() => break,
            _ => {}
        }
    }
    out
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

struct ZipOut<W: Write + Seek> {
    out: W,
    entries: Vec<Entry>,
    pos: u64,
    time: u16,
    date: u16,
}

/// The DOS time and date of now, in UTC (the spike has no time zone data).
fn dos_now() -> (u16, u16) {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // The civil date from the day number (Howard Hinnant).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let time = ((rem / 3600) << 11) | (((rem % 3600) / 60) << 5) | ((rem % 60) / 2);
    let date = ((year - 1980).max(0) << 9) | (month << 5) | day;
    (time as u16, date as u16)
}

impl<W: Write + Seek> ZipOut<W> {
    fn new(out: W) -> Self {
        let (time, date) = dos_now();
        Self {
            out,
            entries: Vec::new(),
            pos: 0,
            time,
            date,
        }
    }

    fn put(&mut self, b: &[u8]) -> io::Result<()> {
        self.out.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }

    fn local_header(&mut self, name: &str, crc: u32, csize: u32, usize: u32) -> io::Result<()> {
        let mut h = Vec::with_capacity(30 + name.len());
        h.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        h.extend_from_slice(&20u16.to_le_bytes()); // version needed
        h.extend_from_slice(&0u16.to_le_bytes()); // flags
        h.extend_from_slice(&8u16.to_le_bytes()); // deflate
        h.extend_from_slice(&self.time.to_le_bytes());
        h.extend_from_slice(&self.date.to_le_bytes());
        h.extend_from_slice(&crc.to_le_bytes());
        h.extend_from_slice(&csize.to_le_bytes());
        h.extend_from_slice(&usize.to_le_bytes());
        h.extend_from_slice(&(name.len() as u16).to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes());
        h.extend_from_slice(name.as_bytes());
        self.put(&h)
    }

    /// A member that is in memory. A large member is compressed in chunks on the pool.
    fn small(&mut self, name: &'static str, data: &[u8]) -> io::Result<()> {
        let chunks: Vec<&[u8]> = data.chunks(CHUNK).collect();
        let packed = compress_chunks(&chunks, &[], true);
        let mut crc = crc32fast::Hasher::new();
        let mut csize = 0;
        for (p, hasher) in &packed {
            crc.combine(hasher);
            csize += p.len();
        }
        let crc = crc.finalize();
        let offset = self.pos;
        self.local_header(name, crc, csize as u32, data.len() as u32)?;
        for (p, _) in &packed {
            self.put(p)?;
        }
        self.entries.push(Entry {
            name,
            crc,
            csize: csize as u64,
            usize: data.len() as u64,
            offset,
        });
        Ok(())
    }

    fn finish(mut self) -> io::Result<W> {
        let cd_start = self.pos;
        let entries = std::mem::take(&mut self.entries);
        for e in &entries {
            let mut h = Vec::with_capacity(46 + e.name.len());
            h.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
            h.extend_from_slice(&(20u16 | (3 << 8)).to_le_bytes()); // made by: Unix, 2.0
            h.extend_from_slice(&20u16.to_le_bytes());
            h.extend_from_slice(&0u16.to_le_bytes());
            h.extend_from_slice(&8u16.to_le_bytes());
            h.extend_from_slice(&self.time.to_le_bytes());
            h.extend_from_slice(&self.date.to_le_bytes());
            h.extend_from_slice(&e.crc.to_le_bytes());
            h.extend_from_slice(&(e.csize as u32).to_le_bytes());
            h.extend_from_slice(&(e.usize as u32).to_le_bytes());
            h.extend_from_slice(&(e.name.len() as u16).to_le_bytes());
            h.extend_from_slice(&[0; 8]); // extra, comment, disk, internal attributes
            h.extend_from_slice(&((0o600u32) << 16).to_le_bytes()); // as zipfile.writestr
            h.extend_from_slice(&(e.offset as u32).to_le_bytes());
            h.extend_from_slice(e.name.as_bytes());
            self.put(&h)?;
        }
        let cd_size = self.pos - cd_start;
        let mut end = Vec::with_capacity(22);
        end.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        end.extend_from_slice(&[0; 4]);
        end.extend_from_slice(&(entries.len() as u16).to_le_bytes());
        end.extend_from_slice(&(entries.len() as u16).to_le_bytes());
        end.extend_from_slice(&(cd_size as u32).to_le_bytes());
        end.extend_from_slice(&(cd_start as u32).to_le_bytes());
        end.extend_from_slice(&0u16.to_le_bytes());
        self.put(&end)?;
        self.out.flush()?;
        Ok(self.out)
    }
}

/// Only the text of the sheet, without compression: for the stage times.
pub fn sheet_text_only(im: &Rgb, compact: bool) -> usize {
    let (ids, _) = style_ids(im);
    let cols: Vec<String> = (0..im.w).map(column_name).collect();
    let mut out = Vec::with_capacity(CHUNK * 2);
    let mut total = 0;
    for y in 0..im.h {
        rows_xml(&mut out, &ids, im.w, y, y + 1, &cols, compact);
        if out.len() > CHUNK {
            total += out.len();
            out.clear();
        }
    }
    total + out.len()
}

/// Write `im` as an .xlsx to `dst`. Returns the number of colors and the stage times.
pub fn write_xlsx<W: Write + Seek>(
    im: &Rgb,
    dst: W,
    compact: bool,
) -> io::Result<(usize, WriteTimes)> {
    let (w, h) = (im.w, im.h);
    if !(w > 0 && w <= MAX_COLUMNS && h > 0 && h <= MAX_ROWS) {
        return Err(io::Error::other(format!("{w}x{h} does not fit a sheet")));
    }
    let max_cell = "<c r=\"XFD1048576\" s=\"63999\"/>".len();
    let max_row = "<row r=\"1048576\" ht=\"0.75\" customHeight=\"1\"></row>".len();
    if w * h * max_cell + h * max_row > (1usize << 31) - 1 {
        return Err(io::Error::other("the spike does not write zip64 members"));
    }
    let mut times = WriteTimes::default();
    let t = Instant::now();
    let (ids, order) = style_ids(im);
    times.ids = t.elapsed();

    let t = Instant::now();
    let mut zip = ZipOut::new(dst);
    zip.small("[Content_Types].xml", content_types_xml().as_bytes())?;
    zip.small("_rels/.rels", root_rels_xml().as_bytes())?;
    zip.small("xl/workbook.xml", workbook_xml().as_bytes())?;
    zip.small("xl/_rels/workbook.xml.rels", workbook_rels_xml().as_bytes())?;
    times.rest += t.elapsed();

    let t = Instant::now();
    let cols: Vec<String> = (0..w).map(column_name).collect();
    let reference = if w == 1 && h == 1 {
        "A1".to_string()
    } else {
        format!("A1:{}{}", cols[w - 1], h)
    };
    let header = sheet_header_xml(&reference, w);
    let row_bytes = 45 + w * if compact { 12 } else { 20 };
    let rows_per_chunk = (CHUNK / row_bytes).max(1);
    let threads = rayon::current_num_threads();
    let rows_per_window = rows_per_chunk * threads * 2;

    let sheet_offset = zip.pos;
    let name = "xl/worksheets/sheet1.xml";
    zip.local_header(name, 0, 0, 0)?;
    let mut crc = crc32fast::Hasher::new();
    let (mut csize, mut usize_) = (0u64, 0u64);
    let mut tail: Vec<u8> = Vec::new(); // the last WINDOW bytes of text before this window
    let mut y = 0;
    let mut first = true;
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
                if first && i == 0 {
                    out.extend_from_slice(header.as_bytes());
                }
                rows_xml(&mut out, &ids, w, a, b, &cols, compact);
                if last_window && i == spans.len() - 1 {
                    out.extend_from_slice(SHEET_FOOTER_XML.as_bytes());
                }
                out
            })
            .collect();
        let views: Vec<&[u8]> = texts.iter().map(Vec::as_slice).collect();
        let packed = compress_chunks(&views, &tail, last_window);
        for ((data, hasher), text) in packed.into_iter().zip(&texts) {
            zip.put(&data)?;
            crc.combine(&hasher);
            csize += data.len() as u64;
            usize_ += text.len() as u64;
        }
        let last_text = texts.last().unwrap();
        tail = last_text[last_text.len().saturating_sub(WINDOW)..].to_vec();
        first = false;
        y = y_end;
    }
    // Write the sizes and the CRC into the local header, as zipfile does.
    let crc = crc.finalize();
    let end = zip.pos;
    zip.out.seek(SeekFrom::Start(sheet_offset + 14))?;
    zip.out.write_all(&crc.to_le_bytes())?;
    zip.out.write_all(&(csize as u32).to_le_bytes())?;
    zip.out.write_all(&(usize_ as u32).to_le_bytes())?;
    zip.out.seek(SeekFrom::Start(end))?;
    zip.entries.push(Entry {
        name,
        crc,
        csize,
        usize: usize_,
        offset: sheet_offset,
    });
    times.sheet = t.elapsed();

    let t = Instant::now();
    zip.small("xl/styles.xml", styles_xml(&order).as_bytes())?;
    zip.finish()?;
    times.rest += t.elapsed();
    Ok((order.len(), times))
}

/// Write to a temporary file next to `path`, then rename it over `path`.
pub fn write_path(im: &Rgb, path: &Path, compact: bool) -> io::Result<(usize, WriteTimes)> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    let tmp = path.with_file_name(format!(
        ".pixheet-{:08x}{:08x}.tmp",
        std::process::id(),
        nanos
    ));
    let result = (|| {
        let file = File::create_new(&tmp)?;
        let out = write_xlsx(im, BufWriter::with_capacity(1 << 20, file), compact)?;
        std::fs::rename(&tmp, path)?;
        Ok(out)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}
