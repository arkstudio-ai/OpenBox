"""Scoped profile management and read projections for typed knowledge."""
from sqlalchemy import select

from core.identifier import ascending
from db.base import get_db_session
from db.models.wiki_workflow import WikiProfile, WikiTypedRecord, WikiWorkflowRun
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.organization import scoped_values
from memory.wiki.service import WikiStateError, domain_for, now
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.profiles import unreachable_states, validate_profile


def view(profile):
    return {"id": profile.id, "revision": profile.revision, "project_id": profile.project_id,
            "title": profile.title, "definition": profile.definition, "definition_hash": profile.definition_hash}


async def get_profile(db, scope, profile_id, *, lock=False):
    stmt = select(WikiProfile).where(WikiProfile.id == profile_id, *scope.predicates(WikiProfile))
    result = await db.scalar(stmt.with_for_update() if lock else stmt)
    if result and canonical_hash(result.definition) != result.definition_hash:
        raise WikiStateError("wiki_profile_changed")
    return result


async def save(*, user_id, workspace_id, project_id, definition, expected_revision, profile_id=None):
    definition = validate_profile(definition)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        domain = domain_for(scope, project_id)
        profile = await get_profile(db, scope, profile_id, lock=True) if profile_id else await db.scalar(select(WikiProfile).where(
            WikiProfile.domain == domain, WikiProfile.profile_key == definition["profileId"]).with_for_update())
        if profile_id and profile is None:
            return None
        if (profile.revision if profile else 0) != expected_revision:
            raise WikiStateError("wiki_profile_changed")
        if profile and (profile.project_id != project_id or profile.profile_key != definition["profileId"]):
            raise WikiStateError("wiki_profile_identity_immutable")
        if profile is None:
            profile = WikiProfile(id=ascending("wiki_profile"), **scoped_values(scope), domain=domain,
                profile_key=definition["profileId"])
            db.add(profile)
        else:
            profile.revision += 1
        profile.title, profile.definition, profile.definition_hash = definition["title"], definition, canonical_hash(definition)
        profile.updated_at = now()
        await db.flush()
        return view(profile)


async def list_profiles(*, user_id, workspace_id, project_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = list((await db.scalars(select(WikiProfile).where(*scope.predicates(WikiProfile))
            .order_by(WikiProfile.updated_at.desc(), WikiProfile.id).offset(offset).limit(21))).all())
        return {"profiles": [view(row) for row in rows[:20]], "next_offset": offset + 20 if len(rows) > 20 else None}


async def detail(*, user_id, workspace_id, profile_id):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        profile = await get_profile(db, scope, profile_id)
        return view(profile) if profile else None


async def records(*, user_id, workspace_id, profile_id, offset=0):
    from memory.wiki.records import record_view
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        profile = await get_profile(db, scope, profile_id)
        if profile is None:
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=profile.project_id)
        rows = list((await db.scalars(select(WikiTypedRecord).where(WikiTypedRecord.profile_id == profile.id,
            *local.predicates(WikiTypedRecord)).order_by(WikiTypedRecord.id).offset(offset).limit(41))).all())
        return {"records": [await record_view(db, local, profile, row) for row in rows[:40]],
                "next_offset": offset + 40 if len(rows) > 40 else None}


async def statistics(*, user_id, workspace_id, profile_id):
    from memory.wiki.records import current_record
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        profile = await get_profile(db, scope, profile_id)
        if profile is None:
            return None
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=profile.project_id)
        rows = list((await db.scalars(select(WikiTypedRecord).where(WikiTypedRecord.profile_id == profile.id,
            *local.predicates(WikiTypedRecord)).limit(2001))).all())
        if len(rows) > 2000:
            raise WikiStateError("wiki_profile_statistics_limit")
        counts = {}
        for key, definition in profile.definition["entities"].items():
            lc = definition.get("lifecycle")
            counts[key] = {"states": {state: 0 for state in definition["fields"][lc["field"]]["enum"]} if lc else {},
                           "total": 0, "unavailable": 0, "unreachable": unreachable_states(definition)}
        for row in rows:
            entry = counts.setdefault(row.entity_type, {"states": {}, "total": 0, "unavailable": 0, "unreachable": []})
            if not await current_record(db, local, profile, row):
                entry["unavailable"] += 1
                continue
            entry["total"] += 1
            lc = profile.definition["entities"][row.entity_type].get("lifecycle")
            if lc:
                entry["states"][row.fields[lc["field"]]] += 1
        return {"entities": counts}


async def exchange_projection(db, scope):
    from db.models.wiki_workflow import WikiArtifact
    from db.models.wiki_platform import WikiRelation
    from memory.wiki.records import current_artifact, current_record, record_view
    from memory.wiki.service import dependencies_current
    from wiki_compiler.profiles import ProfileError
    profiles = list((await db.scalars(select(WikiProfile).where(*scope.predicates(WikiProfile)).order_by(WikiProfile.id).limit(101))).all())
    runs = list((await db.scalars(select(WikiWorkflowRun).where(*scope.predicates(WikiWorkflowRun)).order_by(WikiWorkflowRun.id).limit(501))).all())
    if len(profiles) > 100 or len(runs) > 500:
        raise WikiStateError("wiki_exchange_export_limit")
    result = {"profiles": [view(row) for row in profiles], "workflows": [{"runId": run.id, "workflowId": run.workflow_id, "status": run.status.lower(),
        "profileDigest": run.profile_hash, "stageIndex": run.stage_index,
        "stages": [{"id": key, "status": value.get("status", "pending")} for key, value in run.stages.items()],
        "foreignHistoryExecutable": False} for run in runs], "records": [], "artifacts": [], "relations": []}
    for profile in profiles:
        local = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id, project_id=profile.project_id)
        records = list((await db.scalars(select(WikiTypedRecord).where(WikiTypedRecord.profile_id == profile.id,
            *local.predicates(WikiTypedRecord)).limit(1001))).all())
        if len(records) > 1000:
            raise WikiStateError("wiki_exchange_export_limit")
        current_ids = set()
        for record in records:
            if await current_record(db, local, profile, record):
                current_ids.add(record.id)
                result["records"].append({**await record_view(db, local, profile, record), "profile_id": profile.id})
        relations = list((await db.scalars(select(WikiRelation).where(WikiRelation.profile_id == profile.id,
            *local.predicates(WikiRelation), WikiRelation.status == "ACTIVE").limit(2001))).all())
        artifacts = list((await db.scalars(select(WikiArtifact).where(WikiArtifact.profile_id == profile.id,
            *local.predicates(WikiArtifact)).limit(501))).all())
        if len(relations) > 2000 or len(artifacts) > 500:
            raise WikiStateError("wiki_exchange_export_limit")
        for relation in relations:
            if relation.from_id in current_ids and relation.to_id in current_ids and await dependencies_current(db, local, relation):
                result["relations"].append({"id": relation.id, "type": relation.relation_type, "from": relation.from_id,
                    "to": relation.to_id, "attributes": relation.attributes, "profile_id": profile.id})
        for artifact in artifacts:
            try:
                await current_artifact(db, local, profile, artifact)
            except (ProfileError, WikiStateError):
                continue
            result["artifacts"].append({"id": artifact.id, "revision": artifact.revision, "name": artifact.name,
                "type": artifact.artifact_type, "body": artifact.body, "media_type": artifact.media_type,
                "content_hash": artifact.content_hash, "records": artifact.record_manifest, "profile_id": profile.id})
    return result
