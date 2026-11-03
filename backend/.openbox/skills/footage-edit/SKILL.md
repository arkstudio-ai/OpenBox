---
name: footage-edit
description: 你拍我剪 — cut a small shop owner's own phone clips (3–10 clips, up to 5 minutes) into one vertical video with captions, the shop name and a music bed, priced by the finished cut. Reads the clips with footage_analyze (no desktop needed), proposes one plan card, then composes with video_compose. Use for 你拍我剪 / 帮我剪 / 把这几段剪成一条 / 实拍剪辑, and for the Light home's 你拍我剪 entry.
allowed-tools:
  - footage_analyze
  - video_compose
  - question
  - share_file
---

# 你拍我剪

The owner shot the material; you choose the moments, order them, write the
captions and put the shop name on. It is cheap for them on purpose (the price
is per finished cut, not per AI second), so the craft is in picking good
seconds, not in generating anything. **Never call `video_generate` or
`image_gen` here**, and never use bash, files or desktop scripts: everything
runs in the cloud.

## Steps

The Light page shows these steps; put the matching `step_id` on every
`question` card.

| step_id | what happens |
|---|---|
| `type` | 探店 / 新品上市 / 活动促销 / 日常记录 (the opening card offers them) |
| `upload` | the owner sends the clips (and maybe a sentence about what to stress) |
| `plan` | one plan card |
| `compose` | the cut is made |
| `deliver` | the finished video in the chat |

## 1. Type and clips (`type`, `upload`)

The first reply usually names the type. If it does not, ask once on `type`.
Clips arrive as video attachments with asset ids. If there are none yet, ask
on `upload`: 「从相册选 3–10 段（总共不超过 5 分钟），想突出什么也可以说一句」, options
`选好了` / `custom`. One long clip is fine too.

## 2. Read the clips

Call `footage_analyze` **once**, straight away, with every clip's asset id in
the order sent (the clips were already being read while they uploaded).
It returns per clip: duration, orientation, sound, transcript with sentence
timings, and usable segments (`from_sec`/`to_sec`, what the shot shows,
quality). It also charges the 素材分析 fee for the whole batch and tells you
the amount (`analysis_credits=`), and lists the music library by tag
(`music`: name, length, library_id). Do not call it again for the same clips.

## 3. The plan (`plan`)

Build the cut from the structure for the type (the four below are the whole
reference; do not open the references file during a cut):

- 探店: 门头 → 环境 → 招牌菜 → 价格/活动 → 收尾
- 新品上市: 成品亮相 → 制作或细节 → 吃的瞬间 → 价格 → 收尾
- 活动促销: 活动一句话 → 菜品轮播 → 价格 → 时间地点 → 收尾
- 日常记录: 开店 → 忙碌 → 出餐 → 客人 → 收尾

Rules for picking:

- Only `usable: true` segments. Each shot 2–4 s; the first second must show
  the subject (门头 or the dish), never a blank wall or a shaky start.
- Mix wide and close; never two shots from the same second range.
- A spoken line is kept whole: start and end the shot on its sentence
  timings, and keep the clip's sound (`volume: 1`). Shots without speech keep
  their natural sound too.
- Landscape clips are fine: `fit: "cover"` centre-crops them to 9:16; pick
  segments whose subject is in the middle.
- Target length: what the owner asked for, else **40–60 s**, never longer
  than the usable footage allows. With 3 or more clips, use every clip that
  has a usable segment at least once — the owner shot them to see them. Do not
  stretch shots to reach a length.

Captions: ≤ 12 Chinese characters per cue — split every longer sentence at
its commas into several cues and share the sentence's time between them in
proportion to their length (a 30-character sentence is three cues); a longer
line runs off the 720-px frame. Time cues to the sentence timings; no captions
over silent shots. Banner
texts: the shop name for the whole cut at the bottom (`y: 0.76`, size 60),
and at most two short 花字 (the dish name, the price) on their shots.

Music: one track from the `music` list, by type — 探店 / 日常记录 → 美食
(a quiet tea, coffee or book shop → 轻松), 新品上市 → 欢快, 活动促销 → 动感;
a 国风 shop (茶馆、老字号、汉服、国潮装修) → 国风 whatever the type; a beauty,
nail or hair shop → 时尚. Within the tag take the first track at least as long
as the cut, else the longest (a shorter one repeats). The clips keep their own
sound; the music sits under it and drops while anyone speaks.

Then **one** `question` card on `plan`, `purpose` review, in plain words:

```
成片：约 <N> 秒竖屏，<type>
镜头：
1. <0–3 秒> 门头（第 1 段 2–5 秒）
2. ...
字幕：<共 n 句，例：「今天带大家来巷口面馆」…>
花字：店名「巷口面馆」；「红烧牛肉面 ¥28」
声音：原声 + 配乐「<track name>」（<tag>，说话时自动压低）
价格：成片 <5|9|…> 积分（素材分析 <analysis_credits> 积分已收）
```

The cut price comes from its length: ≤ 60 s 5 积分, ≤ 180 s 9 积分, then +2 for
each further started minute. State both numbers exactly; they are what the
bill will show. Options: `就这么剪` / `短一点` / `换个开头` / `不要配乐` /
`custom`. 不要配乐 means: compose with no `audio` (the price does not change, so
no new card). Apply changes and show the card again only when the length or
the price changed; for 换个配乐 pick another track of the same tag.

## 4. Compose (`compose`)

The format is below; do not call `action="schema"`. Call `validate` once, then
`submit`:

```json
{"version": 1,
 "canvas": {"width": 720, "height": 1280, "fps": 25},
 "shots": [{"asset": "<asset_id>", "in_sec": 2.0, "duration_sec": 3.0, "fit": "cover", "volume": 1}],
 "captions": [{"from_sec": 0.2, "to_sec": 2.8, "text": "今天带大家来巷口面馆"}],
 "texts": [{"text": "<店名>", "from_sec": 0.3, "to_sec": <end - 0.2>, "y": 0.76, "style": {"size": 60, "outline": 4}}],
 "audio": [{"library_id": "<icepublic-… from music>", "volume": 0.35, "duck_under_captions": true,
            "duck_volume": 0.1, "fade_in_sec": 1, "fade_out_sec": 2}]}
```

`style` is an object, not a string. `audio` has no `from_sec`/`to_sec`: the
music runs the whole cut, repeating or cut to fit.

Caption times are on the cut's timeline (add up the shots before it), not the
source clip's.

After `submit`, **do not call `wait`**: a 你拍我剪 cut is watched for you and
attaches itself to this chat the moment it is ready (the submit result says
`delivery=automatic`). End the turn right away with one line: 「开始合成了，一般一两分钟，好了会自动出现在这里并通知你。」

## 5. Deliver (`deliver`)

The cut attaches itself. One short closing line: 「剪好了，可以保存或者发到抖音；想换个开头、
改字幕，直接说。」 No question card — the result card has the buttons.

## Light mode

One decision per card, options rather than typing, no model names, tool
names, ids or paths in anything the owner sees. Never estimate prices other
than the two numbers above.
