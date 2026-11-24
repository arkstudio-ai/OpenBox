"""Latin typography with full Vietnamese diacritics on the worker and on macOS."""

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_LATIN_PATHS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)


@lru_cache(maxsize=32)
def latin_font(size):
    path = next((candidate for candidate in _LATIN_PATHS if candidate.is_file()), None)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


_MIXED_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
)


@lru_cache(maxsize=32)
def mixed_font(size):
    path = next((candidate for candidate in _MIXED_PATHS if candidate.is_file()), None)
    return ImageFont.truetype(path, size) if path else latin_font(size)
