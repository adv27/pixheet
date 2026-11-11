//! The zip archive of the package, written by hand as Python's `zipfile` writes it for a
//! file that can seek: no data descriptors, and zip64 only for a member that can be
//! larger than 2 GiB.
//!
//! Each chunk of a member has its own DEFLATE compressor, with the 32 KiB before the
//! chunk as its preset dictionary (the pigz method, as in `pixheet._threads`). The
//! members decompress to the same bytes as one stream.

use std::io::{self, Seek, Write};

use flate2::{Compress, Compression, FlushCompress, Status};
use rayon::prelude::*;

use super::{CHUNK, WINDOW};

const ZIP_FILECOUNT_LIMIT: usize = (1 << 16) - 1;

/// The DEFLATE level. zipfile uses 6. With zlib-rs, level 3 compresses about 2 times
/// faster, and the files are only 2 to 3 % larger. Read docs/rust-port/PROFILE.md.
const LEVEL: u32 = 3;

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
pub(super) fn compress_chunks(
    chunks: &[&[u8]],
    tail: &[u8],
    last: bool,
) -> Vec<(Vec<u8>, crc32fast::Hasher)> {
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

/// A member of the archive, for the central directory.
pub(super) struct Entry {
    pub(super) name: &'static str,
    pub(super) crc: u32,
    pub(super) csize: u64,
    pub(super) usize: u64,
    pub(super) offset: u64,
}

/// The "version made by" system: 0 for Windows, 3 for Unix, as zipfile writes it.
const CREATE_SYSTEM: u16 = if cfg!(windows) { 0 } else { 3 };

/// The archive as it goes out to `out`. `pos` is the number of bytes written.
pub(super) struct ZipOut<W: Write + Seek> {
    pub(super) out: W,
    pub(super) entries: Vec<Entry>,
    pub(super) pos: u64,
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
    pub(super) fn new(out: W, limit: u64) -> Self {
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

    pub(super) fn put(&mut self, b: &[u8]) -> io::Result<()> {
        self.out.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }

    /// The local file header. With `zip64`, the sizes are in a zip64 extra field.
    pub(super) fn local_header(
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
    pub(super) fn member(&mut self, name: &'static str, data: &[u8]) -> io::Result<()> {
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
    pub(super) fn finish(mut self) -> io::Result<W> {
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
