# Asset and identity contract

- Inputs must be ready OSS `asset_id` values owned by the current user.
- `character_reference_asset` is one image and is always provider reference
  image 1. Generate it once if needed and reuse the exact ID for all segments.
- A recognizable real person requires an explicit, user-owned LivenessFace
  identity. Run `video_identity create → status → add_asset`, wait for each
  active state, and pass both `character_reference_type=real_person` and the
  returned `character_identity_id` to `set_segments`. Never retry the same face
  through AIGC or weaker angles after a privacy rejection.
- AI-generated, illustrated, and virtual hosts use
  `character_reference_type=virtual`. The backend materializes these and all
  ordinary scene/prop references into a per-user AIGC group and submits only
  stable provider `asset://` URIs.
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
credentials remain on the backend. `BytedToken` is backend-only and is cleared
after authorization succeeds or expires. Cached input identity is
bucket/key/size, and per-attempt files are removed on all terminal paths.
