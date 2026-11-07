---
name: menu-maker
description: Turn a restaurant's old menu photo or a typed list of dishes and prices into a clean new menu image (A4 print, phone portrait, or table card). Use for 做菜单 / 换菜单 / 菜单排版 / 价目表, and for the Light home's 做菜单 entry.
allowed-tools:
  - question
  - image_gen
  - image_to_pdf
  - share_file
---

# 做菜单

You help a small restaurant owner get a new menu image they can print or post.
The dish list is the product: a wrong price on a printed menu costs the shop
money, so every name and price is confirmed by the owner before any image is
made, and checked again on the image afterwards.

`image_gen` (see the `imagegen` skill for its storage contract and size rules)
draws the menu; `image_to_pdf` wraps the finished image into a printable PDF.
No sandbox is needed; do not create files, run commands or look for templates
on disk.

## Steps

The Light page shows these steps; put the matching `step_id` on every
`question` card.

| step_id | what happens |
|---|---|
| `style` | the look: 简约黑白 / 烟火红 / 手写粉笔 / 深色高级. If the opening card already named one, that is the style; do not ask again |
| `items` | get the dishes: a photo of the old menu, typed lines, or "改上次那版" |
| `review` | one card with the whole dish table for the owner to confirm or correct |
| `layout` | size and extras: A4 打印 / 手机竖图 / 桌贴, shop name on top, a space for the ordering QR code |
| `deliver` | the image, checked, plus the PDF for printing, with "再做一版" options |

Outside Light mode the same order applies; just skip the step ids.

## 1. Get the dishes (`items`)

- **Photo of the old menu**: the image is attached to the person's message.
  Read it yourself: category headings, dish names, prices, the unit (份 / 斤 /
  例 / 杯), and marks such as 招牌 / 推荐 / 辣. Do not guess a price that is
  blurred, cut off or covered: put it in the table as `?` and ask about it in
  the review card.
- **Typed lines**: accept anything from "红烧肉 38" to a pasted list. Split on
  new lines, commas or spaces; a trailing number is the price.
- **"改上次那版"**: the earlier dish table is in this conversation's history.
  If there is none in this conversation, say so in one line and ask for a
  photo or typed lines.
- Several photos are fine (front and back of a menu); merge them and drop
  exact duplicates.

Keep dish names exactly as the shop writes them. Do not translate, add
descriptions, invent dishes, or "round" prices. More than about 40 dishes do
not fit legibly on one image: suggest splitting by category into two menus
and ask which half to do first.

## 2. Review card (`review`)

One `question` card. Put the table in the question text, grouped by category,
one dish per line, e.g.:

```
【热菜】
红烧肉 38 · 招牌
干锅花菜 26
【主食】
米饭 2
```

Options: `就这样，开始排版` / `改几个价格` / `补几道菜` / `标招牌菜`, plus
`custom` for free corrections. Apply every correction, then show the updated
table again only if something changed that the owner has not seen. Unknown
prices (`?`) must be resolved before moving on.

## 3. Size and extras (`layout`)

One card, single choice: `A4 打印（竖版）` / `手机竖图（发朋友圈、贴群里）` /
`桌贴（横版小卡）`. Use the shop name from the assistant profile or the
conversation for the heading when there is one; otherwise ask once with an
option `不写店名`. Leave a plain square near the bottom right labeled
「扫码点餐」 unless the owner says they have no ordering code.

| choice | `size` | notes |
|---|---|---|
| A4 打印 | `2048x2896` | portrait A4 ratio; prints sharp at A4 |
| 手机竖图 | `1152x2048` | 9:16 |
| 桌贴 | `1536x1024` | landscape; keep to the signature dishes if the list is long |

## 4. Make the image (`deliver`)

Call `image_gen` once, `quality: "high"` (dense text needs it), with the size
above. Prompt shape:

```text
Use case: productivity-visual
Asset type: restaurant menu, <A4 print | phone portrait | landscape table card>
Style: <the chosen style, e.g. 烟火红：deep red background, warm gold headings, festive but tidy>
Layout: shop name "<店名>" as the heading; categories as section headings; each line = dish name left, price right with dot leaders; signature dishes marked with a small 「招牌」 badge; a blank square labeled 「扫码点餐」 bottom right
Text (verbatim, render every character exactly; Simplified Chinese):
<the confirmed table, one line per dish, with ¥ before each price>
Constraints: no other text, no invented dishes, no photos of people; prices aligned in one column; generous margins for printing
```

Style notes:

- 简约黑白: white background, black sans-serif, thin rules.
- 烟火红: red background, gold/cream text, bold headings.
- 手写粉笔: dark chalkboard texture, white and pastel hand-lettered look, still legible.
- 深色高级: charcoal or navy background, thin gold lines, serif headings.

After the image comes back, look at it and check **every** dish name and
price against the confirmed table. If anything is wrong, call `image_gen`
once more with the same prompt plus `Fix: <exact lines that were wrong>`. If
the second image is still wrong, deliver the better one and list the wrong
lines in plain words so the owner can tell you to try again; never claim it
is correct when it is not.

## 5. The PDF for printing (`deliver`, same turn)

A copy shop asks for a PDF, not a picture. Once the image passed the check
(or is the better of two attempts), call `image_to_pdf` once with the image's
`asset_id` from the `image_gen` result:

| layout | call |
|---|---|
| A4 打印 | `paper: "a4"`, `name: "<店名>菜单.pdf"` (or `菜单.pdf` without a shop name) |
| 手机竖图 / 桌贴 | `paper: "image"`, same naming |

It costs nothing and needs no sandbox; the PDF lands on the same card as the
image. If it fails, say so in the closing line (「PDF 没导出来，要的话再说一声」)
and move on; never retry `image_gen` because of it.

Finish with one short line and no question card — the image card already
has 保存 / 存 PDF / 再做一条, and an open question would sit in the owner's
to-do list. Mention the other next steps in that line, e.g. 「排好了，菜名和价格
都对过一遍，打印用的 PDF 也在卡片上。要换个样式、改几道菜，或者做一张同款海报，直接说。」
Do not call `share_file` on `image_gen` or `image_to_pdf` results.

When the owner later asks for the PDF of an earlier menu (「给我 PDF」「要打印的」),
call `image_to_pdf` on that image's `asset_id` from this conversation — no new
image, no new review.

## Light mode

When the system prompt says Light mode, one decision per card, options rather
than typing, and never mention model names, image sizes in pixels, quality
levels or credits. The page shows the price from its own configuration
("约 1 积分 / 版"); do not estimate credits yourself.
