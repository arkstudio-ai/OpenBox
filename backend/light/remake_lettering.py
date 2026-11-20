"""Typeset the restaurant's final advertising card on a transparent 9:16 canvas."""
from __future__ import annotations

import io
import math
import unicodedata
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps

from light.remake_case import ASSETS

FOLDER = ASSETS / "lettering"
INK = (255, 250, 238, 255)
SIZE = (720, 1280)


@lru_cache(maxsize=32)
def _brush(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FOLDER / "ZhiMangXing-Regular.ttf"), size)


def _label(draw: ImageDraw.ImageDraw, xy, text: str, size: int, ink) -> None:
    font = _brush(size)
    x, y = xy
    left, top, _, _ = draw.textbbox((0, 0), text, font=font)
    draw.text((x - left, y - top), text, font=font, fill=ink)


def _cjk(text: str) -> bool:
    return all(unicodedata.east_asian_width(c) in {"W", "F"} for c in text if not c.isspace())


def _headline(text: str, ink=INK) -> Image.Image:
    if text.rstrip("。！! ") == "趁热吃":
        with Image.open(FOLDER / "趁热吃.png") as matte:
            mask = matte.copy()
        art = Image.new("RGBA", mask.size, ink)
        art.putalpha(mask)
        return art

    art = Image.new("RGBA", (420, 540))
    draw = ImageDraw.Draw(art)
    # Chinese is read down each column, starting at the right. A stagger in
    # column height gives the line the rhythm of the lettering in the source.
    if _cjk(text):
        text = text.replace(" ", "")
        cols = max(1, math.ceil(len(text) / 5))
        rows = math.ceil(len(text) / cols)
        em = min(144, 462 // rows, 370 // cols)
        for index, char in enumerate(text):
            col, row = divmod(index, rows)
            x = 414 - (col + 1) * em - (row % 2) * int(em * 0.19)
            y = 15 + row * em + (col % 2) * int(em * 0.35)
            _label(draw, (x, y), char, em, ink)
    else:
        words = text.split()
        em = max(25, min(117, 700 // max(2, len(max(words, key=len)))))
        lines = [""]
        for word in words:
            candidate = (lines[-1] + " " + word).strip()
            if lines[-1] and draw.textlength(candidate, font=_brush(em)) > 407:
                lines.append(word)
            else:
                lines[-1] = candidate
        for i, line in enumerate(lines):
            size = min(em, int(em * 405 / max(1, draw.textlength(line, font=_brush(em)))))
            _label(draw, (4, i * em + 8), line, max(12, size), ink)
    return art.crop(art.getbbox() or (0, 0, 1, 1))


def end_card(headline: str, shop: str, *, headline_ink=INK) -> bytes:
    canvas = Image.new("RGBA", SIZE)
    art = ImageOps.contain(_headline(headline, headline_ink), (292, 523), method=Image.Resampling.LANCZOS)
    left, top = 675 - art.width, 53
    canvas.alpha_composite(art, (left, top))
    if shop.strip():
        shop = shop.strip()
        draw = ImageDraw.Draw(canvas)
        if len(shop) <= 8 and _cjk(shop):
            # Vermilion shop seal, italic brush lettering remains the focus.
            em, pad = 35, 8
            w, h = em + 2*pad, len(shop) * em + 2*pad
            x, y = left - w - 15, top + art.height - h - 12
            draw.rounded_rectangle((x, y, x+w, y+h), radius=6, fill=(157, 47, 32, 255))
            draw.rounded_rectangle((x+3, y+3, x+w-3, y+h-3), radius=4, outline=(236, 161, 120, 255), width=1)
            for i, char in enumerate(shop):
                bounds = draw.textbbox((0, 0), char, font=_brush(em))
                width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                _label(draw, (x+(w-width)//2, y+pad+i*em+(em-height)//2), char, em, INK)
        else:
            # Long or mixed-script shop names use a separate horizontal signature.
            chars_per_line = min(14, len(shop))
            em = max(17, min(31, 315 // chars_per_line))
            for i in range(0, len(shop), chars_per_line):
                line = shop[i:i+chars_per_line]
                _label(draw, (45, 60 + (i//chars_per_line)*em), line, em, headline_ink)
    stream = io.BytesIO()
    canvas.save(stream, format="PNG", optimize=True)
    return stream.getvalue()
