"""A minimal, streaming .xlsx writer: one sheet, one blank cell per pixel.

The XML is written by hand instead of through xlsxwriter, which spends ~15 us per
cell and ~60 us per format. ``sheet1.xml`` and ``styles.xml`` are byte-identical to
what xlsxwriter writes for the same image (see tests/test_golden.py); the theme and
docProps parts it adds are optional and left out.
"""

import os
import shutil
import sys
import tempfile
import zipfile
from array import array
from typing import IO

from PIL import Image

# One pixel per array item; the key scheme below relies on 4-byte items.
assert array("I").itemsize == 4

MAX_COLUMNS = 16_384
MAX_ROWS = 1_048_576

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
SHEET_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml"

CONTENT_TYPES_XML = (
    f"{XML_DECL}"
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/xl/workbook.xml" ContentType="{SHEET_CT}.sheet.main+xml"/>'
    f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{SHEET_CT}.worksheet+xml"/>'
    f'<Override PartName="/xl/styles.xml" ContentType="{SHEET_CT}.styles+xml"/>'
    "</Types>"
)
ROOT_RELS_XML = (
    f'{XML_DECL}<Relationships xmlns="{PKG_REL}">'
    f'<Relationship Id="rId1" Type="{DOC_REL}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
WORKBOOK_XML = (
    f'{XML_DECL}<workbook xmlns="{MAIN_NS}" xmlns:r="{DOC_REL}">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
)
WORKBOOK_RELS_XML = (
    f'{XML_DECL}<Relationships xmlns="{PKG_REL}">'
    f'<Relationship Id="rId1" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{DOC_REL}/styles" Target="styles.xml"/>'
    "</Relationships>"
)
# Columns and rows are ~1 px; rows without data are hidden.
SHEET_HEADER_XML = (
    f'{XML_DECL}<worksheet xmlns="{MAIN_NS}" xmlns:r="{DOC_REL}">'
    '<dimension ref="{ref}"/>'
    '<sheetViews><sheetView tabSelected="1" workbookViewId="0"/></sheetViews>'
    '<sheetFormatPr defaultRowHeight="0.75" customHeight="1" zeroHeight="1"/>'
    '<cols><col min="1" max="{width}" width="0.140625" customWidth="1"/></cols>'
    "<sheetData>"
)
SHEET_FOOTER_XML = (
    "</sheetData>"
    '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    "</worksheet>"
)
STYLES_XML = (
    f'{XML_DECL}<styleSheet xmlns="{MAIN_NS}">'
    '<fonts count="1"><font><sz val="11"/><color theme="1"/><name val="Calibri"/>'
    '<family val="2"/><scheme val="minor"/></font></fonts>'
    '<fills count="{fill_count}"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>{fills}</fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="{xf_count}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    "{xfs}</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '<dxfs count="0"/>'
    '<tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>'
    "</styleSheet>"
)

# The longest cell and row markup. They tell if the sheet part can be larger than 2 GiB.
_MAX_CELL_BYTES = len('<c r="XFD1048576" s="63999"/>')
_MAX_ROW_BYTES = len('<row r="1048576" ht="0.75" customHeight="1"></row>')


def column_name(index: int) -> str:
    """Return the column letters for a 0-based column index (0 -> "A", 16383 -> "XFD")."""
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def styles_xml(keys) -> str:
    """Build styles.xml for pixel keys numbered 1, 2, ... in iteration order.

    Fills 0 and 1 are reserved (``none`` and ``gray125``) and format 0 is the default
    style, so color ``i`` (1-based) uses ``fillId = i + 1`` and is format ``i``.
    """
    byteorder = sys.byteorder
    fills = "".join(
        [
            '<fill><patternFill patternType="solid">'
            f'<fgColor rgb="FF{key.to_bytes(4, byteorder)[:3].hex().upper()}"/>'
            '<bgColor indexed="64"/></patternFill></fill>'
            for key in keys
        ]
    )
    count = len(keys)
    xfs = "".join(
        [
            f'<xf numFmtId="0" fontId="0" fillId="{i + 1}" borderId="0" xfId="0" applyFill="1"/>'
            for i in range(1, count + 1)
        ]
    )
    return STYLES_XML.format(fill_count=count + 2, fills=fills, xf_count=count + 1, xfs=xfs)


def _seekable(f: IO[bytes]) -> bool:
    try:
        f.seek(f.tell())  # the same probe ZipFile uses
    except AttributeError, OSError:
        return False
    return True


def write_xlsx(
    im: Image.Image, dst: str | os.PathLike[str] | IO[bytes], *, compact: bool = False
) -> int:
    """Write ``im`` (mode RGB) as an .xlsx with one filled cell per pixel.

    ``dst`` is a path or a writable binary file object. With ``compact=True`` the
    optional ``r="A1"`` cell references are left out. Returns the number of colors.
    """
    if im.mode != "RGB":
        raise ValueError(f"expected an RGB image, got mode {im.mode}")
    w, h = im.size
    if not (0 < w <= MAX_COLUMNS and 0 < h <= MAX_ROWS):
        raise ValueError(f"{w}x{h} does not fit a sheet ({MAX_COLUMNS}x{MAX_ROWS} at most)")

    # One int per pixel. RGBX pads every pixel with the same byte, so equal colors
    # always get equal keys.
    pixels = array("I", im.convert("RGBX").tobytes())
    columns = [column_name(x) for x in range(w)]
    last_cell = f"{columns[-1]}{h}"
    # Pixel key -> style id, numbered by first appearance.
    style_ids: dict[int, int] = {}
    style_id = style_ids.setdefault
    zip64 = w * h * _MAX_CELL_BYTES + h * _MAX_ROW_BYTES > zipfile.ZIP64_LIMIT
    if zip64 and not isinstance(dst, (str, os.PathLike)) and not _seekable(dst):
        # A stream that cannot seek gets a zip64 entry with a data descriptor, and
        # Excel refuses those. Build the file on disk, then copy it out.
        with tempfile.TemporaryFile() as tmp:
            colors = write_xlsx(im, tmp, compact=compact)
            tmp.seek(0)
            shutil.copyfileobj(tmp, dst)
        return colors

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", WORKBOOK_XML)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS_XML)

        with zf.open("xl/worksheets/sheet1.xml", "w", force_zip64=zip64) as sheet:
            write = sheet.write
            ref = "A1" if last_cell == "A1" else f"A1:{last_cell}"
            write(SHEET_HEADER_XML.format(ref=ref, width=w).encode())
            for y in range(h):
                row = pixels[y * w : (y + 1) * w]
                r = y + 1
                if compact:
                    cells = "".join([f'<c s="{style_id(p, len(style_ids) + 1)}"/>' for p in row])
                else:
                    cells = "".join(
                        [
                            f'<c r="{col}{r}" s="{style_id(p, len(style_ids) + 1)}"/>'
                            for col, p in zip(columns, row, strict=True)
                        ]
                    )
                write(f'<row r="{r}" ht="0.75" customHeight="1">{cells}</row>'.encode())
            write(SHEET_FOOTER_XML.encode())

        # Written after the sheet, once every color is known.
        zf.writestr("xl/styles.xml", styles_xml(style_ids))
    return len(style_ids)
