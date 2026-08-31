# Seedance spoken-segment prompt recipe

Every prompt must contain these explicit lint-safe parts:

1. `全片一致的画面基底：` followed by the exact shared `visual_anchor`;
2. `固定镜头` and medium/half-body framing;
3. `自然肢体动作：` followed by one restrained gesture;
4. `语气：` followed by the performance tone;
5. a speech lead immediately followed by `@<exact segment dialogue>`;
6. `无字幕，字幕只能后期合成`.

Canonical portrait-only pattern:

```text
全片一致的画面基底：<逐字 visual_anchor>
固定镜头。构图：竖屏 9:16 半身中景。
自然肢体动作：使用与本句匹配的克制手势。
语气：自然、清晰、有感染力。
面对镜头说出@<本段逐字台词>
无字幕，字幕只能后期合成。
```

The dialogue is the only text after `@` that the person should say. Keep the
scene prose to one short clause. Do not add speed instructions, camera moves,
brand/celebrity/IP impersonation, medical claims, URLs, asset IDs, logos, or
generated on-screen text.

Useful role variations:

- hook: energetic tone and slight forward lean;
- body: confident informative tone with restrained counting gestures;
- transition: warm conversational tone and one small nod;
- closing: friendly invitation to comment/save, with a natural smile.

The server rejects a prompt when the exact dialogue, visual anchor, fixed-camera
framing, gesture, tone, or `无字幕` marker is missing. Recount normalized spoken
characters after every split: 40 or fewer is preferred and 48 is a hard maximum.
It also validates every `参考图片N`/`参考视频N` against the actual per-type count.
