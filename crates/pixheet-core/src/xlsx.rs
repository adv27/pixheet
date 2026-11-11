//! A port of `pixheet._xlsx`: a streaming .xlsx writer, one blank cell for each pixel.
//!
//! `sheet1.xml` and `styles.xml` are byte-identical to the output of the Python version,
//! and thus to the output of xlsxwriter. The child module `xml` makes the text of the
//! parts, and `archive` writes the zip by hand, as Python's `zipfile` does.
//!
//! The sheet XML goes out in chunks of about CHUNK bytes. The rayon pool makes the text
//! of the chunks, then compresses them.

mod archive;
mod xml;

use std::io::{self, Seek, SeekFrom, Write};
use std::ops::ControlFlow;

use rayon::prelude::*;

use crate::resample::Pixels;
use archive::{Entry, ZipOut, compress_chunks};
use xml::{
    SHEET_FOOTER_XML, column_name, content_types_xml, root_rels_xml, rows_xml, sheet_header_xml,
    style_ids, styles_xml, workbook_rels_xml, workbook_xml,
};

/// Excel's column limit (A to XFD).
pub const MAX_COLUMNS: usize = 16_384;
/// Excel's row limit.
pub const MAX_ROWS: usize = 1_048_576;
/// Excel allows 64,000 cell formats, and format 0 is the default style.
pub const MAX_COLORS: usize = 63_999;
/// `zipfile.ZIP64_LIMIT`: a larger member or offset needs zip64.
const ZIP64_LIMIT: u64 = (1 << 31) - 1;

const CHUNK: usize = 256 * 1024;
const WINDOW: usize = 32 * 1024;

/// The longest cell and row markup. They tell if the sheet part can be larger than 2 GiB.
const MAX_CELL_BYTES: usize = "<c r=\"XFD1048576\" s=\"63999\"/>".len();
const MAX_ROW_BYTES: usize = "<row r=\"1048576\" ht=\"0.75\" customHeight=\"1\"></row>".len();

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

    /// A file on a disk that is full after `left` bytes. Then each write and seek fails.
    struct FullDisk {
        file: Cursor<Vec<u8>>,
        left: usize,
    }

    impl Write for FullDisk {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            if self.left == 0 {
                return Err(io::Error::other("disk full"));
            }
            let n = buf.len().min(self.left);
            self.left -= n;
            self.file.write(&buf[..n])
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    impl Seek for FullDisk {
        fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
            if self.left == 0 {
                return Err(io::Error::other("disk full"));
            }
            self.file.seek(pos)
        }
    }

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
    fn a_large_sheet_spans_many_chunks() {
        // About 1.5 MB of text. A window has 2 chunks for each thread, and a chunk has 18
        // rows of this image. With one thread, the rows 37 and 73 start a new window,
        // which uses the text before it as its preset dictionary.
        let im = image(700, 100, &[[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]);
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(1)
            .build()
            .unwrap();
        let mut zip = pool.install(|| write(&im, false, ZIP64_LIMIT));
        let sheet = part(&mut zip, "xl/worksheets/sheet1.xml");
        assert!(sheet.ends_with("</worksheet>"));
        assert_eq!(sheet.matches("<row ").count(), 100);
        assert!(sheet.contains("<c r=\"A37\" s=\"1\"/>"));
        assert!(sheet.contains("<c r=\"A73\" s=\"1\"/>"));
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
        // The end record and the locator of zip64.
        let has = |bytes: &[u8], signature: &[u8]| bytes.windows(4).any(|w| w == signature);
        let bytes = zip.into_inner().into_inner();
        assert!(has(&bytes, b"PK\x06\x06") && has(&bytes, b"PK\x06\x07"));
        let bytes = write(&im, false, ZIP64_LIMIT).into_inner().into_inner();
        assert!(!has(&bytes, b"PK\x06\x06") && !has(&bytes, b"PK\x06\x07"));
    }

    #[test]
    fn a_full_disk_stops_the_writer_with_its_error() {
        let im = image(3, 2, &[[1, 2, 3], [4, 5, 6]]);
        let write = |left: usize| {
            let mut disk = FullDisk {
                file: Cursor::new(Vec::new()),
                left,
            };
            let result = write_xlsx(&im, &mut disk, false, &mut |_, _| ControlFlow::Continue(()));
            (result, disk.file.into_inner())
        };
        let (result, file) = write(usize::MAX);
        assert_eq!(result.unwrap(), 2);
        // Each size below the size of the file stops a different write or seek.
        for left in 0..file.len() {
            match write(left) {
                (Err(WriteError::Io(e)), _) => assert_eq!(e.to_string(), "disk full"),
                (other, _) => panic!("{left} bytes: {other:?}"),
            }
        }
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
    fn a_stop_before_the_first_row_cancels_the_writer() {
        let im = image(3, 5, &[[1, 2, 3]]);
        let mut calls = 0;
        let result = write_xlsx(&im, Cursor::new(Vec::new()), false, &mut |_, _| {
            calls += 1;
            ControlFlow::Break(())
        });
        assert!(matches!(result, Err(WriteError::Cancelled)));
        assert_eq!(calls, 1);
    }

    #[test]
    fn a_sheet_larger_than_the_grid_fails() {
        let im = Pixels::new(MAX_COLUMNS + 1, 1);
        let result = write_xlsx(&im, Cursor::new(Vec::new()), false, &mut |_, _| {
            ControlFlow::Continue(())
        });
        assert!(matches!(result, Err(WriteError::Invalid(_))));
    }
}
