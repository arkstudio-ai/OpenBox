# STT quality and retry policy

STT compares normalized intended dialogue with actual speech. Punctuation,
whitespace, and common filler particles are ignored. The default similarity
threshold is 0.90, but a two-character omission/insertion or even a
single-character replacement remains `suspect` if the overall ratio passes.
Short Chinese substitutions such as `出片→出花` often preserve a high ratio but
change the meaning and must not be accepted automatically.

For every segment show:

- the attached generated video;
- intended dialogue;
- actual transcript;
- similarity and `ok`/`suspect`;
- phrase-level notes.

An `ok` segment is accepted automatically but still appears in the quality
confirmation. A suspect segment requires either selective regeneration or an
explicit user override. If regenerating, create a segment revision first; the
new revision receives new generation/STT idempotency keys and reopens plan/spend
approval. Never overwrite or pretend the paid old take did not exist.

For an unchanged line, `revise_segment` copies the approved dialogue and prompt
into a new planned revision. When shortening or correcting the line, provide
both the replacement `script_text` and the full replacement `segment_prompt`.
The operation changes only that ordinal, preserves all other active generated
segments, updates the full-script hash, and therefore reopens script approval.
Do not call `set_script` or rebuild all segments for a one-segment correction.

Retries:

- provider submit timeout/connection loss may be ambiguous: do not resubmit;
- same generation key + same request reconciles the existing job;
- same key + different request is a hard conflict;
- a definite creative bad take uses a new segment revision after approval;
- STT provider failure retains extracted audio, but retry is explicit;
- render failures may use `video_render(action="retry")` because they do not
  regenerate billed Seedance footage.

Subtitles use accepted actual transcripts, never intended dialogue. This keeps
visible words aligned with the audio when Seedance changes a particle or phrase.
