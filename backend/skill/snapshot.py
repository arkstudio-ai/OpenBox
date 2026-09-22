"""Owner-scoped, content-addressed Skill knowledge for frozen Agent versions.

Uses the Skill snapshot store independently of Trace recording.
Skill text is never interpreted as a tool or permission grant.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import PurePosixPath

from skill.provider import ScopeKey, SkillCatalogSnapshot, SkillDefinition, SkillScopeMismatch, skill_registry_for
from team.errors import TeamError
from team.journal import digest
from skill.storage import get_blob_store

MAX_BODY_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 8 * MAX_BODY_BYTES


def namespace(actor) -> str:
    return "team-skills/" + digest([actor.owner_user_id, actor.workspace_id]) + "/"


async def put_content(actor, data: bytes, *, max_bytes: int = MAX_BODY_BYTES) -> tuple[str, str]:
    if len(data) > max_bytes:
        raise TeamError("SKILL_SNAPSHOT_TOO_LARGE", "A Skill body or resource exceeds the 1 MiB snapshot limit.", status=422)
    sha = hashlib.sha256(data).hexdigest()
    key = namespace(actor) + sha
    try:
        await get_blob_store().put(key, data, content_type="application/json", if_absent=True)
    except (OSError, RuntimeError) as exc:
        raise TeamError("SKILL_SNAPSHOT_UNAVAILABLE", "Skill snapshot storage is unavailable. Keep the draft and retry after storage is restored; changing the Agent configuration will not fix this error.", status=503) from exc
    return key, sha


async def read_content(actor, entry: dict) -> dict:
    sha = entry.get("content_digest", "")
    if entry.get("blob_key") != namespace(actor) + sha or len(sha) != 64:
        raise TeamError("SKILL_SNAPSHOT_INVALID", "The Skill snapshot belongs to another scope.", status=403)
    try:
        data = await get_blob_store().get(entry["blob_key"])
    except (OSError, RuntimeError) as exc:
        raise TeamError("SKILL_SNAPSHOT_UNAVAILABLE", "The frozen Skill snapshot cannot be read. Restore its storage before resuming.", status=503) from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise TeamError("SKILL_SNAPSHOT_INVALID", "The stored Skill snapshot failed its content check.")
    try:
        material = json.loads(data)
        if not isinstance(material, dict):
            raise ValueError("Snapshot must be an object")
        return material
    except (ValueError, TypeError) as exc:
        raise TeamError("SKILL_SNAPSHOT_INVALID", "The stored Skill snapshot is malformed.") from exc


async def freeze_specs(specs, actor, *, sandbox=None, registry=None, scope: ScopeKey | None = None) -> list[dict]:
    """Resolve one catalog for a compilation, loading only requested knowledge."""
    refs = {(ref.name, ref.source) for spec in specs for ref in spec.skill_refs}
    all_accessible = any(spec.skill_mode == "all_accessible" for spec in specs)
    if not refs and not all_accessible:
        return []
    if registry is None:
        if sandbox is None:
            from sandbox.manager import sandbox_manager
            sandbox = await sandbox_manager.get_client_any(user_id=actor.owner_user_id, workspace_id=actor.workspace_id)
        registry = skill_registry_for(sandbox)
    scope = scope or ScopeKey(user_id=actor.owner_user_id)
    if scope.user_id != actor.owner_user_id:
        raise SkillScopeMismatch("Cannot freeze another owner's Skill scope")
    snapshot = await registry.snapshot(scope)
    if not snapshot.available:
        raise TeamError("SKILL_CATALOGUE_UNAVAILABLE", "The Skill catalog cannot be verified; retry when it is available.", status=503)
    # Explicit references freeze at admission. all_accessible discovers other
    # Skills live and freezes each body only on its first actual read.
    selected = [skill for skill in snapshot.skills if (skill.name, skill.source) in refs or (skill.name, None) in refs]
    for name, source in refs:
        if not any(skill.name == name and (source is None or skill.source == source) for skill in selected):
            raise TeamError("AGENT_NOT_ACCESSIBLE", f"Skill {name!r} is not accessible in this workspace.", status=422)
    if len(selected) > 256:
        raise TeamError("SKILL_SNAPSHOT_TOO_LARGE", "Select at most 256 Skills for one Agent.", status=422)
    entries, total = [], 0
    for listed in selected:
        skill = await registry.load(snapshot, listed.name, scope=scope)
        if skill is None:
            raise TeamError("SKILL_SNAPSHOT_INVALID", "A selected Skill disappeared during compilation.")
        material = asdict(skill)
        if skill.path and not skill.base_dir:
            from tool.knowledge.skill_tool import _host_files
            material["files"] = _host_files(skill.path, limit=2000)
        data = json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        total += len(data)
        if total > MAX_CATALOG_BYTES:
            raise TeamError("SKILL_SNAPSHOT_TOO_LARGE", "Selected Skill contents exceed the 8 MiB total limit.", status=422)
        key, sha = await put_content(actor, data)
        entries.append({"name": skill.name, "source": skill.source, "description": skill.description,
                        "allowed_tools": list(skill.allowed_tools), "content_digest": sha, "blob_key": key,
                        "scope": asdict(scope)})
    return entries


class FrozenSkillRegistry:
    """Adapter for the existing Skill tool; no alternative loader or fallback."""
    def __init__(self, actor, entries: list[dict], live_registry, *, instance_id: str = "", all_accessible: bool = False):
        self.actor, self.entries, self.live_registry = actor, entries, live_registry
        self.instance_id = instance_id
        self.all_accessible = all_accessible

    async def snapshot(self, scope: ScopeKey) -> SkillCatalogSnapshot:
        if scope.user_id != self.actor.owner_user_id:
            raise SkillScopeMismatch("Frozen Skills belong to another owner")
        live = await self.live_registry.snapshot(scope)
        allowed = {(skill.name, skill.source) for skill in live.skills}
        # Unavailability must fail closed; absence after revocation does not
        # expose a formerly accessible body through its historical snapshot.
        skills = tuple(SkillDefinition(name=entry["name"], description=entry.get("description", ""),
                       source=entry["source"], content="") for entry in self.entries
                       if (entry["name"], entry["source"]) in allowed and self._same_scope(entry, scope))
        if self.all_accessible:
            frozen_names = {(skill.name, skill.source) for skill in skills}
            skills += tuple(replace(skill, content="") for skill in live.skills
                if (skill.name, skill.source) not in frozen_names)
        return SkillCatalogSnapshot(scope, skills, live.complete, digest(self.entries), live.diagnostics,
                                    available=live.available, stale=live.stale)

    @staticmethod
    def _same_scope(entry, scope):
        frozen = entry.get("scope") or {}
        return all(not frozen.get(key) or frozen[key] == getattr(scope, key) for key in ("user_id", "project_id", "workdir"))

    async def _entry(self, current, name):
        entry = next((entry for entry in self.entries if entry["name"] == name and self._same_scope(entry, current.scope)), None)
        if entry is not None or not self.all_accessible:
            return entry
        listed = next((skill for skill in current.skills if skill.name == name), None)
        if listed is None or not self.instance_id:
            return None
        key = namespace(self.actor) + "first-read/" + digest([self.instance_id, asdict(current.scope), name, listed.source])
        store = get_blob_store()
        try:
            if not await store.exists(key):
                # Freeze through the same validated loader and byte limits as
                # explicit admission. The conditional reference write selects
                # one winner if two calls read a changing Skill concurrently.
                from agent_catalog.schemas import AgentSpec
                request = AgentSpec(name="Snapshot", description="First Skill read", when_to_use="Internal snapshot",
                    instruction="Read the selected Skill", skill_refs=[{"name": name, "source": listed.source}])
                entries = await freeze_specs([request], self.actor, registry=self.live_registry, scope=current.scope)
                await store.put(key, json.dumps(entries[0]).encode(), content_type="application/json", if_absent=True)
            entry = json.loads(await store.get(key))
            if entry["name"] != name or entry["source"] != listed.source or not self._same_scope(entry, current.scope):
                raise TeamError("SKILL_SNAPSHOT_INVALID", "The first-read Skill reference belongs to another scope.")
            return entry
        except (OSError, RuntimeError) as exc:
            raise TeamError("SKILL_SNAPSHOT_UNAVAILABLE", "The first-read Skill snapshot cannot be stored or read.", status=503) from exc
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            if isinstance(exc, TeamError):
                raise
            raise TeamError("SKILL_SNAPSHOT_INVALID", "The first-read Skill reference is malformed.") from exc

    async def load(self, snapshot, name, *, scope=None):
        scope = scope or snapshot.scope
        if scope != snapshot.scope or scope.user_id != self.actor.owner_user_id:
            raise SkillScopeMismatch("Frozen Skills belong to another scope")
        current = await self.snapshot(scope)
        if not current.available or name not in {skill.name for skill in current.skills}:
            return None
        entry = await self._entry(current, name)
        if entry is None:
            return None
        material = await read_content(self.actor, entry)
        # Avoid re-enumerating mutable host files in the existing renderer.
        return SkillDefinition(**{**material, "path": "", "files": tuple(material.get("files", [])),
                                  "allowed_tools": tuple(material.get("allowed_tools", []))})

    async def resource(self, snapshot, name: str, resource: str, ctx) -> str:
        if ctx.user_id != self.actor.owner_user_id or ctx.workspace_id != self.actor.workspace_id:
            raise SkillScopeMismatch("Frozen Skill resources belong to another workspace")
        if await self.load(snapshot, name, scope=snapshot.scope) is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", "This Skill is no longer accessible.", status=403)
        current = await self.snapshot(snapshot.scope)
        entry = await self._entry(current, name)
        if entry is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", "This Skill is no longer accessible.", status=403)
        material = await read_content(self.actor, entry)
        if resource in {"SKILL.md", "./SKILL.md"}:
            return material["content"]
        path = PurePosixPath(resource)
        if path.is_absolute() or ".." in path.parts or "\\" in resource or "\x00" in resource or str(path) not in material.get("files", []):
            raise TeamError("SKILL_RESOURCE_NOT_ACCESSIBLE", "Choose a relative resource from this Skill's frozen file list.", status=422)
        ref_key = namespace(self.actor) + "resources/" + digest([self.instance_id, entry["content_digest"], str(path)])
        store = get_blob_store()
        from skill.snapshot_resource import encode_resource, read_resource_bytes, render_resource
        try:
            if not await store.exists(ref_key):
                raw = await read_resource_bytes(material, str(path), ctx, MAX_BODY_BYTES)
                data = json.dumps(encode_resource(raw), ensure_ascii=False).encode()
                # JSON escaping and base64 are storage encodings; the resource
                # limit applies to its actual bytes, not their encoded length.
                key, sha = await put_content(self.actor, data, max_bytes=MAX_BODY_BYTES * 6 + 256)
                await store.put(ref_key, json.dumps({"blob_key": key, "content_digest": sha}).encode(), content_type="application/json", if_absent=True)
            ref = json.loads(await store.get(ref_key))
            return render_resource(await read_content(self.actor, ref))
        except (OSError, RuntimeError) as exc:
            raise TeamError("SKILL_SNAPSHOT_UNAVAILABLE", "Skill resource storage is unavailable; no replacement version was used.", status=503) from exc
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, TeamError):
                raise
            raise TeamError("SKILL_SNAPSHOT_INVALID", "The frozen Skill resource reference is malformed.") from exc


class PolicySkillRegistry:
    """Coordinator discovery follows the live run's approved Skill scope."""
    def __init__(self, registry):
        self.registry = registry

    async def snapshot(self, scope):
        from team.runtime_binding import current_grant
        grant = await current_grant()
        snapshot = await self.registry.snapshot(scope)
        allowed = grant.get("allowed_skills")
        if allowed is None:
            return snapshot
        return replace(snapshot, skills=tuple(skill for skill in snapshot.skills if any(
            ref["name"] == skill.name and (ref.get("source") is None or ref["source"] == skill.source) for ref in allowed)))

    async def load(self, snapshot, name, *, scope=None):
        current = await self.snapshot(scope or snapshot.scope)
        if name not in {skill.name for skill in current.skills}:
            return None
        return await self.registry.load(current, name, scope=scope or snapshot.scope)


def bound_registry(registry):
    from team.runtime_binding import current_binding
    from team.journal import Actor
    binding = current_binding()
    if binding is None:
        return registry
    if binding.role == "coordinator":
        return PolicySkillRegistry(registry)
    entries = binding.admission.get("capability_summary", {}).get("skills", [])
    live_registry = PolicySkillRegistry(registry) if binding.role == "member" else registry
    return FrozenSkillRegistry(Actor(binding.owner_user_id, binding.workspace_id), entries, live_registry,
        instance_id=binding.member_id, all_accessible=binding.spec.get("skill_mode") == "all_accessible")
