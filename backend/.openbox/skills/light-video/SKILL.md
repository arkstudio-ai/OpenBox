---
name: light-video
description: The Light home's 做一条视频 — one 15-second vertical clip for a small shop from a picked style and 0–3 photos, generated in the cloud with the shop name overlaid. Needs no desktop, so it works on the free plan. Use only on the Light 做一条视频 topic page; spoken presenter videos and multi-shot cuts are the video-production skill.
allowed-tools:
  - question
  - video_generate
  - video_compose
---

# 做一条视频（Light）

One clip, made the way the first video is made: the style the owner picked in
the gallery plus their photo become a single generation, then the shop name
goes on top as text. Everything runs in the cloud — **no desktop, no bundled
scripts, no files**: never call `bash`, `read`, `skill` scripts or anything
under `/opt/openbox` or `/workspace`. If the system prompt says the sandbox or
desktop is unavailable, that does not matter here; `video_generate`,
`video_compose` and `question` all work without it.

## Steps

The page shows these steps; put the matching `step_id` on every `question`.

| step_id | what happens |
|---|---|
| `topic` | the look. The opening card already carries it (「烟火气特写」 etc.); never ask again |
| `photos` | 0–3 photos the owner attached, or a sentence instead |
| `confirm` | one review card |
| `generate` | the clip, then the name overlay |
| `deliver` | the finished clip in the chat |

## 1. Photos (`photos`)

Photos arrive attached to the owner's message (each with an asset id). Pick the
one that shows the dish or the shop best as the reference; the clip will keep
its look. No photo is fine: describe the subject from what they said (a dish
name, the shop type). If the first reply has neither a photo nor a subject, ask
once on `photos` with options 「我拍一张」/「没照片，就做招牌菜」/`custom`.

## 2. Review card (`confirm`)

One `question` card, `purpose` review. In plain words, three short lines:

```
画面：<one sentence: subject + the picked look, e.g. 红烧肉特写，热气和酱汁光泽，镜头慢慢推近>
时长：15 秒竖屏
店名：<shop name, shown on the clip> (or 「不加店名」 when unknown)
```

Options: `就这样，开始做` / `换个画面` / `custom`. No prices, no model names,
no "(Recommended)" labels — the page shows the price itself. If they change
something, apply it and go straight to generation unless the change is unclear.

Post this card once. A change that arrives after it (a revision from the owner
or relayed by their assistant, "可以的，但是…", a new line or a new look) is the
answer: apply it and generate, do not post the review card again. The owner is
often on the phone, where every card is read aloud and a second one sounds
like you did not hear them.

The owner may name a model, tier or "那款". You cannot change the model: the
page's selection is used. Do not ask a card about it and do not plan around
the named model's limits; say in the delivery line that the clip was made with
the model chosen on the page.

## 3. Generate (`generate`)

Write the generation prompt in Chinese, one paragraph, from the look below plus
the subject. With a photo: start with 「以参考图中的<主体>为主体，保持它的外观、颜色和摆盘」.
Always end with: 「画面中不要出现任何文字、字幕、水印或 logo，不要出现清晰人脸。」
(the model draws wrong characters; the name goes on afterwards).

| look (opening card) | shot language |
|---|---|
| 烟火气特写 | 刚出锅特写，热气升腾，酱汁在暖光下泛光，俯拍慢推近再环绕半圈，木桌，浅景深，暖色 |
| 高级感暗调 | 深色背景，侧逆光勾出光泽与蒸汽，极慢平移推近，暗调低饱和，留白 |
| 快节奏多菜 | 一桌菜轮流特写入画，每道一两秒，短切配快速推拉，热气腾腾，色彩鲜亮 |
| 傍晚亮灯 | 傍晚亮暖灯的门头，镜头从门外推进店内，食客只用中远景或背影，烟火气 |
| 明亮整洁 | 白天自然光，浅色桌椅整齐，镜头沿座位平稳滑过，清新干净 |
| 满座人气 | 饭点满座，食客中远景与背影，端菜穿行，手持感轻晃，暖色热闹 |
| 边走边吃 | 第一视角：门头 → 看菜单板 → 上菜 → 夹起一口 → 拉远看整桌 |
| 三道必点 | 三道招牌依次特写，各配推近与筷子夹起，节奏分明 |
| 人均多少 | 俯拍一桌分量十足的菜，再特写满满一勺，实惠接地气 |

No look picked: use 烟火气特写 for a dish, 傍晚亮灯 for a shop.

Call:

```
video_generate(action="submit", prompt=<prompt>, duration=15, ratio="9:16",
               input_assets=[{"asset_id": <photo>, "role": "reference_image"}],   # omit without a photo
               idempotency_key="light_video:<this conversation's session id>:<n>:<now, e.g. 1109-0308>")
```

The key is scoped to the owner, not to this conversation: a plain
`light_video:<session>:1` collides with the clip they made yesterday and the
tool answers that the key "already belongs to a different request". That means
the key, not the clip, is wrong: submit once more with a fresh key (new time
suffix). Never resend the same key, and never copy a session id from the brief
or an earlier task — use this conversation's own.

Do not pass `model` or `resolution` (the page fixed them). Then
`video_generate(action="wait", job_id=…, wait_seconds=25)` until it completes;
while waiting say nothing technical. If the tool says polling is paused, end
the turn with one line (「片子还在做，做好了会自动出现在这里」): the clip — and later the
name overlay — wakes this conversation by itself when it lands. If the generation is refused because of the photo (a face,
a hand), drop the photo and submit once more from text; tell the owner in one
plain line.

## 4. Name on top

When the shop name is known (the system prompt gives it, or the owner said it),
overlay it with `video_compose(action="submit")`:

```json
{"version": 1,
 "shots": [{"asset": "<generated asset_id>", "duration_sec": 15}],
 "texts": [
   {"text": "<店名>", "from_sec": 0.6, "to_sec": 14.8, "y": 0.76, "style": {"size": 64, "outline": 4}},
   {"text": "<一句主打，≤12 字>", "from_sec": 1.2, "to_sec": 14.8, "y": 0.835, "style": {"size": 38, "outline": 3}}
 ]}
```

then `wait` on it. If the overlay fails, deliver the plain clip — it is still the
owner's video.

## 5. Deliver (`deliver`)

The finished clip attaches itself. Close with one short line in the owner's
language, e.g. 「做好了，可以直接保存或者发到抖音。想换个样子再做一条，直接说。」.
Do not ask another question; the page's result card has the buttons.

## Light mode

One decision per card, options rather than typing, no model names, tiers,
credits, ids, paths or tool names in anything the owner sees.
