# Skill Provider lifecycle and scope contract

OpenBox resolves Skills through lifecycle-owned providers. Agent/session paths
must construct a `ScopeKey(user_id, project_id, workdir)` from already-resolved
session state; providers never infer a project from the backend process cwd.

## Provider contract

`backend/skill/provider.py` defines `SkillProvider` with:

- stable `id` and integer `rank`;
- `revision(scope)` and `observe(scope)` for body-free discovery;
- `list(scope)` as the provider-level listing API;
- `load(scope, candidate, revision=...)` for on-demand bodies;
- explicit `invalidate(scope)` and `dispose()` lifecycle hooks.

Every observation carries `complete`, `revision`, `diagnostics`, and
`available`. A cold provider failure produces an explicitly unavailable
catalog. An incomplete observation never replaces the registry's last-known
good (LKG) snapshot.

The standard registry mounts four sources:

| Provider | Layer | Default rank |
|---|---:|---:|
| `host-project` | exact project workdir | 100 |
| `personal-user-library` | exact user | 200 |
| `wuying-scoped` | exact user / tenant-scoped Action Server | 400 |
| `host-builtin` | global | 600 |

Nearest scope wins before rank. Within the same scope, lower rank wins, then
provider id, then provider candidate stable id. Every discarded duplicate is
reported as a deterministic conflict diagnostic.

## Snapshot and loading invariant

The model-facing listing and loader share one `SkillCatalogSnapshot`. The
snapshot retains the selected provider id, provider revision, locator, and
scope without loading Skill bodies. At tool execution, the registry verifies:

1. the calling scope is the selected scope or its safe descendant;
2. the selected provider registration still exists;
3. provider revision is unchanged before and after body loading;
4. loaded name/install identity still matches the selected candidate.

Any mismatch returns a refresh-required error instead of loading from another
provider or tenant. This closes the catalogue/body TOCTOU gap.

## Cache and lifecycle

Completed cache keys contain the full `ScopeKey`, registry epoch, and every
provider revision. Concurrent observations for one key share a single in-flight
task. Explicit installation/removal invalidates the sandbox catalogue and the
two sandbox-backed Skill providers. The bounded TTL is only a fallback for
external filesystem or remote changes without a mutation notification.

Both completed-cache and LKG scope maps use the same bounded LRU limit.
Provider revisions, candidate counts, names, descriptions, sources, stable
ids, locators, paths, tool declarations, diagnostics, and metadata all have
hard bounds. Remote LKG rows retain only a body-free field allowlist; arbitrary
remote fields and Skill bodies are never copied into the provider cache.
Filesystem observations recheck their revision after every candidate has been
read, so a mixed pre/post-edit scan is incomplete and cannot replace LKG.

Registries are owned by their tenant-scoped `SandboxClient`; they are not kept
in a process-global tenant map. `register()`, `unregister()`, and `dispose()`
make test teardown and runtime client teardown explicit. The historical
host-only `skill.skill.list_skills()` / `get_skill()` calls remain available for
management integrations, while Agent resolution uses the scoped provider API.
