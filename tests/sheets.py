"""Helpers that write an image with the private writer and read the result back."""

import io
import re
import zipfile

from PIL import Image

from pixheet._xlsx import write_xlsx

SHEET = "xl/worksheets/sheet1.xml"
STYLES = "xl/styles.xml"


def write(im: Image.Image, **kwargs) -> zipfile.ZipFile:
    buf = io.BytesIO()
    write_xlsx(im, buf, **kwargs)
    return zipfile.ZipFile(buf)


def part(zf: zipfile.ZipFile, name: str) -> str:
    # xlsxwriter on Windows writes "\r\n" after the XML declaration.
    return zf.read(name).decode().replace("\r\n", "\n")


def drop_cell_refs(sheet_xml: str) -> str:
    """The sheet XML as compact mode writes it: no ``r="A1"`` on the cells."""
    return re.sub(r'<c r="[A-Z]+\d+" ', "<c ", sheet_xml)


def hex_rgb(rgb: tuple[int, int, int]) -> str:
    return bytes(rgb).hex().upper()


class Unseekable(io.RawIOBase):
    """A write-only stream that cannot seek, like a pipe or a socket."""

    def __init__(self):
        self.data = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.data += b
        return len(b)
