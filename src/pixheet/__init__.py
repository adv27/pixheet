"""Turn any image into an .xlsx spreadsheet where every cell is one pixel."""

from pixheet._image import MAX_COLORS
from pixheet._xlsx import MAX_COLUMNS, MAX_ROWS
from pixheet.core import ConvertResult, convert, prepare

__all__ = ["MAX_COLORS", "MAX_COLUMNS", "MAX_ROWS", "ConvertResult", "convert", "prepare"]
