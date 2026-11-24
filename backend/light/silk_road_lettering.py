"""Widescreen ivory chapter cards for the Silk Road edition."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from light.media_fonts import latin_font, mixed_font
from light.remake_lettering import FOLDER, _cjk, _has_cjk
from light.silk_road import CANVAS, Plan

SONGTI = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
INK = (248, 246, 238, 255)
WIDTH, HEIGHT = CANVAS["width"], CANVAS["height"]
CHAPTER_TIMES = ((3.18, 6.18), (8.74, 13.48), (16.30, 22.20), (25.56, 30.11),
                 (36.60, 40.83), (48.78, 51.16), (53.95, 55.82))


@lru_cache(maxsize=24)
def font(size):
    if SONGTI.is_file():
        return ImageFont.truetype(SONGTI, size, index=1)
    return ImageFont.truetype(FOLDER / "ZhiMangXing-Regular.ttf", size)


def glyph(character, width, height):
    brush = font(min(width, height) * 2)
    pad = Image.new("L", (width * 2, height * 2))
    ImageDraw.Draw(pad).text((width, height), character, fill=255, font=brush, anchor="mm")
    return pad.resize((width, height), Image.Resampling.LANCZOS)


def text_face(draw, text, size, width):
    family = font if _cjk(text) else mixed_font if _has_cjk(text) else latin_font
    face = family(size)
    if draw.textlength(text, font=face) > width:
        face = family(max(14, int(size * width / draw.textlength(text, font=face))))
    return face


def chapter(words, note, side):
    main = side == "main"
    mask = Image.new("L", (WIDTH, HEIGHT))
    x = round(WIDTH * {"main": 0.069, "left": 0.125, "right": 0.72}[side])
    y = 155 if main else 146
    w, h = (187, 267) if main else (119, 127)
    draw = ImageDraw.Draw(mask)
    width = 605 if main else min(500, WIDTH - x - 45)
    if _has_cjk(words) and len(words) <= 4:
        for index, letter in enumerate(words):
            row, col = divmod(index, 2)
            mask.paste(glyph(letter, w, h), (x + col * (w + 16), y + row * (h + 19)))
        text_y = y + (h if main else 2 * h + 19) + 16
    else:
        face = text_face(draw, words, 72 if main else 51, width)
        draw.text((x, y + 32), words, font=face, fill=255, anchor="lt")
        text_y = y + 52 + face.size
    draw.text((x + 3, text_y), note, font=text_face(draw, note, 26 if main else 21, width),
              fill=224, anchor="lt")
    shadow = Image.new("RGBA", mask.size, (31, 23, 14, 0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(2.1)).point(lambda a: a // 2))
    ink = Image.new("RGBA", mask.size, INK)
    ink.putalpha(mask)
    return Image.alpha_composite(shadow, ink)


def end_card(plan, shop):
    image = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    name = plan.product
    brush = text_face(draw, name, 62, 560) if not _has_cjk(name) else font(min(62, max(30, 560 // min(len(name), 10))))
    rows = [""]
    for letter in name:
        if draw.textlength(rows[-1] + letter, font=brush) > 560:
            rows.append("")
        rows[-1] += letter
    if len(rows) > 1 and len(rows[-1]) == 1:
        rows[-1] = rows[-2][-1] + rows[-1]
        rows[-2] = rows[-2][:-1]
    for index, part in enumerate(rows):
        draw.text((111, 265 + 67 * index), part, font=brush,
                  fill=INK, stroke_width=1, stroke_fill=(61, 40, 27, 220))
    tag_y = 270 + 69 * len(rows)
    x = 111
    if shop:
        label_font = text_face(draw, shop, 24, 425)
        right = x + int(draw.textlength(shop, font=label_font)) + 18
        draw.rounded_rectangle((x, tag_y, right, tag_y + 35), radius=4, fill=(139, 48, 33, 244))
        draw.text((x + 8, tag_y + 17), shop, font=label_font, fill=INK, anchor="lm")
        x = right + 16
    note = plan.note
    for label in (shop, plan.product):
        if label and note.startswith(label):
            note = note.removeprefix(label).lstrip(" ·｜|:：，,、")
    draw.text((x, tag_y + 17), note, font=text_face(draw, note, 22, 550), fill=INK, anchor="lm",
              stroke_width=1, stroke_fill=(61, 40, 27, 195))
    return image


def artwork(plan: Plan, shop):
    if plan.world:
        words = [(cue.title, cue.subtitle) for cue in plan.world.chapters]
    else:
        words = [
            ("初尝入味", "帐中初遇"), ("集市寻味", "灯火与烟尘"),
            ("误买干馕" if "馕" in plan.decoy else "集市误买", "只顾赶路"),
            ("沙中试馕" if "馕" in plan.decoy else "沙中试味", "嚼了又嚼"),
            ("再嚼难咽", "商队同行"), ("码头相赠", "留一份带走"), ("故味入怀", "见礼知味"),
        ]
    if not plan.world and plan.language != "zh":
        words = {
            "vi": [("Vị đầu tiên", "Gặp nhau trong lều"), ("Tìm vị ngon", "Qua khu chợ"),
                   ("Mua nhầm", "Vội lên đường"), ("Nếm thử", "Giữa đường đi"),
                   ("Một thoáng ngập ngừng", "Đi cùng đoàn"), ("Quà ở bến tàu", "Gửi một phần mang theo"),
                   ("Nhớ hương vị", "Vị ngon ở lại")],
            "en": [("First taste", "A meeting in the tent"), ("Looking for flavor", "Through the market"),
                   ("A mix-up", "In a hurry"), ("A bite on the road", "Far from home"),
                   ("A moment of doubt", "With the caravan"), ("A gift at the dock", "One to take along"),
                   ("A taste remembered", "The flavor stays")],
        }[plan.language]
    opening = plan.product[-2:] if plan.language == "zh" and _has_cjk(plan.product) else plan.product
    opening_note = " · ".join(filter(None, (shop, plan.product))) if opening != plan.product else shop
    cards = [(0.16, 2.85, chapter(opening, opening_note, "main"))]
    cards += [(start, end, chapter(title, sub, "right" if i % 2 == 0 else "left"))
              for i, ((start, end), (title, sub)) in enumerate(zip(CHAPTER_TIMES, words, strict=True))]
    cards.append((57.45, 61.75, end_card(plan, shop)))
    result = []
    for start, end, image in cards:
        buf = io.BytesIO()
        image.save(buf, "PNG", optimize=True)
        result.append((start, end, buf.getvalue()))
    return result
