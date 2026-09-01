# Choosing a model, and holding a look steady

`video_generate(action="models")` prints the live registry — ids, resolutions,
ratios, duration ranges and which extras each model accepts. **Read it there.**
The relay publishes no model list of its own, so the registry is the only
description that exists, and a copy in this file would drift.

What the registry cannot tell you:

| Situation | Choose |
|---|---|
| Default vertical talking head | The configured default (Wan 3.0 today) — widest parameter range: 2–30s, six ratios, seed, first/last frame |
| A quick look at whether an idea works | The cheapest fast tier at 480p, then regenerate the keeper at full resolution |
| Final delivery | A 1080p tier |
| The shot uses a reference **video** | Not the 720p tier — it drops video references upstream, and you pay for a take that ignored them |

Always `action="estimate"` first on anything unusual. It runs the full
validation and costs nothing.

## Keeping the presenter identical across shots

In order of strength:

1. **The same reference image on every shot**, and a prompt that describes the
   *action* rather than re-describing the person. "画面中的人物自然看向镜头"
   beats a paragraph about her face and clothes — a full description competes
   with the photo. How the reference travels is the backend's problem: it
   picks the shape each model actually honours.
2. **One `seed` reused across shots**, on a model that accepts one. Same seed
   plus same anchor removes most of the remaining drift, for free.
3. **`last_frame` of shot N as the `first_frame` of shot N+1**, on a model that
   accepts frame roles. Strongest continuity available, and the honest way to
   do it — the shots genuinely join.

What not to do: never feed a **generated** clip's frame back as the general
character reference. Each generation drifts a little from the anchor; anchoring
to a drifted frame compounds it, and by the fifth shot it is a different person.

If a role is declared by the model but the gateway cannot carry it, the tool
says so explicitly rather than quietly downgrading it to a plain reference.
That refusal is a real answer: use the seed tactic instead.

## Cost

Billing on this route is per second of generated video, so an explicit
`duration` is the whole cost story — `-1` lets the model choose and is the
right default when you do not care, but a wrong guess is what you pay for.
The daily ceiling is back-pressure, not permission: if it refuses, tell the
person rather than retrying.


## Shot length per model

Measured 2026-09-01 — the gateway passes duration straight through, so the
limit is the vendor's and it refuses out-of-range values outright rather than
clamping them:

| model | seconds | smart (-1) |
|---|---|---|
| Seedance 2.0 / 2.0 Fast | 4–15 | yes |
| SD 480p / 720p / 1080p | 4–15 | **no** |
| Wan 3.0 / Prime | 2–30 | yes |
| MiniMax H3 | 4–15 | no |

`-1` asks the model to choose the length. Where it works it really does
choose: Seedance 2.0 returned 12.05s for a line that would otherwise have got
the 5s default. The three SD tiers accept `-1` and then return exactly 5.06s —
indistinguishable from the default, so treat it as unsupported there and send
a number. Wan 3.0's range is 2–30 on this deployment, wider than the 2–15 in
通义万相 2.7's public docs, and 30s was measured as 30.02s of actual video.

Two consequences for splitting. A line that needs less than the model's floor
gets padded up to it — on Seedance a three-character line still occupies 4s,
so merge it into a neighbour instead. And a line needing more than the ceiling
has to be split: 15s at 4 chars/second is about 55 spoken characters, which is
the real upper bound on one Seedance shot.

Wan 3.0's 2–30 range is the widest by far, and it is the only model that
accepts `-1` for a duration it chooses itself. Prefer it when the script has
lines of very uneven length.
