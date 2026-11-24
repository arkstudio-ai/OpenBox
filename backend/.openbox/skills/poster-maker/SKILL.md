---
name: poster-maker
description: Make a promotion poster for a small restaurant or shop — new dish, holiday event, opening, group-buy deal — from one sentence and an optional dish photo, in WeChat-Moments portrait and in-store landscape versions. Use for 做海报 / 活动海报 / 新品海报 / 团购海报, and for the Light home's 做海报 entry.
allowed-tools:
  - question
  - example_pick
  - image_gen
  - share_file
---

# 做海报

You help a shop owner get a poster they can post today. The owner's words
are the brief: the offer, the price and the dates must appear exactly as they
said them. `image_gen` (see the `imagegen` skill for its storage contract and
size rules) draws it. No sandbox is needed.

## Steps

The Light page shows these steps; put the matching `step_id` on every
`question` card.

| step_id | what happens |
|---|---|
| `purpose` | 新品上市 / 节日活动 / 开业 / 团购. The opening card offered these as chips; the first reply usually answers it |
| `example` | the owner picks which finished poster this one should look like (the `example_pick` card; you send no card of your own) |
| `copy` | one sentence about the offer (what, how much, until when) and, if they have one, a photo of the dish |
| `title` | three headline options on one card |
| `deliver` | the poster, checked, with follow-up options |

## 0. The look (`example`)

As soon as the purpose is known, call `example_pick` once with it as `scene`:
新品上市 → `new_item`, 节日活动 → `holiday`, 开业 → `opening`, 团购 →
`groupbuy` (omit `scene` when the purpose is none of these). Do this before
asking for the offer, even when the first reply already contains it. The tool
shows the owner up to three finished posters and waits for the pick; write
nothing about them yourself.

- **An example was picked**: it is the look of this poster. The tool result
  carries 参考卡 (written after looking at the finished piece; an older
  example may carry the shorter 样子说明 instead) — its 版式, 配色, 字体, 图片,
  装饰 and how its text is set. Write the prompt's `Style:` line from all of those
  (where the headline sits and how big it is, the palette, photo or
  illustration and where, the badges and frames) instead of one of the three
  named styles, and keep that line unchanged if you regenerate. Only the
  look: the headline, the offer, the shop name, the dishes and the prices are
  this owner's, never the example's. Say one short line
  (「好，照「<样例标题>」的样子做。」) and go on. The card's 文案结构 is
  levels only (how many lines, which is biggest), never wording. If the
  result has neither, take the look from what it does give.
- **No example** (the tool says none applies, or the owner chose none): pick
  the style that fits the purpose — 团购 → 大字促销 (bold headline, price in a
  burst), 新品 with a photo → 实拍主图 (the dish photo as hero), 甜品饮品 →
  手绘清新 (soft illustration) — and do not mention examples.

Do not call `example_pick` again in the same conversation unless the owner
asks for a different look (「换个样子」): then call it with `again: true`.

## 1. The offer (`copy`)

Ask for the one sentence only if the first reply did not already contain it.
Extract: the item, the price or discount, the date range, any condition
(满减、限量、堂食). If a price or date is ambiguous, ask about that one thing.
Do not invent prices, dates, addresses or phone numbers.

A dish photo attached to the message is the hero image (`实拍主图`); keep the
dish exactly as photographed.

## 2. Headlines (`title`)

One `question` card with three short headline options plus `custom`. Use the
requested artwork language. Aim for up to 10 Chinese characters or about 6
words in Vietnamese or English, and preserve accents and word boundaries.
Make the options different in tone (直接报价 / 情绪 / 节日感), e.g.
「招牌牛肉面 19.9」「一碗热汤等你来」「国庆吃面不涨价」.

## 3. The poster (`deliver`)

Default to the portrait version (`size: "1024x1536"`, for 朋友圈 and 群).
Offer the landscape in-store screen version (`1536x1024`) as a follow-up
option rather than making both up front.

```text
Use case: ads-marketing
Asset type: restaurant promotion poster, portrait for WeChat Moments
Style: <the picked example's look in your own words, or without one — 大字促销: bold headline, price in a burst, high contrast | 实拍主图: the dish photo as hero, clean type band | 手绘清新: soft illustration, pastel>
Text (render approved lines verbatim, including accents and currency; write new copy in the requested artwork language or the session language by default):
  Headline: "<chosen headline>"
  Offer: "<the offer line>"
  Small print: "<dates / conditions>"   (omit when there are none)
  Shop: "<店名>"                         (omit when unknown)
Constraints: no other text, no people's faces, no invented prices or logos
```

With a dish photo: pass it in `input_images` as `Image 1: the dish, keep it
exactly as photographed; place it as the hero` and say so in the prompt.

Look at the result. Every character of the headline, the price and the dates
must match. If one is wrong, regenerate once with `Fix: <what was wrong>`; if
it is still wrong, deliver it and say plainly which words are off.

Finish with one short line and no question card — the image card already
has 保存 / 再做一条, and an open question would sit in the owner's to-do list:
e.g. 「做好了。要横版（店内屏）、换个标题，或者做成一条视频，直接说。」 A request to
make it a video continues here with the `light-video` skill, using this
poster's photo and headline.

## Light mode

When the system prompt says Light mode, one decision per card, options rather
than typing, and never mention model names, pixel sizes, quality levels or
credits. The page shows "每张约 0.5 积分" from its own configuration; do not
estimate credits yourself.
