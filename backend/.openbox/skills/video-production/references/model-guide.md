# Choosing a model, and holding a look steady

`video_generate(action="models")` prints the live registry — ids, resolutions,
ratios, duration ranges and which extras each model accepts. **Read it there.**
The relay publishes no model list of its own, so the registry is the only
description that exists, and a copy in this file would drift.

What the registry cannot tell you:

| Situation | Choose |
|---|---|
| **Anything with a fixed presenter or product** | **A model that lists reference-image support.** This outranks every other consideration: a model without it invents a new person per shot |
| A shot with no reference material | The configured default — widest parameter range: 2–30s, six ratios, seed |
| A quick look at whether an idea works | The cheapest fast tier at 480p, then regenerate the keeper at full resolution |
| Final delivery | A 1080p tier |
| The shot uses a reference **video** | Not the 720p tier — it drops video references upstream, and you pay for a take that ignored them |

Always `action="estimate"` first on anything unusual. It runs the full
validation and costs nothing.

## Keeping the presenter identical across shots

In order of strength:

0. **Use a model that can actually receive a reference image.** Everything
   below is worthless without this. `action="models"` marks the ones that
   cannot; measured behaviour, not a vendor feature list, decides that flag.
1. **The same reference image on every shot**, and a prompt that describes the
   *action* rather than the person. "画面中的人物自然看向镜头" beats a paragraph
   re-describing her face and clothes — a full description competes with the
   photo and the model will happily follow the words instead.
2. **One `seed` reused across shots**, on a model that accepts one.
3. **`last_frame` of shot N as the `first_frame` of shot N+1** — the strongest
   continuity there is, and currently unreachable: it needs the task endpoint,
   which this gateway does not expose.

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
