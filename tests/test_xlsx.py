import io
import re
import zipfile
from xml.etree import ElementTree

import pytest
from images import noise
from openpyxl.utils import get_column_letter
from PIL import Image
from sheets import SHEET, STYLES, Unseekable, part, write

from pixheet._xlsx import column_name, styles_xml, write_xlsx

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PARTS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    SHEET,
    STYLES,
]


@pytest.mark.parametrize(
    ("index", "name"),
    [
        (0, "A"),
        (1, "B"),
        (25, "Z"),
        (26, "AA"),
        (51, "AZ"),
        (52, "BA"),
        (701, "ZZ"),
        (702, "AAA"),
        (16383, "XFD"),
    ],
)
def test_column_name(index, name):
    assert column_name(index) == name


def test_column_names_match_openpyxl():
    assert [column_name(i) for i in range(16384)] == [
        get_column_letter(i + 1) for i in range(16384)
    ]


def test_column_name_rejects_negative():
    with pytest.raises(ValueError, match=">= 0"):
        column_name(-1)


def test_parts_and_wellformed_xml(rgb_gradient):
    zf = write(rgb_gradient)
    assert zf.namelist() == PARTS
    assert zf.testzip() is None
    assert {info.compress_type for info in zf.infolist()} == {zipfile.ZIP_DEFLATED}
    for name in PARTS:
        ElementTree.fromstring(zf.read(name))


def test_package_wiring(rgb_gradient):
    zf = write(rgb_gradient)
    ct = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    types = ElementTree.fromstring(zf.read("[Content_Types].xml"))
    sheet_ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    assert {o.get("PartName"): o.get("ContentType") for o in types.iter(f"{ct}Override")} == {
        "/xl/workbook.xml": f"{sheet_ct}.sheet.main+xml",
        f"/{SHEET}": f"{sheet_ct}.worksheet+xml",
        f"/{STYLES}": f"{sheet_ct}.styles+xml",
    }
    rel = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
    doc = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    root_rels = ElementTree.fromstring(zf.read("_rels/.rels")).findall(rel)
    assert [(r.get("Type"), r.get("Target")) for r in root_rels] == [
        (f"{doc}/officeDocument", "xl/workbook.xml")
    ]
    wb_rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels")).findall(rel)
    assert {r.get("Type"): r.get("Target") for r in wb_rels} == {
        f"{doc}/worksheet": "worksheets/sheet1.xml",
        f"{doc}/styles": "styles.xml",
    }
    sheet = ElementTree.fromstring(zf.read("xl/workbook.xml")).find("m:sheets/m:sheet", NS)
    rid = sheet.get(f"{{{doc}}}id")
    assert {r.get("Id"): r.get("Target") for r in wb_rels}[rid] == "worksheets/sheet1.xml"


def test_sheet_layout(rgb_noise):
    w, h = rgb_noise.size
    sheet = part(write(rgb_noise), SHEET)
    assert f'<dimension ref="A1:{column_name(w - 1)}{h}"/>' in sheet
    # One column definition covering exactly the used columns (not w + 1).
    assert f'<col min="1" max="{w}" width="0.140625" customWidth="1"/>' in sheet
    assert '<sheetFormatPr defaultRowHeight="0.75" customHeight="1" zeroHeight="1"/>' in sheet
    rows = re.findall(r'<row r="(\d+)" ht="0.75" customHeight="1">', sheet)
    assert rows == [str(r) for r in range(1, h + 1)]
    assert sheet.count("<c ") == w * h
    root = ElementTree.fromstring(sheet)
    first_row = root.find("m:sheetData/m:row", NS)
    assert [c.get("r") for c in first_row][:3] == ["A1", "B1", "C1"]


def test_single_cell_dimension(rgb_one_pixel):
    sheet = part(write(rgb_one_pixel), SHEET)
    assert '<dimension ref="A1"/>' in sheet
    assert '<c r="A1" s="1"/>' in sheet


def test_styles_follow_first_appearance():
    im = Image.new("RGB", (3, 2))
    im.putdata([(1, 2, 3), (255, 0, 16), (1, 2, 3), (0, 0, 0), (255, 0, 16), (0, 0, 0)])
    zf = write(im)
    assert re.findall(r' s="(\d+)"', part(zf, SHEET)) == ["1", "2", "1", "3", "2", "3"]
    styles = ElementTree.fromstring(zf.read(STYLES))
    fills = styles.findall("m:fills/m:fill", NS)
    assert styles.find("m:fills", NS).get("count") == "5"
    assert [f[0].get("patternType") for f in fills] == ["none", "gray125"] + ["solid"] * 3
    assert [f.find("m:patternFill/m:fgColor", NS).get("rgb") for f in fills[2:]] == [
        "FF010203",
        "FFFF0010",
        "FF000000",
    ]
    xfs = styles.findall("m:cellXfs/m:xf", NS)
    assert styles.find("m:cellXfs", NS).get("count") == "4"
    assert [xf.get("fillId") for xf in xfs] == ["0", "2", "3", "4"]
    assert [xf.get("applyFill") for xf in xfs] == [None, "1", "1", "1"]


def test_style_count_matches_getcolors():
    # Guards the padding byte of _KEY_LAYOUT: equal colors must share one key and one style.
    im = noise(64, 64, 1000, seed=3)
    styles = ElementTree.fromstring(write(im).read(STYLES))
    assert len(styles.findall("m:cellXfs/m:xf", NS)) - 1 == len(im.getcolors(64 * 64)) == 1000


def test_returns_color_count(rgb_noise):
    assert write_xlsx(rgb_noise, io.BytesIO()) == 300


def test_styles_xml_empty():
    assert '<fills count="2">' in styles_xml({})
    assert '<cellXfs count="1">' in styles_xml({})


@pytest.mark.parametrize("mode", ["RGBA", "L", "P", "CMYK"])
def test_rejects_non_rgb(mode):
    with pytest.raises(ValueError, match="expected an RGB image"):
        write_xlsx(Image.new(mode, (2, 2)), io.BytesIO())


def test_rejects_oversized():
    with pytest.raises(ValueError, match="does not fit"):
        write_xlsx(Image.new("RGB", (16385, 1)), io.BytesIO())


@pytest.mark.parametrize("zip64", [False, True], ids=["normal", "zip64"])
@pytest.mark.parametrize("dst_type", [io.BytesIO, Unseekable])
def test_destinations(rgb_gradient, monkeypatch, dst_type, zip64):
    if zip64:
        # A tiny sheet now counts as "over 2 GiB", so the zip64 path runs.
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 50_000)
    zf = write(rgb_gradient, dst_type())
    assert zf.namelist() == PARTS
    assert zf.testzip() is None
    if zip64:
        # Excel rejects a zip64 entry followed by a data descriptor (flag bit 3), which
        # is what streaming to an unseekable file would give.
        assert zf.getinfo(SHEET).flag_bits & 0x08 == 0
