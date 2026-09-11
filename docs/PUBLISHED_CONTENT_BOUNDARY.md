# Published-content deletion boundary

A publishing run must not infer permission to delete existing works. The boundary
in `backend/AGENTS.md` is loaded by `instruction_system_with_config` on every model
step. It requires explicit approval of identified works, stable target IDs,
one deletion per call, read-only result checks, and stopping when targets disappear
or the user reports missing content. Browser and both Douyin publishing skills
repeat the same constraints at the point of use.

This is a model instruction boundary, not a tool-level authorization engine. It
does not make arbitrary Bash, direct CDP, or raw API deletion technically impossible.
No destructive platform operation was executed during verification.

## Validation

- `tests/unit/test_published_content_boundary.py`: 6 model-family prompt builds
  include the actual shipped policy through the instruction loader.
- Live gw2 prompt builds: Gemini 3.8 Flash, GPT 5.6 Luna, and Qwen 3.8 Max include it.
- Existing-worker path verified with `config.instructions=[]`, proving the live
  `/app/AGENTS.md` discovery works without restarting the backend.

## Production activation on 2026-09-11

The policy was hot-loaded at 17:38 Beijing time without restarting services or
changing model routes. The source for deployment was `origin/main@459e899` in the
dedicated `codex/published-content-boundary` worktree.

- Live copy: `/app/AGENTS.md` in `openbox-backend-1`.
- Durable copy: `/tmp/openbox-blobs/policies/published-content.md` on the existing
  `openbox_blob-data` Docker volume.
- `/opt/openbox/config/openbox.json` has the durable path appended to its
  `instructions` list. No existing entries were removed. Fresh workers and
  replacement containers therefore load the policy even on the previous image.
- Config backup: `/opt/openbox/backups/20260911-published-content-boundary/openbox.json.before`.
- Policy SHA-256: `82c7b4df7954d7549f23800bb98d6942c66d23c55b5a1cd84388402b8b2288a3`.

The current worker sees the hot copy. A fresh worker can see both copies. Once an
image containing `backend/AGENTS.md` is deployed and verified, remove the emergency
instruction-list entry to avoid duplicate prompt text. Preserve unrelated current
configuration rather than restoring the whole historical backup.

The skill-file edits become available through the normal image/runtime skill
delivery path; the globally injected rule is already active independently of them.
