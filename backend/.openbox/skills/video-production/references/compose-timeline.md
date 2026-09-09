# Cloud composition: the timeline you hand to `video_compose`

`video_compose` renders a cut on Aliyun IMS from a JSON timeline you write. It
costs money the moment you `submit`, so the order is fixed:

1. `video_compose(action="schema")` once per session if you have not seen the format.
2. Build the JSON from the **accepted takes' asset_ids** (from `video_generate`),
   the **accepted actual transcript** for captions, and the person's hook/handle.
3. `video_compose(action="validate", timeline=…)` — free. It returns
   `duration_sec`, `tier`, `minutes_billed`, `estimated_credits`, and `warning=` lines.
   Fix every error; read every warning.
4. **Card 4 (合成确认)** — show the person, in prose before the card: shot order
   with each shot's seconds, every caption line with its timing, every banner
   (hook / handle) with its timing, transitions, output size, `duration_sec`,
   and the exact `estimated_credits` with the billing note. Then `question` with
   `可以` / `改字幕或时间` / `换转场或动效` / `改用无特效拼接（ffmpeg）`.
   No `submit` until `可以`. If `estimated_credits` is unavailable, say so and stop.
5. `submit` with a stable `idempotency_key` (`<slug>:compose:v1`); `wait` on the
   job_id; on `polling_paused=true` end the run and resume later. `credits=` on
   the completed result is what was actually recorded; report it.
6. Deliver via the attached card / `download_url`; `share_file` is not needed
   for a composition — the tool already attached it.

## Units and shape (the tool's `schema` action is the authority)

```jsonc
{
  "canvas": { "width": 720, "height": 1280, "fps": 24 },
  "shots": [
    { "asset": "<asset_id of the accepted take>", "duration_sec": 5.06,
      "fit": "cover", "transition_out": { "type": "fade", "seconds": 0.5 } },
    { "asset": "<asset_id>", "duration_sec": 4.9 }
  ],
  "captions": [ { "from_sec": 0.2, "to_sec": 2.4, "text": "实际念出的那句" } ],
  "caption_style": { "bottom_ratio": 0.095, "max_width": 0.88 },
  "texts": [
    { "text": "钩子标题", "from_sec": 0, "to_sec": 3, "align": "center", "y": 0.125, "max_width": 0.9,
      "style": { "size": 56, "color": "#FFD700", "outline": 2 },
      "motion": { "in_effect": "slide_down_in", "in_sec": 0.5, "out_effect": "fade_out", "out_sec": 0.6 } },
    { "text": "@频道名", "from_sec": 3, "to_sec": 9.5, "align": "left", "x": 0.056, "y": 0.727,
      "style": { "size": 34, "color": "#FFD700", "outline": 2, "outline_color": "#111111" },
      "motion": { "in_effect": "slide_right_in", "in_sec": 0.5 } }
  ]
}
```

- Seconds and canvas fractions only. Never pixels, never IMS field names.
- `shots[].duration_sec` = the take's **measured** length (`ffprobe`), not the planned one.
- `transition_out` sits on the shot it leads out of; it shortens the total by its length — two 7.5 s shots with a 0.6 s fade make a 14.4 s film, not 15 s. Card 4 must state the `duration_sec` from `validate` as the real length, with the words 含转场重叠.
- `fit: cover` crops to fill (default for vertical talking heads); `contain` letterboxes on purpose.
- Captions: one per spoken phrase, non-overlapping, text = accepted STT words. The tool places them bottom-centre.
- `texts[]`: banners. `align=center` ignores `x`; `y` is the top edge as a fraction of height.

## Effects you may use without a warning (verified in a real job)

- transitions: `fade`
- motion in: `slide_down_in`, `slide_right_in`, `zoomin_in`
- motion out: `fade_out`
- font: `AlibabaPuHuiTi`

Other documented names (`wiperight`, `typewriter1_in`, 花字 `golden`…) compile with a
`warning=… not yet verified`; tell the person it is untested before spending on it.
Unknown names are refused before submission — that is the tool saving money, not a bug.

## Billing

Per output minute by tier, 不足 1 分钟按 1 分钟计, 合成失败不计费, settled on the
**actual** output duration: 720p 0.03 credits/min, 1080p 0.06. A 9.5 s short is
1 minute = 0.03. Quote before, report after; never call it free.

## When not to use it

Plain concat with burnt ASS captions and no effects: `$S/build_ass.py` + `$S/compose.sh`
in the sandbox costs nothing. Offer that as the `改用无特效拼接` option on card 4.
