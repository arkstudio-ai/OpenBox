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
timings, usable segments (`from_sec`/`to_sec`, what is happening in the shot,
`kind`, quality), `beats` (the one to three moments that grab — a beat has
`at_sec`, `what`, `why`), `hook` (the picture that can open the cut) and
`best`. Read them before planning: the cut is built from what the clips
actually show, not from the type's template. It also charges the 素材分析 fee for the whole batch and tells you
the amount (`analysis_credits=`), and lists the music library by tag
(`music`: name, length, library_id). Do not call it again for the same clips.

## 3. The plan (`plan`)

Start from what the readings say the clips are about (`subject`, `kind`,
`beats`), then pick the structure that fits — the owner's type is the
intent, the footage decides the shape. The five below are the whole
reference; do not open the references file during a cut:

- 餐饮 探店: 门头 → 环境 → 招牌菜 → 价格/活动 → 收尾
- 餐饮 新品 / 活动: 成品或活动一句话 → 制作或细节 → 吃的瞬间 / 菜品轮播 → 价格 → 时间地点 → 收尾
- 服务类（美业、洗车、维修、养生，clips that are mostly `服务过程`）: 招牌或问题亮相 →
  过程 3–4 步，每步一镜 → 成果展示 / 前后对比 → 价格或承诺 → 收尾
- 零售（货架、商品、门店）: 门头或货架 → 爆品特写 2–3 个 → 价格或活动 → 收尾
- 活动现场 / 日常记录（舞台、人群、忙碌）: 开场一瞬 → 高潮 → 人群反应 → 时间地点或一句话 → 收尾

Rules for picking:

- Only `usable: true` segments. The **first shot is the hook**: a clip's
  `hook` or its strongest `beat`, never a blank wall or a shaky start, and
  its first second already shows the subject.
- Shot length follows the action, not a fixed count: an action beat 1.5–3 s,
  a process step 2–4 s, the 成果展示 / 前后对比 / 亮相 shot 4–6 s so the eye
  lands. Cut a shot so a `beat` sits inside it, near its start. Never two
  shots from the same second range; mix wide and close; keep the order of a
  process (挑石子 before 冲洗, 生 before 熟).
- A spoken line is kept whole: one shot from the sentence's `from_sec` to its
  `to_sec` in that clip, never two shots that each hold half of it, and keep
  the clip's sound (`volume: 1`). Shots without speech keep their natural
  sound too.
- Landscape clips are fine: `fit: "cover"` centre-crops them to 9:16; pick
  segments whose subject is in the middle.
- Target length: what the owner asked for, else **30–50 s**, never longer
  than the usable footage allows. With 3 or more clips, use every clip that
  has a usable segment at least once — the owner shot them to see them. Do
  not stretch shots to reach a length; a cut of 25 good seconds beats 45
  padded ones.
- Transitions: only at the two structure joints — into the 成果 / 高潮
  section and into the closing card — as `transition_out: {"type": "fade",
  "seconds": 0.5}` on the shot before (`wiperight` and `perlin` are verified
  too; use one of them at most once). Never between every shot.
- Tempo and camera: the 成果 / 亮相 / 吃的瞬间 shot may run at `speed: 0.5`
  (slow motion; its timeline length doubles) and a long process step at
  `speed: 1.5–2`; a static shot gets `"move": {"preset": "zoom_in"}`. One or
  two such shots per cut, never all of them. Caption and text times are on
  the timeline after speed.
- Landscape clips: `fit: "contain"` + `backdrop: "blur"` keeps the whole
  frame with a blurred copy behind it; `cover` centre-crops. Use contain+blur
  when the subject is not in the middle or the shot is wide (a stage, a
  crowd), cover when it is a close-up.
- Colour: one `filter` for the whole cut when the clips do not match —
  `warm` for food and evening shots, `m7` (灰橙) for a calmer, product look.
  Never a different filter per shot.
- Narration: only when the owner picked 加旁白 on the plan card. Then write
  a script of 2–4 short lines (≤ 20 characters each, one idea per line, the
  shop name once), place each as `audio[].voiceover` over its shots with
  about 0.3 s per character of room, `voice: "zhiyuan"` (the verified one),
  `volume: 1`, and set `duck_under_voice: true` on the music. Captions still
  carry the same words.

Captions — every shot says something to the viewer, with or without speech:

- Spoken shots: ≤ 12 Chinese characters per cue — split every longer sentence
  at its commas into several cues and share the sentence's time between them
  in proportion to their length (a 30-character sentence is three cues); a
  longer line runs off the 720-px frame. Time cues to the sentence timings.
- Silent shots: one **动作字幕** per shot, written from the segment's `shot`
  for the viewer, not as a label — 「先把石子一颗颗挑出来」, not 「清理石子」;
  ≤ 12 characters, from 0.2 s after the shot starts to 0.2 s before it ends.
  The hook shot carries the hook line instead: a question or a promise in ≤
  10 characters (「轮毂多久没洗了？」「三分钟还你新轮毂」).
- The closing card: the last 2.5–3 s of the cut, on the 成果 or the warmest
  shot, with no caption — its texts carry the shop name and one line the
  owner gave or the footage shows (地址 / 电话 / 价格 / 一句承诺). Make it from
  what the owner said; invent nothing.

Banner texts (`texts`):

- The shop name only on the hook shot (0.3 s → end of that shot) and on the
  closing card, `y: 0.76`, size 60 — not across the whole cut, which reads as
  a watermark. The hook-shot name slides in: `"motion": {"in_effect": "slide_down_in"}`.
- At most two short 花字 on their own shots (the dish or service name, the
  price), `y: 0.2`, size 48, 2–4 s each.

Music: one track from the `music` list, by type — 探店 / 日常记录 → 美食
(a quiet tea, coffee or book shop → 轻松), 新品上市 → 欢快, 活动促销 → 动感;
a 国风 shop (茶馆、老字号、汉服、国潮装修) → 国风 whatever the type; a beauty,
nail or hair shop → 时尚. Within the tag take the first track at least as long
as the cut, else the longest (a shorter one repeats). The clips keep their own
sound; the music sits under it and drops while anyone speaks.

Then **one** `question` card on `plan`, `purpose` review, in plain words:

```
成片：约 <N> 秒竖屏，<type>
开场：<hook 画面一句话>，字幕「<hook line>」
镜头：
1. <0–3 秒> 门头（第 1 段 2–5 秒）「<这一镜的字幕>」
2. ...
收尾：<成果或最暖的一镜>，卡上写店名「巷口面馆」+「<地址/电话/价格/一句承诺>」
字幕：<共 n 句（口播 n 句 + 动作字幕 n 句），例：「先把石子一颗颗挑出来」…>
花字：店名「巷口面馆」（开场与收尾）；「红烧牛肉面 ¥28」
声音：原声 + 配乐「<track name>」（<tag>，说话时自动压低）<；旁白：知媛，n 句，只在老板点了 加旁白 之后>
价格：成片 <5|9|…> 积分（素材分析 <analysis_credits> 积分已收）
```

The cut price comes from its length: ≤ 60 s 5 积分, ≤ 180 s 9 积分, then +2 for
each further started minute. State both numbers exactly; they are what the
bill will show. Options: `就这么剪` / `短一点` / `换个开头` / `不要配乐` /
`加旁白` / `custom`. 加旁白 adds an AI narration (Narration above; the price
does not change) and shows the card again with the script lines. 不要配乐 means: compose with no `audio` (the price does not change, so
no new card). Apply changes and show the card again only when the length or
the price changed; for 换个配乐 pick another track of the same tag.

## 4. Compose (`compose`)

The format is below; do not call `action="schema"` or `validate`. Call
`submit` straight away — it checks the timeline before anything is rendered
and refuses an invalid one with the reason; fix that and submit again:

```json
{"version": 1,
 "canvas": {"width": 720, "height": 1280, "fps": 25},
 "shots": [{"asset": "<asset_id>", "in_sec": 2.0, "duration_sec": 3.0, "fit": "cover", "volume": 1, "move": {"preset": "zoom_in"}},
           {"asset": "<asset_id>", "in_sec": 0.0, "duration_sec": 4.0, "fit": "contain", "backdrop": "blur", "volume": 1},
           {"asset": "<asset_id>", "in_sec": 5.0, "duration_sec": 3.0, "fit": "cover", "volume": 1, "speed": 0.5},
           {"asset": "<asset_id>", "in_sec": 5.0, "duration_sec": 4.5, "fit": "cover", "volume": 1,
            "transition_out": {"type": "fade", "seconds": 0.5}}],
 "captions": [{"from_sec": 0.2, "to_sec": 2.8, "text": "轮毂多久没洗了？"},
              {"from_sec": 3.2, "to_sec": 7.3, "text": "先把石子一颗颗挑出来"}],
 "texts": [{"text": "<店名>", "from_sec": 0.3, "to_sec": 3.0, "y": 0.76, "style": {"size": 60, "outline": 4}, "motion": {"in_effect": "slide_down_in"}},
           {"text": "<店名>", "from_sec": <end - 3>, "to_sec": <end - 0.2>, "y": 0.76, "style": {"size": 60, "outline": 4}},
           {"text": "<地址或一句承诺>", "from_sec": <end - 3>, "to_sec": <end - 0.2>, "y": 0.84, "style": {"size": 40, "outline": 3}}],
 "filter": "warm",
 "audio": [{"library_id": "<icepublic-… from music>", "volume": 0.35, "duck_under_captions": true,
            "duck_under_voice": true, "duck_volume": 0.1, "fade_in_sec": 1, "fade_out_sec": 2},
           {"voiceover": {"text": "轮毂多久没洗了？三分钟还你新轮毂。", "voice": "zhiyuan"}, "from_sec": 0.5, "to_sec": 6.0, "volume": 1}]}
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
