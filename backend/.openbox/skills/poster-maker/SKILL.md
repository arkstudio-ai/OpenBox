---
name: poster-maker
description: Make a promotion poster for a small restaurant or shop — new dish, holiday event, opening, group-buy deal — from one sentence and an optional dish photo, in WeChat-Moments portrait and in-store landscape versions. Use for 做海报 / 活动海报 / 新品海报 / 团购海报, and for the Light home's 做海报 entry.
allowed-tools:
  - question
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
| `copy` | one sentence about the offer (what, how much, until when) and, if they have one, a photo of the dish |
| `title` | three headline options on one card |
| `deliver` | the poster, checked, with follow-up options |

If the opening card named a style (大字促销 / 实拍主图 / 手绘清新), use it and
do not ask again; otherwise pick the one that fits the purpose (团购 →
大字促销, 新品 with a photo → 实拍主图, 甜品饮品 → 手绘清新).

## 1. The offer (`copy`)

Ask for the one sentence only if the first reply did not already contain it.
Extract: the item, the price or discount, the date range, any condition
(满减、限量、堂食). If a price or date is ambiguous, ask about that one thing.
Do not invent prices, dates, addresses or phone numbers.

A dish photo attached to the message is the hero image (`实拍主图`); keep the
dish exactly as photographed.

## 2. Headlines (`title`)

One `question` card with three headline options, each ≤ 10 characters, plus
`custom`. Make them different in tone (直接报价 / 情绪 / 节日感), e.g.
「招牌牛肉面 19.9」「一碗热汤等你来」「国庆吃面不涨价」.

## 3. The poster (`deliver`)

Default to the portrait version (`size: "1024x1536"`, for 朋友圈 and 群).
Offer the landscape in-store screen version (`1536x1024`) as a follow-up
option rather than making both up front.

```text
Use case: ads-marketing
Asset type: restaurant promotion poster, portrait for WeChat Moments
Style: <大字促销: bold headline, price in a burst, high contrast | 实拍主图: the dish photo as hero, clean type band | 手绘清新: soft illustration, pastel>
Text (verbatim, Simplified Chinese, render exactly):
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
