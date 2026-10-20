"""Byte-for-byte comparison with xlsxwriter (a dev dependency only)."""

import io
import zipfile

import pytest
import xlsxwriter
from images import SAMPLES, noise
from PIL import Image
from sheets import SHEET, STYLES, drop_cell_refs, hex_rgb, part, write

from pixheet import prepare

CASES = {
    **SAMPLES,  # many-colors is fitted (65,536 -> at most 63,999 colors) first
    "one-row": lambda: noise(30, 1, 20, seed=2),
    "one-column": lambda: noise(1, 30, 20, seed=3),
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
                fmt = formats[rgb] = wb.add_format({"bg_color": f"#{hex_rgb(rgb)}"})
            ws.write_blank(y, x, None, fmt)
    wb.close()
    return zipfile.ZipFile(buf)


@pytest.mark.parametrize("case", CASES)
def test_matches_xlsxwriter(case):
    # Both writers get the same fitted image.
    im = prepare(CASES[case]())
    got, expected = write(im), xlsxwriter_reference(im)
    assert part(got, SHEET) == part(expected, SHEET)
    assert part(got, STYLES) == part(expected, STYLES)


def test_compact_is_golden_minus_cell_refs():
    im = prepare(SAMPLES["noise"]())
    got, expected = write(im, compact=True), xlsxwriter_reference(im)
    assert part(got, SHEET) == drop_cell_refs(part(expected, SHEET))
    assert part(got, STYLES) == part(expected, STYLES)
