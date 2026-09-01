# Prompt shape for a spoken shot

Six parts. They are here because each one fixes a failure that shows up
otherwise, not because the model requires a template.

1. `全片一致的画面基底：` + the shared anchor, **byte for byte identical** in
   every shot. This is what keeps the presenter, wardrobe, set and light the
   same; paraphrasing it between shots is the single most common cause of a
   presenter who changes halfway through.
2. `固定镜头` — a talking head that drifts or pushes in cuts badly against the
   next shot.
3. Framing: `半身中景` / `近景`. Vertical 9:16 crops a wide shot into a distant
   figure nobody can read.
4. `自然肢体动作：` + one restrained gesture. Without it the model either
   freezes the presenter or has them wave through the whole line.
5. The line: a speech lead, then `@` immediately followed by the **exact**
   words. Anything after `@` is what gets said, so put nothing else there.
6. `无字幕，字幕只能后期合成` — captions are added in post from the transcript.
   A model that burns in its own leaves two sets on the final cut.

Canonical shape:

```text
全片一致的画面基底：<逐字 anchor>
固定镜头。构图：竖屏 9:16 半身中景。
自然肢体动作：使用与本句匹配的克制手势。
语气：自然、清晰、有感染力。
面对镜头说出@<本段逐字台词>
无字幕，字幕只能后期合成。
```

Check it with `scripts/lint_prompt.py --prompt-file seg1.txt --script "…"
--anchor "…"`. It reports; you decide.

## Per role

- **hook** — energetic, slight forward lean. Earn the next two seconds.
- **body** — confident and informative, restrained counting gestures.
- **transition** — warm and conversational, one small nod.
- **closing** — friendly invitation to comment or save, natural smile.
- **b-roll** — a shot with nobody speaking. No line, no framing rule, no tone;
  keep the visual anchor and `无字幕`. Lint it with `--broll`.

## Referring to material

Number images and videos separately in prose: `参考图片1`, `参考视频1`. Never
put a URL, an `asset://` id or a file path in prompt text — the reference
travels as a structured input, and an id in the prose just confuses the model.

Keep scene description to one short clause. Do not ask for camera moves, speed
changes, on-screen text, logos, brand or celebrity likeness, or medical claims.
