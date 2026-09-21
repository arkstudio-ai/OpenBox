"""Tenant-scoped, immutable Agent and team-template version storage."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from agent_catalog.schemas import AgentSpec, TeamSpec
from core.identifier import ascending
from db.base import get_db_session
from db.models.team import AgentDefinition, AgentDefinitionVersion, TeamDefinition, TeamDefinitionVersion, TeamRun
from team.errors import TeamError
from team.journal import Actor, digest, utcnow, write_transaction

Kind = Literal["agent", "team"]


def tables(kind: Kind):
    return (AgentDefinition, AgentDefinitionVersion, AgentSpec) if kind == "agent" else (TeamDefinition, TeamDefinitionVersion, TeamSpec)


def _receipt_key(actor: Actor, key: str) -> str:
    if not key or len(key) > 256:
        raise TeamError("IDEMPOTENCY_KEY_REQUIRED", "A stable idempotency key is required.", status=422)
    return digest([actor.owner_user_id, actor.workspace_id, key])


def _definition(row, version=None) -> dict:
    return {"id": row.id, "name": row.name, "source": row.source, "status": row.status,
        "current_version_id": row.current_version_id, "draft_version_id": row.draft_version_id,
        "current_version_number": version.version if version is not None and version.id == row.current_version_id else row.provenance.get("current_version_number"),
        "revision": row.provenance.get("revision", 1),
        "provenance": {key: value for key, value in row.provenance.items() if key not in {"receipts", "revision"}},
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
        **({"version": _version(version)} if version is not None else {})}


def _version(row) -> dict:
    return {"id": row.id, "version": row.version, "spec": deepcopy(row.spec_json),
        "capability_summary": {key: deepcopy(value) for key, value in row.capability_summary.items() if not key.startswith("_")}, "content_digest": row.content_digest,
        "created_at": row.created_at.isoformat()}


async def owned(db, kind: Kind, definition_id: str, actor: Actor, *, lock=False):
    definition, _, _ = tables(kind)
    query = select(definition).where(definition.id == definition_id, definition.owner_user_id == actor.owner_user_id,
        definition.workspace_id == actor.workspace_id)
    if lock:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise TeamError("AGENT_NOT_ACCESSIBLE", "Definition is not accessible in this workspace.", status=404)
    return row


async def get(kind: Kind, definition_id: str, actor: Actor, *, version_id: str | None = None, active_only: bool = False) -> dict:
    async with get_db_session() as db:
        row = await owned(db, kind, definition_id, actor)
        if active_only and row.status != "active":
            raise TeamError("AGENT_NOT_ACCESSIBLE", "Only an enabled definition can join a new team.", status=422)
        _, versions, _ = tables(kind)
        selected = version_id or (row.current_version_id if active_only else row.draft_version_id or row.current_version_id)
        published = set(row.provenance.get("published_version_ids", [])) | {row.current_version_id}
        if active_only and selected not in published:
            raise TeamError("AGENT_NOT_ACCESSIBLE", "A draft version must be published before joining a team.", status=422)
        version = (await db.execute(select(versions).where(versions.id == selected, versions.definition_id == row.id))).scalar_one_or_none()
        if version is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", "This version is not part of the accessible definition.", status=404)
        return _definition(row, version)


async def list_definitions(kind: Kind, actor: Actor, *, search: str = "", status: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> dict:
    definition, versions, _ = tables(kind)
    query = select(definition).where(definition.owner_user_id == actor.owner_user_id, definition.workspace_id == actor.workspace_id)
    query = query.where(definition.status == status) if status else query.where(definition.status != "archived")
    if search:
        query = query.where(definition.name.contains(search, autoescape=True))
    if cursor:
        query = query.where(definition.id < cursor)
    limit = max(1, min(limit, 100))
    async with get_db_session() as db:
        rows = list((await db.execute(query.order_by(definition.id.desc()).limit(limit + 1))).scalars().all())
        def selected(row):
            return row.current_version_id if status == "active" else row.draft_version_id or row.current_version_id
        ids = {selected(row) for row in rows[:limit]}
        summaries = {row.id: row for row in (await db.execute(select(versions).where(versions.id.in_(ids)))).scalars().all()}
        items = [_definition(row, summaries.get(selected(row))) for row in rows[:limit]]
        if kind == "team" and items:
            counts = dict((await db.execute(select(TeamRun.template_id, func.count()).where(
                TeamRun.owner_user_id == actor.owner_user_id, TeamRun.workspace_id == actor.workspace_id,
                TeamRun.template_id.in_([item["id"] for item in items])).group_by(TeamRun.template_id))).all())
            members = [member for item in items for member in item.get("version", {}).get("spec", {}).get("preset_members", [])]
            agents = {row.id: row for row in (await db.scalars(select(AgentDefinition).where(
                AgentDefinition.id.in_([m["agent_ref"] for m in members if m.get("agent_ref")]),
                AgentDefinition.owner_user_id == actor.owner_user_id, AgentDefinition.workspace_id == actor.workspace_id))).all()}
            version_ids = {m.get("version_id") if m.get("version_policy") == "pinned" else agents[m["agent_ref"]].current_version_id
                for m in members if m.get("agent_ref") in agents}
            agent_versions = {row.id: row for row in (await db.scalars(select(AgentDefinitionVersion).where(
                AgentDefinitionVersion.id.in_(version_ids), AgentDefinitionVersion.definition_id.in_(agents)))).all()}
            for item in items:
                item["run_count"] = counts.get(item["id"], 0)
                previews = []
                for member in item.get("version", {}).get("spec", {}).get("preset_members", []):
                    if not member.get("enabled", True):
                        continue
                    spec = member.get("inline") or {}
                    agent = agents.get(member.get("agent_ref"))
                    if agent:
                        version = agent_versions.get(member.get("version_id") if member.get("version_policy") == "pinned" else agent.current_version_id)
                        if version and version.definition_id == agent.id:
                            spec = version.spec_json
                    previews.append({"alias": member["alias"], "name": spec.get("name", member["alias"]), "display": deepcopy(spec.get("display"))})
                item["member_previews"] = previews
        return {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


async def versions(kind: Kind, definition_id: str, actor: Actor, *, before: int | None = None, limit: int = 50) -> dict:
    async with get_db_session() as db:
        definition = await owned(db, kind, definition_id, actor)
        _, model, _ = tables(kind)
        query = select(model).where(model.definition_id == definition_id)
        if before:
            query = query.where(model.version < before)
        rows = (await db.execute(query.order_by(model.version.desc()).limit(min(limit, 100) + 1))).scalars().all()
        published = set(definition.provenance.get("published_version_ids", [])) | {definition.current_version_id}
        return {"items": [{**_version(row), "published": row.id in published} for row in rows[:limit]], "next_cursor": rows[limit - 1].version if len(rows) > limit else None}


async def create(kind: Kind, actor: Actor, key: str, spec: AgentSpec | TeamSpec, *, capability_summary: dict | None = None,
                 source: str = "user", provenance: dict | None = None) -> dict:
    try:
        async with write_transaction() as db:
            return await create_locked(db, kind, actor, key, spec, capability_summary=capability_summary, source=source, provenance=provenance)
    except IntegrityError as exc:
        raise TeamError("DEFINITION_NAME_TAKEN", "An active definition already uses this name.", status=409) from exc


async def create_locked(db, kind: Kind, actor: Actor, key: str, spec: AgentSpec | TeamSpec, *, capability_summary: dict | None = None,
                 source: str = "user", provenance: dict | None = None, publish: bool = False) -> dict:
    from core.config import get_config
    definition, version_model, schema = tables(kind)
    spec = schema.model_validate(spec.model_dump(mode="json"))
    receipt_key = _receipt_key(actor, key)
    identifier = f"{kind}_" + receipt_key[:56]
    request_hash = digest({"spec": spec.model_dump(mode="json"), "source": source, "provenance": provenance or {}, **({"publish": True} if publish else {})})
    # Per-owner lock serializes quota checks as well as retries of a
    # not-yet-created definition across PostgreSQL workers.
    from db.models.user import User
    await db.execute(select(User.id).where(User.id == actor.owner_user_id).with_for_update())
    existing = await db.get(definition, identifier)
    if existing is not None:
        receipt = existing.provenance.get("receipts", {}).get(receipt_key)
        if not receipt or receipt["digest"] != request_hash:
            raise TeamError("IDEMPOTENCY_CONFLICT", "This key already created a different definition.")
        return deepcopy(receipt["result"])
    count = await db.scalar(select(func.count()).select_from(definition).where(definition.owner_user_id == actor.owner_user_id,
        definition.workspace_id == actor.workspace_id, definition.status != "archived"))
    if count >= get_config().team_max_definitions:
        raise TeamError("DEFINITION_LIMIT", "Archive an unused definition before creating another.")
    now, version_id = utcnow(), ascending("version")
    row = definition(id=identifier, owner_user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
        name=spec.name, source=source, status="draft", active_name=1, draft_version_id=version_id,
        provenance={**(provenance or {}), "revision": 1}, created_at=now, updated_at=now)
    version = version_model(id=version_id, definition_id=identifier, version=1,
        spec_json=spec.model_dump(mode="json"), capability_summary=capability_summary or {},
        content_digest=digest(spec.model_dump(mode="json")), created_by=actor.owner_user_id, created_at=now)
    if publish:
        row.status, row.current_version_id, row.draft_version_id = "active", version_id, None
        row.provenance = {**row.provenance, "published_version_ids": [version_id], "current_version_number": 1}
    db.add(row)
    await db.flush()
    db.add(version)
    result = _definition(row, version)
    row.provenance = {**row.provenance, "receipts": {receipt_key: {"digest": request_hash, "result": result}}}
    await db.flush()
    return result


async def mutate(kind: Kind, definition_id: str, actor: Actor, key: str, action: str, expected_revision: int, *,
                 spec: AgentSpec | TeamSpec | None = None, capability_summary: dict | None = None) -> dict:
    _, version_model, schema = tables(kind)
    if action not in {"save_draft", "publish", "activate", "archive"}:
        raise TeamError("INVALID_DEFINITION_ACTION", "Unknown definition action.", status=422)
    material = schema.model_validate(spec.model_dump(mode="json")) if spec else None
    receipt_key = _receipt_key(actor, key)
    request_hash = digest({"action": action, "expected_revision": expected_revision, "spec": material.model_dump(mode="json") if material else None})
    try:
        async with write_transaction() as db:
            row = await owned(db, kind, definition_id, actor, lock=True)
            receipts = deepcopy(row.provenance.get("receipts", {}))
            prior = receipts.get(receipt_key)
            if prior:
                if prior["digest"] != request_hash:
                    raise TeamError("IDEMPOTENCY_CONFLICT", "This key refers to another definition change.")
                return deepcopy(prior["result"])
            revision = row.provenance.get("revision", 1)
            if revision != expected_revision:
                raise TeamError("STALE_REVISION", "Definition changed since it was loaded.", current=_definition(row))
            if row.status == "archived":
                raise TeamError("AGENT_NOT_ACCESSIBLE", "An archived definition must be duplicated before editing.")
            version = None
            if material:
                number = (await db.scalar(select(func.max(version_model.version)).where(version_model.definition_id == row.id)) or 0) + 1
                version = version_model(id=ascending("version"), definition_id=row.id, version=number,
                    spec_json=material.model_dump(mode="json"), capability_summary=capability_summary or {},
                    content_digest=digest(material.model_dump(mode="json")), created_by=actor.owner_user_id, created_at=utcnow())
                db.add(version)
                row.draft_version_id = version.id
                row.name = material.name
            if action == "save_draft" and material is None:
                raise TeamError("INVALID_DEFINITION", "Saving a draft requires a complete spec.", status=422)
            if action in {"publish", "activate"}:
                previous_version_id = row.current_version_id
                row.current_version_id = row.draft_version_id or row.current_version_id
                row.provenance = {**row.provenance, "published_version_ids": list(dict.fromkeys(value for value in [
                    *row.provenance.get("published_version_ids", []), previous_version_id, row.current_version_id] if value))}
                row.draft_version_id = None
                row.status = "active"
            elif action == "archive":
                row.status = "archived"
                row.active_name = None
            row.updated_at = utcnow()
            row.provenance = {**row.provenance, "revision": revision + 1}
            if version is None:
                version = await db.get(version_model, row.draft_version_id or row.current_version_id)
            if action in {"publish", "activate"}:
                row.provenance = {**row.provenance, "current_version_number": version.version}
            result = _definition(row, version)
            receipts[receipt_key] = {"digest": request_hash, "result": result}
            row.provenance = {**row.provenance, "receipts": receipts}
            await db.flush()
            return result
    except IntegrityError as exc:
        raise TeamError("DEFINITION_NAME_TAKEN", "An active definition already uses this name.", status=409) from exc
