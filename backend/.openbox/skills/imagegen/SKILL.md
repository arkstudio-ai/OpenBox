---
name: imagegen
description: Generate or edit raster images through OpenBox for image creation, image-to-image changes, compositing, illustrations, mockups, or other bitmap assets.
allowed-tools:
  - image_gen
---

# OpenBox Image Generation

Use the built-in `image_gen` tool for image generation and editing. The tool
reads its provider, model, defaults, API key, and base URL from `openbox.json`;
never copy credentials into a command, prompt, or workspace file.

Loading this skill activates `image_gen` only for the current agent run. Its
schema is intentionally absent from ordinary conversations.

## Storage contract

OpenBox uses OSS as the durable source of truth for image work:

- Text-to-image results are uploaded to OSS, indexed in `file_assets`, shown as
  image cards in the current reply, and listed under model output in the
  resource centre.
- When a sandbox is available, the same OSS object is also pulled to the
  logical `path` returned by the tool, normally
  `/workspace/generated_images/<name>`.
- Image-to-image inputs are read from OSS. Pass either an `asset_id` or the
  `/workspace/uploads/<name>` path shown in the message.
- If a source exists only as a local workspace file, call `view_image` once to
  stage it to OSS, then pass the returned `asset_id` to `image_gen`.
- `image_gen` already attaches every result. Do not call `view_image` or
  `share_file` on its outputs, because that creates duplicate OSS resources.

For follow-up edits, prefer the returned `asset_id`; filenames can be
ambiguous across conversations.

## Choose generation or editing

- No `input_images`: generate a new image from text.
- One or more `input_images`: edit, restyle, preserve, or composite those
  images with `/images/edits`.
- A supplied image used only for style, composition, mood, or subject guidance
  is still passed in `input_images`; explain its reference role in the prompt.
- `mask_image` is optional, must be a PNG OSS resource, and applies to the
  first input image. Masking is prompt-guided, not guaranteed pixel-perfect.

Label every input inside the prompt, for example:

```text
Input images: Image 1: edit target; Image 2: style reference
```

## Workflow

1. Decide whether the request is a new generation or an edit.
2. Identify each input image and its role. Use an attachment path when that is
   what the user supplied; do not ask for an asset ID unnecessarily.
3. Shape the user's request into a concise production prompt. Preserve a
   detailed prompt rather than adding new creative requirements.
4. Call `image_gen` once for one asset or for variants of the same prompt.
5. Inspect the image attached on the following model turn. Check subject,
   composition, text, and edit invariants before claiming success.
6. Iterate with one targeted change and repeat all invariants that must remain
   fixed.
7. Tell the user what was produced. The image card and resource-centre entry
   already provide preview and download access.

For several distinct assets, use separate `image_gen` calls with separate
prompts. Use `n` only for variants of one prompt.

## Prompt shaping

Use only the labels that improve the request:

```text
Use case: <category>
Asset type: <where the image will be used>
Primary request: <the user's request>
Input images: <Image 1 role; Image 2 role> (when editing)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo, illustration, 3D, etc.>
Composition/framing: <viewpoint and layout>
Lighting/mood: <lighting and mood>
Color palette: <only when requested or implied>
Text (verbatim): "<exact text>"
Constraints: <what must remain and what must not appear>
Avoid: <negative constraints>
```

Useful categories include `photorealistic-natural`, `product-mockup`,
`ui-mockup`, `infographic-diagram`, `scientific-educational`,
`ads-marketing`, `productivity-visual`, `logo-brand`, `illustration-story`,
`stylized-concept`, and `historical-scene`.

Prompt rules:

- Order complex prompts as scene/backdrop → subject → details → constraints →
  intended use.
- Add framing, useful negative space, or polish hints only when they materially
  help the requested asset.
- Do not invent characters, props, brand palettes, slogans, or story beats.
- Quote required in-image text verbatim and specify placement and typography.
  For uncommon words, spell them letter by letter. Use medium/high quality for
  dense labels.
- For realistic photos, name the photographic intent and concrete textures;
  avoid synthetic studio polish unless requested.

## Editing invariants

For edits, explicitly say `change only X; keep Y unchanged` and repeat the
invariants on every iteration.

- Identity-sensitive work: preserve face, body shape, pose, hair, expression,
  and identity unless the user asks otherwise.
- Object replacement: preserve framing, surrounding texture, lighting,
  perspective, and shadows.
- Lighting/weather changes: preserve geometry, composition, and subject.
- Text localization: change only the text; preserve layout, typography,
  spacing, hierarchy, logos, and imagery.
- Compositing: name which image supplies the base and subject; match lighting,
  perspective, and scale.
- Sketch-to-render: preserve layout, proportions, and perspective; do not add
  unrequested elements.

## Configured `gpt-image-2` controls

Omit `size`, `quality`, and `output_format` when the defaults in
`openbox.json` are suitable.

- Quality: `low`, `medium`, `high`, or `auto`. Use `low` for quick drafts and
  `medium`/`high` for final assets, dense text, diagrams, or identity-sensitive
  edits.
- Format: `png`, `jpeg`, or `webp`. Compression applies only to JPEG/WebP.
- Size: `auto` or `WIDTHxHEIGHT`. For `gpt-image-2`, both edges must be
  multiples of 16, the longest edge must be at most 3840px, aspect ratio at
  most 3:1, and total pixels between 655,360 and 8,294,400.
- Common sizes: `1024x1024`, `1536x1024`, `1024x1536`, `2048x2048`,
  `2048x1152`, `3840x2160`, and `2160x3840`. Square drafts are typically
  fastest.
- `gpt-image-2` always uses high-fidelity image inputs; there is no
  `input_fidelity` argument.
- The configured `gpt-image-2` API does not provide native
  `background=transparent`. Do not silently switch models. Use an opaque or
  explicitly requested chroma-key background, or explain that another model
  must be configured for native alpha.

## Boundaries and failure handling

- Use this skill for raster output. Edit established SVG/vector/code-native
  assets directly instead of rasterizing them.
- Never bypass `image_gen` with ad-hoc HTTP, SDK, or shell calls; doing so loses
  OSS ownership checks, chat attachment creation, and resource-centre indexing.
- Do not retry a failed provider call automatically: an ambiguous retry may
  create and bill a second image. Report the error and let the user or agent
  decide whether to try again.
- If OSS is unavailable, report that image generation cannot produce a usable
  OpenBox resource; do not leave the only output in a temporary directory.
