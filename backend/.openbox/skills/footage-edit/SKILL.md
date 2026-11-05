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

### 风格

One style for the whole cut; it sets tempo, colour, music, text and voice
together, so never mix two. Pick it from the footage first, the type second,
and name it on the card in the owner's words:

| 风格 | 挑它当 | 镜头节奏 | 滤镜 | 配乐 | 字幕、花字 | 转场、运镜、变速 | 旁白音色 |
|---|---|---|---|---|---|---|---|
| 快剪动感 | 活动促销；多道菜 / 多件商品轮播；人群、舞台、忙碌的现场；clips full of motion | 1.5–2.5 s a shot, the 高潮 shot 3 s, nothing over 4 s unless it is a spoken sentence | none, or `warm` for food | 动感（a beauty / nail / hair shop → 时尚） | captions size 44; up to three 花字, size 52, `"color": "#FFD700"` | `wiperight` once into the 高潮, `fade` into the closing card; `speed: 1.5–2` on a long process shot; no `move` | 知甜 `zhitian_emo` |
| 温暖纪实 | 探店、日常记录；服务过程；people talking in the clips | 2.5–4 s a shot, a spoken sentence whole, the 成果 shot 4–6 s | `warm` | 美食（a quiet tea, coffee or book shop → 轻松） | captions size 44 white; at most two 花字, size 48 | `fade` at the two joints only; `"move": {"preset": "zoom_in"}` on one static shot; `speed: 0.5` on the 成果 | 知媛 `zhiyuan` |
| 产品大片 | 新品上市；close-ups of one dish, product, finished nails or hair; few clips, each good | 3–5 s a shot, 5–7 shots in all, the hero shot 5–6 s at `speed: 0.5` | `m7` | 轻松（a dessert, flower or wedding shop → 浪漫） | captions only on spoken shots; 花字 only the product name and the price, size 56 | `fade` at both joints; `zoom_in` on the hero shot; landscape clips `fit: "contain"` + `backdrop: "blur"` | 知祥 `zhixiang` |
| 口播 vlog | the owner talks to the camera for most of the footage (spoken ≥ 60 %) | follows the sentences; a 2–3 s cut-away between sentences when there is one | none | 轻松 at `volume: 0.25` | spoken captions as below; 花字 only the shop name | none, no `move`, no `speed` | none — the owner's voice is the narration |

A 国风 shop (茶馆、老字号、汉服、国潮装修) takes 国风 music whatever the style.
Defaults by type when the footage does not decide: 探店 / 日常记录 → 温暖纪实,
新品上市 → 产品大片, 活动促销 → 快剪动感. Say the style on the card as one line
the owner can picture, e.g. 「风格：温暖纪实（暖色、原声为主、每镜 3 秒左右）」; the
tempo, filter, music, text and transition rules below bend to the style's
column.

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
- Narration: only after the owner picked 加旁白 on the plan card — see 旁白
  below. Never add it on your own.

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

Music: one track from the `music` list, by the style's tag (table above; a
国风 shop → 国风 whatever the style). Within the tag take the first track at
least as long as the cut, else the longest (a shorter one repeats). The clips
keep their own sound; the music sits under it and drops while anyone speaks.

### 旁白

Only after the owner picks 加旁白, and never in 口播 vlog — there the owner's
own voice is the narration: say so in one line and show the card again
unchanged. Otherwise write the script, then place it:

- 2–4 lines, ≤ 20 characters each, one idea per line, spoken Chinese (「三分钟
  还你新轮毂」, not 「本店提供高效轮毂清洗服务」):
  1. the hook line — the same words as the hook caption (a question or a promise);
  2. one or two body lines — what the viewer is looking at, plus the one
     concrete fact the owner gave (price, how long it takes, the 招牌, the
     时间地点). The owner's own sentence from the upload step comes first,
     the readings' `shot` / `beats` second; invent no number, name or claim;
  3. the closing line — the shop name once and what to do (来店 / 预约 / 地址).
- Timing: about 0.3 s per character plus 0.5 s of air before and after the
  line. A line sits on the shots it describes: the hook line on the hook shot,
  the closing line on the closing card, body lines on silent shots. Never over
  a spoken sentence; when a body line has nowhere else to go, lengthen the
  silent shot it describes rather than speeding the voice (no `speech_rate`
  above 100). Each line is its own `audio[]` item: `{"voiceover": {"text":
  "<line>", "voice": "<the style's voice>"}, "from_sec", "to_sec", "volume": 1}`.
- The narrated shots' captions carry the narration line (split at commas,
  ≤ 12 characters a cue) instead of a separate action caption, so the viewer
  never reads one thing and hears another.
- The voice is the style's (table above): 知媛 `zhiyuan`, 知祥 `zhixiang`,
  知甜 `zhitian_emo`; no other. The music gets `duck_under_voice: true`.
- Then the card again, with the lines on it:
  `旁白：<音色>，<n> 句：「<line 1>」「<line 2>」…`, options `就这么剪` / `改旁白` /
  `不要旁白` / `custom`. 改旁白 means the owner types what to change — rewrite
  the lines, card again; 不要旁白 drops the `voiceover` items and the card is
  not shown again. The price does not change either way.

Then **one** `question` card on `plan`, `purpose` review, in plain words:

```
成片：约 <N> 秒竖屏，<type>
风格：<风格名>（<三五个字说它长什么样>）
开场：<hook 画面一句话>，字幕「<hook line>」
镜头：
1. <0–3 秒> 门头（第 1 段 2–5 秒）「<这一镜的字幕>」
2. ...
收尾：<成果或最暖的一镜>，卡上写店名「巷口面馆」+「<地址/电话/价格/一句承诺>」
字幕：<共 n 句（口播 n 句 + 动作字幕 n 句），例：「先把石子一颗颗挑出来」…>
花字：店名「巷口面馆」（开场与收尾）；「红烧牛肉面 ¥28」
声音：原声 + 配乐「<track name>」（<tag>，说话时自动压低）
<旁白：<音色>，n 句：「…」「…」 — only after the owner picked 加旁白>
价格：成片 <5|9|…> 积分（素材分析 <analysis_credits> 积分已收）
```

The cut price comes from its length: ≤ 60 s 5 积分, ≤ 180 s 9 积分, then +2 for
each further started minute. State both numbers exactly; they are what the
bill will show. Options: `就这么剪` / `短一点` / `换个开头` / `换风格` /
`不要配乐` / `加旁白` / `custom`.

- 换风格: one more `question` card on `plan`, `purpose` choose — 「换成哪种？」
  with the other three styles as options (`custom` too), each described in the
  card text in one line (快剪动感 — 节奏快、动感配乐；温暖纪实 — 暖色、原声为主；
  产品大片 — 镜头少而长、灰橙色调；口播 vlog — 跟着你说的话剪). Then re-plan from
  the chosen column (shots, music, filter, texts all follow) and show the plan
  card again; it is a different cut, so always show it.
- 加旁白: write the script (旁白 above) and show the card again with the lines.
- 不要配乐: compose with no music `audio` (the price does not change, so no
  new card).
- Otherwise apply the change and show the card again only when the length or
  the price changed; for 换个配乐 pick another track of the same tag.

## 4. Compose (`compose`)

The format is below; do not call `action="schema"` or `validate`. Call
`submit` straight away — it checks the timeline before anything is rendered
and refuses an invalid one with the reason; fix that and submit again. If a
refusal cannot be fixed, never quietly compose less than the card promised
(dropping the 旁白, the music or a shot): say in one line what cannot be done
and why, and ask whether to go on without it.

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
names, ids or paths in anything the owner sees — a style is 「温暖纪实」 with
five words on what it looks like, never a filter id or a voice id. Never estimate prices other
than the two numbers above.
