# Checking what the video actually says

A generated presenter often says something slightly different from the line you
wrote. It is common, it is cheap to catch, and it is the difference between a
publishable cut and one where the captions do not match the audio.

Per shot:

```bash
S=/opt/openbox/skills/video-production/scripts
"$S/extract_audio.sh" shot1.mp4 shot1.mp3
# share_file(file_path=".../shot1.mp3", attach=false)  -> asset_id
# video_transcribe(action="submit", asset_id=..., idempotency_key="<slug>:shot1:stt")
python3 "$S/compare_transcript.py" --intended "本段台词" --heard "转写结果"
```

`attach=false` keeps the intermediate audio out of the conversation — the
person asked for a video, not a stack of mp3 cards.

Show them, per shot: the video, the intended line, the actual words, the
similarity, and the notes. Then decide together.

## Reading the verdict

- **`ok`** — accept it, and still show it.
- **`suspect`** — look. It means either the similarity fell below 0.90, or a
  substitution was found at any length. A one-character swap keeps the ratio
  high and can invert the meaning (`出片` → `出花`), which is why a bare number
  is not the verdict.

Punctuation, spacing and filler particles (嗯、啊、吧…) are stripped before
comparing — nobody hears them.

## Regenerating

Change only the shot that is wrong, and keep exactly one current take per shot:
a `:v2` supersedes `:v1`, and only current takes go into the composition. The
old take stays on disk as evidence, never in the cut. Track which is current in
`state.py` so a later turn does not have to guess. Give it a fresh idempotency key (`:v2`),
keep the old take, and leave every good shot alone. If the line itself was the
problem, rewrite that line and the prompt together — the prompt carries the
verbatim words after `@`, so they must move as a pair.

Do not retry an ambiguous paid submit. If a submit timed out or the result was
unclear, reconcile the same `job_id`; the task is durable and a second submit
pays twice for the same shot.

## Captions

Captions use the **accepted actual transcript**, never the written line. That
is the whole point of this step: it keeps the words on screen aligned with the
words in the audio when the model changed a particle or a phrase.


## Shot length

A shot's duration is an output of its line, never an input. Mandarin narration
runs about 4 characters per second (broadcast pace is 280–300/minute) and needs
a breath at each end, so `plan_shots.py` computes each shot from its own text.

Measured 2026-09-01, on a run that divided "30 seconds" by five shots:

| shot | chars | asked | rate | result |
|---|---|---|---|---|
| 1 | 13 | 5s | 2.6/s | model padded with words nobody wrote |
| 3 | 31 | 6s | 5.2/s | rushed |
| 4 | 32 | 6s | 5.3/s | rushed |

The same script planned per-line needs 40s, every shot landing at 3.4–4.0/s.
So a "30-second" request gets either a 40-second video or a shorter script —
those are the two honest options, and the person picks which.
