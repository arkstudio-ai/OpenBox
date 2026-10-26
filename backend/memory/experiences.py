"""Private procedural experience: evidence-backed candidate, human validation, enable.

Reading/loading a skill is never execution success. Enabled procedures remain
subject to normal tool and membership authorization; nothing here runs a tool
or publishes a marketplace release.
"""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from core.config import get_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory_protocol import MemoryExperience
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.service import MemoryConflict, lock_memory_authority, read_source_in_scope
from memory.versions import changed
from memory.redaction import json_hash, sensitive_kind


class Procedure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    conditions: str = Field(min_length=1, max_length=1000)
    steps: list[str] = Field(min_length=1, max_length=12)
    expected_result: str = Field(min_length=1, max_length=1000)
    failure_boundaries: str = Field(min_length=1, max_length=1000)
    test_plan: str = Field(min_length=1, max_length=1000)


def checked_content(content):
    value = Procedure.model_validate(content).model_dump()
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > 8000 or any(not step.strip() or len(step) > 1000 for step in value["steps"]) or sensitive_kind(text):
        raise ValueError("Experience content exceeds its budget or includes private credentials")
    return value


def _enabled(user_id):
    if not get_config().memory.enabled("experience_learning", user_id):
        raise MemoryAccessDenied("Experience learning is unavailable")


async def sources_current(db, scope, refs):
    if not refs:
        return False
    for ref in refs:
        item = await read_source_in_scope(db, access=scope, source_id=ref["id"], source_revision=ref["revision"])
        if not item or not item.get("available", item.get("body_available", False)):
            return False
        if item.get("content_hash") != ref["content_hash"]:
            return False
    return True


async def propose_in_session(db, scope, *, operation_id, content, sources):
    _enabled(scope.user_id)
    content = checked_content(content)
    refs = [{"id": row.id, "revision": row.source_revision, "content_hash": row.content_hash} for row in sources]
    old = await db.scalar(select(MemoryExperience).where(MemoryExperience.user_id == scope.user_id,
        MemoryExperience.operation_id == operation_id))
    if old:
        if old.content != content or old.source_refs != refs:
            raise MemoryConflict("Experience operation already has different evidence")
        return old
    now = datetime.now(timezone.utc)
    row = MemoryExperience(id=ascending("experience"), user_id=scope.user_id, workspace_id=scope.workspace_id,
        project_id=scope.project_id, operation_id=operation_id, revision=1, state="candidate", content=content,
        source_refs=refs, validation={}, created_at=now, updated_at=now)
    db.add(row)
    await db.flush()
    await changed(db, scope.user_id)
    return row


async def _row(db, user_id, workspace_id, identity, *, lock=False):
    _enabled(user_id)
    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    stmt = select(MemoryExperience).where(MemoryExperience.id == identity, *scope.predicates(MemoryExperience))
    row = await db.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise MemoryAccessDenied("Experience is unavailable")
    return row, scope


def view(row, *, sources_available):
    return {"id": row.id, "revision": row.revision,
        "state": row.state if sources_available else "needs_review", "project_id": row.project_id,
        "content": row.content if sources_available else None, "source_refs": row.source_refs, "sources_available": sources_available,
        "validation": row.validation if sources_available else {},
        "skill_id": row.skill_id, "updated_at": row.updated_at.isoformat()}


async def revise(*, user_id, workspace_id, identity, expected_revision, content):
    content = checked_content(content)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row, scope = await _row(db, user_id, workspace_id, identity, lock=True)
        if row.revision != expected_revision:
            raise MemoryConflict("Experience changed; reload before editing")
        if not await sources_current(db, scope, row.source_refs):
            raise MemoryConflict("Original evidence is no longer available")
        row.content, row.validation, row.state = content, {}, "candidate"
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        await changed(db, user_id)
        return view(row, sources_available=True)


async def listing(*, user_id, workspace_id, project_id=None, offset=0):
    _enabled(user_id)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(MemoryExperience).where(*scope.predicates(MemoryExperience))
            .order_by(MemoryExperience.updated_at.desc(), MemoryExperience.id).offset(offset).limit(21))).all())
        return {"items": [view(row, sources_available=await sources_current(db, scope, row.source_refs)) for row in rows[:20]],
            "next_offset": offset + 20 if len(rows) > 20 else None}


async def review(*, user_id, workspace_id, identity, expected_revision, passed, notes):
    """A direct authenticated UI attestation, explicitly labelled as such.

    This is not offered as a model tool and does not claim automated execution.
    Failure keeps the candidate disabled, with its reproducible test notes.
    """
    if not 10 <= len(notes.strip()) <= 3000 or sensitive_kind(notes):
        raise ValueError("Describe what you checked and the observed result")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row, scope = await _row(db, user_id, workspace_id, identity, lock=True)
        if row.revision != expected_revision or row.state not in {"candidate", "validated"}:
            raise MemoryConflict("Experience changed; reload before reviewing")
        if not await sources_current(db, scope, row.source_refs):
            raise MemoryConflict("Experience evidence changed or was forgotten")
        row.validation = {"method": "owner_test_attestation", "actor": user_id, "passed": passed,
            "notes": notes.strip(), "content_hash": json_hash(row.content), "source_hash": json_hash(row.source_refs),
            "at": datetime.now(timezone.utc).isoformat()}
        row.state = "validated" if passed else "candidate"
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        await changed(db, user_id)
        return view(row, sources_available=True)


def archive(row):
    name = "experience-" + row.id[-20:].lower()
    value = row.content
    text = "---\nname: " + name + "\ndescription: " + json.dumps(value["title"], ensure_ascii=False) + "\n---\n\n"
    text += ("# " + value["title"] + "\n\nApplicable conditions:\n" + value["conditions"] + "\n\n"
        + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(value["steps"]))
        + "\n\nExpected result:\n" + value["expected_result"] + "\n\nStop/failure boundaries:\n"
        + value["failure_boundaries"] + "\n\nValidation:\n" + value["test_plan"]
        + "\n\nThis private experience does not grant execution, payment, publication or account permissions.\n")
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as zipped:
        info = ZipInfo(name + "/SKILL.md", date_time=(1980, 1, 1, 0, 0, 0))
        zipped.writestr(info, text.encode())
    return name, output.getvalue()


async def enable(*, user_id, workspace_id, identity, expected_revision):
    from skill.user_library import upsert_personal_snapshot
    from db.models.user_skill import UserSkill
    async with get_db_session() as db:
        row, scope = await _row(db, user_id, workspace_id, identity)
        if row.state == "enabled" and row.revision == expected_revision:
            return view(row, sources_available=await sources_current(db, scope, row.source_refs))
        if row.state != "validated" or row.revision != expected_revision or not row.validation.get("passed"):
            raise MemoryConflict("Validate this exact version before enabling it")
        if (row.validation.get("content_hash") != json_hash(row.content)
                or row.validation.get("source_hash") != json_hash(row.source_refs)
                or not await sources_current(db, scope, row.source_refs)):
            raise MemoryConflict("The validation or original evidence is no longer current")
        name, data = archive(row)
        existing = await db.scalar(select(UserSkill).where(UserSkill.owner_id == user_id,
            UserSkill.workspace_id == workspace_id, UserSkill.name == name))
        if existing and existing.archive_sha256 not in {sha256(data).hexdigest(), row.skill_hash}:
            raise MemoryConflict("The private skill was edited independently")
    # A crash here can leave only an inert private draft. Retry uses identical
    # bytes; no task or published snapshot is created and no user skill is replaced.
    skill = await upsert_personal_snapshot(user_id, {"name": name, "description": row.content["title"],
        "install_dir": name}, data, workspace_id)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row, scope = await _row(db, user_id, workspace_id, identity, lock=True)
        if row.state == "enabled" and row.skill_hash == sha256(data).hexdigest():
            return view(row, sources_available=await sources_current(db, scope, row.source_refs))
        if row.revision != expected_revision or row.state != "validated" or not await sources_current(db, scope, row.source_refs):
            raise MemoryConflict("Experience changed before enable committed")
        row.state, row.skill_id, row.skill_hash = "enabled", skill["id"], sha256(data).hexdigest()
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        await changed(db, user_id)
        return view(row, sources_available=True)


async def retire(*, user_id, workspace_id, identity, expected_revision):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row, scope = await _row(db, user_id, workspace_id, identity, lock=True)
        if row.revision != expected_revision:
            raise MemoryConflict("Experience changed; reload before stopping it")
        row.state, row.revision, row.updated_at = "retired", row.revision + 1, datetime.now(timezone.utc)
        await changed(db, user_id)
        return view(row, sources_available=await sources_current(db, scope, row.source_refs))


async def read_enabled(db, scope, *, query="", offset=0, refs=(), matched_only=False):
    from db.models.user_skill import UserSkill
    _enabled(scope.user_id)
    statement = select(MemoryExperience).where(*scope.predicates(MemoryExperience), MemoryExperience.state == "enabled")
    wanted = {ref["id"]: ref.get("revision") for ref in refs if isinstance(ref, dict) and isinstance(ref.get("id"), str)}
    if refs:
        statement = statement.where(MemoryExperience.id.in_(wanted))
    rows = list((await db.scalars(statement.order_by(MemoryExperience.updated_at.desc(), MemoryExperience.id)
        .limit(201))).all())
    scanned_more = len(rows) > 200
    rows = rows[:200]
    if query and not refs:
        from memory.index.lexical import bm25, tokenize
        texts = [(row.content["title"] + "\n" + row.content["conditions"]) if matched_only
                 else json.dumps(row.content, ensure_ascii=False) for row in rows]
        ranking = bm25(query, texts)
        if matched_only:
            # Discovery is a bounded hint, not a claim that the procedure applies.
            # Ignore single-character CJK overlap; common characters alone must
            # not insert unrelated private methods into every permitted request.
            terms = {term for term in tokenize(query) if len(term) > 1}
            ranking = [(index, score) for index, score in ranking if terms.intersection(tokenize(texts[index]))]
        selected = {index for index, _ in ranking}
        rows = [rows[index] for index, _ in ranking] + ([] if matched_only else
            [row for index, row in enumerate(rows) if index not in selected])
    elif matched_only and not refs:
        rows = []
    rows = rows[offset:]
    items = []
    for row in rows[:8]:
        if wanted and wanted.get(row.id) not in (None, row.revision):
            continue
        skill = await db.get(UserSkill, row.skill_id)
        if (skill is None or skill.owner_id != scope.user_id or skill.workspace_id != scope.workspace_id
                or skill.archive_sha256 != row.skill_hash or not await sources_current(db, scope, row.source_refs)):
            continue
        items.append({"id": row.id, "revision": row.revision, "project_id": row.project_id,
            "skill_id": row.skill_id, "skill_version": skill.version, "content": row.content,
            "validation_method": row.validation.get("method"), "source_refs": row.source_refs})
    return {"kind": "experience", "status": "partial" if scanned_more else "complete", "items": items,
        "scan_limited": scanned_more,
        "next_offset": offset + 8 if len(rows) > 8 else None,
        "instruction": "These are owner-validated private procedures. Match the stated conditions; they never authorize tool execution."}
