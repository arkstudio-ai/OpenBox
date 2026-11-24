---
name: menu-maker
description: Turn a restaurant's old menu photo or a typed list of dishes and prices into a clean new menu image (A4 print, phone portrait, or table card). Use for 做菜单 / 换菜单 / 菜单排版 / 价目表, and for the Light home's 做菜单 entry.
allowed-tools:
  - question
  - example_pick
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
| `example` | the owner picks which finished menu this one should look like (the `example_pick` card; you send no card of your own) |
| `items` | get the dishes: a photo of the old menu, typed lines, or "改上次那版" |
| `review` | one card with the whole dish table for the owner to confirm or correct |
| `layout` | size and extras: A4 打印 / 手机竖图 / 桌贴, shop name on top, a space for the ordering QR code |
| `deliver` | the image, checked, plus the PDF for printing, with "再做一版" options |

Outside Light mode the same order applies; just skip the step ids.

## 0. The look (`style`, `example`)

The style is known when the opening card named one; otherwise ask once on
`style` with the four as options. As soon as it is known — on your first turn
when the opening card named it, even if the owner's first message already
carries the menu photo — call `example_pick` once with the style as `scene`:
简约黑白 → `menu_mono`, 烟火红 → `menu_red`, 手写粉笔 → `menu_chalk`,
深色高级 → `menu_dark`. The tool shows the owner up to three finished menus
and waits for the pick; write nothing about them yourself.

- **An example was picked**: it is the look of this menu. The tool result
  carries 参考卡 (written after looking at the finished piece; an older
  example may carry the shorter 样子说明 instead) — its 版式, 配色, 字体, 图片,
  装饰 and how names and prices are set. Write the prompt's `Style:` and
  `Layout:` lines from those sections, all of them: its palette even when it is not the named
  style's (the owner chose it with their eyes), its columns or cards rather
  than the default list, its badges and frames. When its 图片 line has dish
  photos or illustrations, ask for one per listed dish in that same shape
  and place, and for no dish that is not on the list. Only the look: every
  dish, price and the shop name are this owner's, never the example's. Say
  one short line (「好，照「<样例标题>」的样子排。」) and go on to the dishes.
  The card's 文案结构 is levels only (sections, names, prices), never wording.
  If the result has neither, take the look from what it does give.
- **No example** (the tool says none applies, or the owner chose none): use
  the named style's note below and do not mention examples.

Do not call `example_pick` again in the same conversation unless the owner
asks for a different look: then call it with `again: true`.

## 1. Get the dishes (`items`)

- **Photo of the old menu**: the image is attached to the person's message.
  Read it yourself: category headings, dish names, prices, the unit (份 / 斤 /
  例 / 杯), and marks such as 招牌 / 推荐 / 辣. Do not guess a price that is
  blurred, cut off or covered: put it in the table as `?` and ask about it in
  the review card.
- **Typed lines**: accept anything from "红烧肉 38" to a pasted list. Split on
  new lines or clear item separators. Read a trailing price with its original
  currency and numeric separators, such as `45.000 ₫`; do not split inside it.
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
option in the session language to omit the shop name. Reserve a square for
an ordering QR code unless the owner says there is none. Label it in the
artwork language.

| choice | `size` | notes |
|---|---|---|
| A4 打印 | `1712x2416` | portrait A4 ratio; about 200 dpi on A4, and inside the image model's size limit (a larger A4 such as 2048x2896 is refused) |
| 手机竖图 | `1152x2048` | 9:16 |
| 桌贴 | `1536x1024` | landscape; keep to the signature dishes if the list is long |

## 4. Make the image (`deliver`)

Call `image_gen` once, `quality: "high"` (dense text needs it), with the size
above. Prompt shape:

```text
Use case: productivity-visual
Asset type: restaurant menu, <A4 print | phone portrait | landscape table card>
Style: <the picked example's look in your own words, or without one the chosen style, e.g. 烟火红：deep red background, warm gold headings, festive but tidy>
Layout: <the picked example's layout from 参考卡 or 样子说明, or without one: shop name "<店名>" as the heading; categories as section headings; each line = dish name left, price right with dot leaders>; signature dishes may have a small badge in the artwork language; a reserved ordering QR area unless the owner says there is none, labeled in the artwork language
Text (render each approved line verbatim; use the session language or the owner-requested language for new labels; preserve Vietnamese diacritics):
<the confirmed table, one line per dish, with its exact price, optional currency symbol, and separators>
Constraints: no other text, no invented dishes, no photos of people; prices aligned in one column; generous margins for printing
```

Style notes:

- 简约黑白: white background, black sans-serif, thin rules.
- 烟火红: red background, gold/cream text, bold headings.
- 手写粉笔: dark chalkboard texture, white and pastel hand-lettered look, still legible.
- 深色高级: charcoal or navy background, thin gold lines, serif headings.

After the image comes back, look at it and check **every** dish name and
price against the confirmed table. If anything is wrong, call `image_gen`
once more with the same prompt — the same `Style:` and `Layout:` lines, so
the picked look is not lost on the way — plus `Fix: <exact lines that were wrong>`. If
the second image is still wrong, deliver the better one and list the wrong
lines in plain words so the owner can tell you to try again; never claim it
is correct when it is not.

## 5. The PDF for printing (`deliver`, same turn)

A copy shop asks for a PDF, not a picture. Once the image passed the check
(or is the better of two attempts), call `image_to_pdf` once with the image's
`asset_id` from the `image_gen` result:

| layout | call |
|---|---|
| A4 打印 | `paper: "a4"`, a localized menu filename, optionally prefixed by the shop name (e.g. `Bếp Nhà - Thực đơn.pdf`) |
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
