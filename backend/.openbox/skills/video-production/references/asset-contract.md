# Asset and identity contract

- Inputs must be ready OSS `asset_id` values owned by the current user.
- `character_reference_asset` is one image and is always provider reference
  image 1. Generate it once if needed and reuse the exact ID for all segments.
- Additional `input_assets` preserve the declared order. Number images and
  videos separately in prompt text: the first extra video is `参考视频1`; if a
  host image exists, the first extra image is `参考图片2`.
- Prompt text uses only `参考图片N` / `参考视频N`; never embed `asset://`, an OSS
  URL, a provider URL, or a local path.
- Do not use an earlier generated segment as the new character anchor. It bakes
  generation drift into every later segment.
- Provider outputs are copied immediately to OSS and indexed. Do not manually
  download/re-upload them or use expiring provider URLs for composition.

The WUYING worker receives only short-lived object-scoped URLs. Provider and OSS
credentials remain on the backend. Cached input identity is bucket/key/size, and
per-attempt files are removed on all terminal paths.
