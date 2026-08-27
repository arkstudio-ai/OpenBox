# Seedance spoken-segment prompt recipe

Every prompt must contain these five parts:

1. camera and framing: `固定镜头中景` or `固定镜头半身`;
2. the exact shared `visual_anchor` (same bytes in every prompt);
3. a clear speech lead followed immediately by `@<exact segment dialogue>`;
4. a restrained gesture plus tone;
5. `无字幕`.

Canonical portrait-only pattern:

```text
固定镜头中景，参考图片1的人物坐在明亮整洁的室内旅行分享区，人物造型、服装、背景和机位全程保持一致，面对镜头开口说出@<本段逐字台词>，手势随语气自然舒展，语气亲切有感染力，无字幕
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
framing, gesture, tone, or `无字幕` marker is missing. It also validates every
`参考图片N`/`参考视频N` against the actual per-type reference count.
