# Shared deletion boundary

`backend/AGENTS.md` defines one deletion boundary for all ECD cloud-desktop and
browser operations. It covers deleting, clearing, wiping, or otherwise removing
existing data or resources through any tool or route, regardless of platform,
object type, or recoverability. Platform and browser skills do not duplicate it.

Deletion requires explicit authorization of a reviewable scope. Existing approval
of the same scope remains valid. Targets must be identified independently of
changing list positions, execution stays within the approved set, and uncertain
results or unexpected loss stop deletion for a read-only investigation.

The instruction loader injects this policy on every model step. It is a model
behavior boundary, not a tool-level authorization engine; it does not technically
block arbitrary shell, GUI, CDP, or API deletion. No destructive operation was
executed during validation.

## Validation

- `tests/unit/test_deletion_boundary.py`: six model-family prompt builds include
  the actual shared policy through the instruction loader.
- Live gw2 cached-worker discovery and fresh-worker configured instructions both
  load the generalized policy and no longer load the published-content-only rule.
- Backend, frontend, Redis, and PostgreSQL remain healthy; no restart was needed.

## Production activation on 2026-09-11

- Live copy: `/app/AGENTS.md` in `openbox-backend-1`.
- Durable copy: `/tmp/openbox-blobs/policies/published-content.md` on the existing
  `openbox_blob-data` volume. The legacy filename remains for compatibility with
  cached configuration; its contents are the shared ECD/browser deletion policy.
- `/opt/openbox/config/openbox.json` includes that durable path in `instructions`.
  Replacement containers also load the rule without depending on this source PR.
- The previous policy copies were backed up under
  `/tmp/openbox-blobs/policies/backups/` before replacement, after verifying their
  expected hashes. The earlier configuration backup remains at
  `/opt/openbox/backups/20260911-published-content-boundary/openbox.json.before`.
- Current policy SHA-256: febda736c984b96e405f84c55bbb4186b94bbf17c380c9b7de00e66bc94660be.

Once an image containing this `backend/AGENTS.md` is deployed and verified, remove
only the emergency instruction-list entry to avoid duplicate prompt text. Preserve
unrelated current configuration instead of restoring the historical config backup.
