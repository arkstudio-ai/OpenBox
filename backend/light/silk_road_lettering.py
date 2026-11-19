"""Widescreen ivory chapter cards for the Silk Road edition."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from light.remake_lettering import FOLDER
from light.silk_road import CANVAS, Plan

SONGTI = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
INK = (248, 246, 238, 255)
WIDTH, HEIGHT = CANVAS["width"], CANVAS["height"]


@lru_cache(maxsize=24)
def font(size):
    if SONGTI.is_file():
        return ImageFont.truetype(SONGTI, size, index=1)
    return ImageFont.truetype(FOLDER / "ZhiMangXing-Regular.ttf", size)


def glyph(character, width, height):
    brush = font(height * 2)
    left, top, right, bottom = brush.getbbox(character)
    pad = Image.new("L", (right - left + 4, bottom - top + 4))
    ImageDraw.Draw(pad).text((2 - left, 2 - top), character, fill=255, font=brush)
    return pad.crop(pad.getbbox() or (0, 0, 1, 1)).resize((width, height), Image.Resampling.LANCZOS)


def chapter(words, note, side):
    main = side == "main"
    mask = Image.new("L", (WIDTH, HEIGHT))
    x = round(WIDTH * {"main": 0.069, "left": 0.125, "right": 0.72}[side])
    y = 155 if main else 146
    w, h = (187, 267) if main else (119, 127)
    for index, letter in enumerate(words):
        row, col = divmod(index, 2)
        mask.paste(glyph(letter, w, h), (x + col * (w + 16), y + row * (h + 19)))
    text_y = y + (h if main else 2 * h + 19) + 16
    ImageDraw.Draw(mask).text((x + 3, text_y), note, font=font(26 if main else 21), fill=224, anchor="lt")
    shadow = Image.new("RGBA", mask.size, (31, 23, 14, 0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(2.1)).point(lambda a: a // 2))
    ink = Image.new("RGBA", mask.size, INK)
    ink.putalpha(mask)
    return Image.alpha_composite(shadow, ink)


def end_card(plan, shop):
    image = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    name = plan.product
    # The longer dish line wraps in the same spot as the original art.
    split = 4 if 5 <= len(name) <= 8 else (len(name) + 1) // 2
    rows = [name] if len(name) <= 4 else [name[:split], name[split:]]
    for index, part in enumerate(rows):
        draw.text((111, 265 + 67 * index), part, font=font(min(62, 550 // max(1, len(part)))),
                  fill=INK, stroke_width=1, stroke_fill=(61, 40, 27, 220))
    tag_y = 270 + 69 * len(rows)
    x = 111
    if shop:
        label_font = font(min(24, 440 // max(1, len(shop))))
        right = x + int(draw.textlength(shop, font=label_font)) + 18
        draw.rounded_rectangle((x, tag_y, right, tag_y + 35), radius=4, fill=(139, 48, 33, 244))
        draw.text((x + 8, tag_y + 17), shop, font=label_font, fill=INK, anchor="lm")
        x = right + 16
    draw.text((x, tag_y + 17), plan.note, font=font(22), fill=INK, anchor="lm",
              stroke_width=1, stroke_fill=(61, 40, 27, 195))
    return image


def artwork(plan: Plan, shop):
    cards = [
        (0.16, 2.85, chapter(plan.product[-2:], " · ".join(filter(None, (shop, plan.product))), "main")),
        (3.18, 6.18, chapter("初尝入味", "帐中初遇", "right")),
        (8.74, 13.48, chapter("集市寻味", "灯火与烟尘", "left")),
        (16.30, 22.20, chapter("误买干馕" if "馕" in plan.decoy else "集市误买", "只顾赶路", "right")),
        (25.56, 30.11, chapter("沙中试馕" if "馕" in plan.decoy else "沙中试味", "嚼了又嚼", "left")),
        (36.60, 40.83, chapter("再嚼难咽", "商队同行", "right")),
        (48.78, 51.16, chapter("码头相赠", "留一份带走", "left")),
        (53.95, 55.82, chapter("故味入怀", "见礼知味", "right")),
        (57.45, 61.75, end_card(plan, shop)),
    ]
    result = []
    for start, end, image in cards:
        buf = io.BytesIO()
        image.save(buf, "PNG", optimize=True)
        result.append((start, end, buf.getvalue()))
    return result
