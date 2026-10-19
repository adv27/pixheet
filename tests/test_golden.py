"""Byte-for-byte comparison with xlsxwriter (a dev dependency only)."""

import io
import re
import zipfile

import pytest
import xlsxwriter
from images import gradient, many_colors, noise, palette_with_transparency
from PIL import Image

from pixheet import prepare
from pixheet._xlsx import write_xlsx

CASES = {
    "gradient": lambda: gradient(64, 48),
    "noise": lambda: noise(50, 40, 300),
    "one-pixel": lambda: Image.new("RGB", (1, 1), (1, 2, 3)),
    "one-row": lambda: noise(30, 1, 20, seed=2),
    "one-column": lambda: noise(1, 30, 20, seed=3),
    "palette-transparent": palette_with_transparency,
    "rgba": lambda: Image.new("RGBA", (5, 5), (0, 128, 255, 100)),
    "black-and-white": lambda: Image.new("RGB", (27, 3), (0, 0, 0)),
}


def xlsxwriter_reference(im: Image.Image) -> zipfile.ZipFile:
    """What xlsxwriter writes for ``im``: one format per color, by first appearance."""
    w, h = im.size
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True})
    ws = wb.add_worksheet()
    ws.set_column(0, w - 1, 0.08)
    ws.set_default_row(0.75, hide_unused_rows=True)
    formats = {}
    px = im.load()
    for y in range(h):
        for x in range(w):
            rgb = px[x, y]
            fmt = formats.get(rgb)
            if fmt is None:
                fmt = formats[rgb] = wb.add_format({"bg_color": "#{:02X}{:02X}{:02X}".format(*rgb)})
            ws.write_blank(y, x, None, fmt)
    wb.close()
    return zipfile.ZipFile(buf)


def part(zf: zipfile.ZipFile, name: str) -> str:
    # xlsxwriter on Windows writes "\r\n" after the XML declaration.
    return zf.read(name).decode().replace("\r\n", "\n")


def ours(im: Image.Image, compact: bool = False) -> zipfile.ZipFile:
    buf = io.BytesIO()
    write_xlsx(im, buf, compact=compact)
    return zipfile.ZipFile(buf)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("name", ["xl/worksheets/sheet1.xml", "xl/styles.xml"])
def test_matches_xlsxwriter(case, name):
    im = prepare(CASES[case]())
    assert part(ours(im), name) == part(xlsxwriter_reference(im), name)


def test_matches_xlsxwriter_after_fit():
    # The image is fitted (65,536 colors -> at most 63,999) before both writers see it.
    im = prepare(many_colors())
    expected = xlsxwriter_reference(im)
    got = ours(im)
    for name in ("xl/worksheets/sheet1.xml", "xl/styles.xml"):
        assert part(got, name) == part(expected, name)


def test_compact_is_golden_minus_cell_refs():
    im = prepare(noise(50, 40, 300))
    expected = re.sub(
        r'<c r="[A-Z]+\d+" ', "<c ", part(xlsxwriter_reference(im), "xl/worksheets/sheet1.xml")
    )
    got = ours(im, compact=True)
    assert part(got, "xl/worksheets/sheet1.xml") == expected
    assert part(got, "xl/styles.xml") == part(xlsxwriter_reference(im), "xl/styles.xml")
