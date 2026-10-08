"""Typed record writes share field, lifecycle, source and target-version checks."""
import json

from sqlalchemy import select

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_platform import WikiRelation
from db.models.wiki_workflow import WikiArtifact, WikiTypedRecord
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki import service
from memory.wiki.organization import scoped_values
from memory.wiki.profiles import get_profile
from wiki_compiler.hashing import canonical_hash, text_hash
from wiki_compiler.profiles import ProfileError, identifier, require, validate_fields, validate_transition


def record_hash(row):
    return canonical_hash({"type": row.entity_type, "slug": row.slug, "title": row.title, "fields": row.fields,
                           "page_id": row.page_id, "page_revision": row.page_revision})


async def current_page(db, scope, page_id, revision=None):
    page = await db.scalar(select(MemoryWikiPage).where(MemoryWikiPage.id == page_id, *scope.predicates(MemoryWikiPage)))
    if (page is None or page.project_id != scope.project_id or revision is not None and page.revision != revision
            or not (await service._page_view(db, scope, page))["body_available"]):
        raise service.WikiStateError("wiki_record_page_unavailable")
    return page


async def get_record(db, scope, profile, record_id, *, revision=None):
    row = await db.scalar(select(WikiTypedRecord).where(WikiTypedRecord.id == record_id,
        WikiTypedRecord.profile_id == profile.id, *scope.predicates(WikiTypedRecord)).with_for_update())
    if row is None:
        raise service.WikiStateError("wiki_record_unavailable")
    if revision is not None and row.revision != revision:
        raise service.WikiStateError("wiki_record_changed")
    return row


async def current_record(db, scope, profile, row):
    try:
        if row.profile_id != profile.id or row.entity_type not in profile.definition["entities"] or record_hash(row) != row.content_hash:
            return False
        validate_fields(row.fields, profile.definition["entities"][row.entity_type]["fields"])
        lifecycle = profile.definition["entities"][row.entity_type].get("lifecycle")
        if lifecycle and lifecycle["field"] not in row.fields:
            return False
        await current_page(db, scope, row.page_id, row.page_revision)
        return True
    except (service.WikiStateError, ProfileError):
        return False


async def record_view(db, scope, profile, row):
    valid = await current_record(db, scope, profile, row)
    return {"id": row.id, "revision": row.revision, "entity_type": row.entity_type, "slug": row.slug,
        "title": row.title if valid else None, "fields": row.fields if valid else {}, "page_id": row.page_id,
        "page_revision": row.page_revision, "content_hash": row.content_hash, "available": valid}


async def artifact_refs(db, scope, profile, fields, definitions):
    for key, field in definitions.items():
        if key not in fields or field["type"] not in {"artifactRef", "artifactRef[]"}:
            continue
        for value in fields[key] if field["type"].endswith("[]") else [fields[key]]:
            parts = value.split("#")
            require(len(parts) == 2, key, "wiki_artifact_unavailable")
            row = await db.scalar(select(WikiArtifact).where(WikiArtifact.id == parts[0],
                WikiArtifact.profile_id == profile.id, *scope.predicates(WikiArtifact)))
            require(row and row.content_hash == parts[1] and text_hash(row.body) == parts[1], key, "wiki_artifact_unavailable")
            require(not field.get("artifactTypes") or row.artifact_type in field["artifactTypes"], key, "wiki_artifact_type_denied")
            await current_artifact(db, scope, profile, row)


async def save_record(db, scope, profile, payload, *, apply):
    kind = payload["entity_type"]
    require(kind in profile.definition["entities"], "entity_type", "wiki_record_type_unknown")
    identifier(payload["slug"], "slug")
    require(isinstance(payload["title"], str) and 1 <= len(payload["title"].strip()) <= 160, "title")
    page = await current_page(db, scope, payload["page_id"], payload["page_revision"])
    identity = "wiki_record_" + canonical_hash({"profile": profile.id, "type": kind, "slug": payload["slug"]})[:40]
    row = await db.get(WikiTypedRecord, identity)
    if (row.revision if row else 0) != payload["expected_revision"]:
        raise service.WikiStateError("wiki_record_changed")
    entity = profile.definition["entities"][kind]
    values = dict(payload["fields"])
    lifecycle = entity.get("lifecycle")
    if lifecycle:
        expected = row.fields.get(lifecycle["field"]) if row else lifecycle["initial"]
        values.setdefault(lifecycle["field"], expected)
        require(values[lifecycle["field"]] == expected, "lifecycle", "wiki_record_transition_required")
    values = validate_fields(values, entity["fields"])
    await artifact_refs(db, scope, profile, values, entity["fields"])
    if not apply:
        return {"id": identity, "page_id": page.id, "entity_type": kind, "title": payload["title"]}
    if row is None:
        row = WikiTypedRecord(id=identity, **scoped_values(scope), profile_id=profile.id, entity_type=kind, slug=payload["slug"])
        db.add(row)
    else:
        row.revision += 1
    row.title, row.fields = payload["title"].strip(), values
    row.page_id, row.page_revision, row.updated_at = page.id, page.revision, service.now()
    row.content_hash = record_hash(row)
    await db.flush()
    return await record_view(db, scope, profile, row)


async def relation_requirements(db, scope, profile, row, target):
    entity = profile.definition["entities"][row.entity_type]
    requirements = entity.get("lifecycle", {}).get("transitionRelationRequirements", {}).get(target, [])
    for requirement in requirements:
        role = requirement["role"]
        stmt = select(WikiRelation).where(*scope.predicates(WikiRelation), WikiRelation.profile_id == profile.id,
            WikiRelation.status == "ACTIVE", WikiRelation.relation_type == requirement["relationType"],
            (WikiRelation.from_id if role == "from" else WikiRelation.to_id) == row.id)
        rows = list((await db.scalars(stmt)).all())
        count = 0
        for relation in rows:
            other = await get_record(db, scope, profile, relation.to_id if role == "from" else relation.from_id)
            if not await current_record(db, scope, profile, other) or not await service.dependencies_current(db, scope, relation):
                continue
            if requirement.get("otherTypes") and other.entity_type not in requirement["otherTypes"]:
                continue
            lifecycle = profile.definition["entities"][other.entity_type].get("lifecycle", {})
            if requirement.get("otherStates") and other.fields.get(lifecycle.get("field")) not in requirement["otherStates"]:
                continue
            count += 1
        require(count >= requirement["minCount"], "relations", "wiki_record_relation_requirement")


async def transition(db, scope, profile, payload, *, apply):
    row = await get_record(db, scope, profile, payload["record_id"], revision=payload["expected_revision"])
    require(await current_record(db, scope, profile, row), "record", "wiki_record_unavailable")
    values = validate_transition(profile.definition["entities"][row.entity_type], row.fields, payload["to"])
    await relation_requirements(db, scope, profile, row, payload["to"])
    if apply:
        row.fields, row.revision, row.updated_at = values, row.revision + 1, service.now()
        row.content_hash = record_hash(row)
    return {"id": row.id, "revision": row.revision, "entity_type": row.entity_type, "to": payload["to"]}


async def save_relation(db, scope, profile, payload, *, apply):
    definition = profile.definition["relations"].get(payload["type"])
    require(definition is not None, "type", "wiki_relation_type_unknown")
    left = await get_record(db, scope, profile, payload["from_id"], revision=payload["from_revision"])
    right = await get_record(db, scope, profile, payload["to_id"], revision=payload["to_revision"])
    require(left.entity_type in definition["from"] and right.entity_type in definition["to"], "endpoints", "wiki_relation_endpoint_denied")
    require(await current_record(db, scope, profile, left) and await current_record(db, scope, profile, right), "endpoints", "wiki_record_unavailable")
    fields = validate_fields(payload.get("attributes", {}), definition.get("attributes", {}))
    require(all(key in fields for key in definition.get("requiredAttributes", [])), "attributes", "wiki_record_required_field")
    if definition["direction"] == "symmetric" and right.id < left.id:
        left, right = right, left
    identity = canonical_hash({"profile": profile.id, "type": payload["type"], "from": left.id, "to": right.id})
    row = await db.scalar(select(WikiRelation).where(WikiRelation.identity == identity))
    if (row.revision if row else 0) != payload["expected_revision"]:
        raise service.WikiStateError("wiki_relation_changed")
    if not apply:
        return {"id": row.id if row else "wiki_relation_" + identity[:40], "type": payload["type"]}
    pages = [await current_page(db, scope, item.page_id, item.page_revision) for item in (left, right)]
    if row is None:
        row = WikiRelation(id="wiki_relation_" + identity[:40], **scoped_values(scope), domain=service.domain_for(scope, scope.project_id),
            identity=identity, profile_id=profile.id, relation_type=payload["type"], from_kind="record", to_kind="record",
            from_id=left.id, to_id=right.id)
        db.add(row)
    else:
        row.revision += 1
    row.attributes, row.status, row.acl_epoch = fields, "ACTIVE", scope.acl_epoch
    row.source_manifest = list({item["id"]: item for page in pages for item in page.source_manifest}.values())
    row.memory_manifest = list({item["id"]: item for page in pages for item in page.memory_manifest}.values())
    row.evidence = [{"record_id": item.id, "revision": item.revision, "content_hash": item.content_hash} for item in (left, right)]
    row.updated_at = service.now()
    await db.flush()
    return {"id": row.id, "revision": row.revision, "type": row.relation_type}


async def current_artifact(db, scope, profile, row):
    require(profile is not None and row.profile_id == profile.id and row.artifact_type in profile.definition["artifacts"],
            "artifact", "wiki_artifact_unavailable")
    require(text_hash(row.body) == row.content_hash, "artifact", "wiki_artifact_unavailable")
    for reference in row.record_manifest:
        record = await get_record(db, scope, profile, reference["id"], revision=reference["revision"])
        require(record.content_hash == reference["content_hash"] and await current_record(db, scope, profile, record),
                "artifact", "wiki_artifact_unavailable")
    return row


async def save_artifact(db, scope, profile, payload, *, apply, run_id=None):
    definition = profile.definition["artifacts"].get(payload["type"])
    require(definition is not None and payload["media_type"] in definition["mediaTypes"], "artifact", "wiki_artifact_type_denied")
    require(isinstance(payload["body"], str) and 0 < len(payload["body"]) <= 64000, "artifact", "wiki_artifact_size_limit")
    require(isinstance(payload["name"], str) and 0 < len(payload["name"]) <= 160, "name")
    if payload["media_type"] == "application/json":
        try:
            json.loads(payload["body"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except ValueError as exc:
            raise ProfileError("wiki_artifact_invalid_json") from exc
    references = payload["records"]
    require(isinstance(references, list) and 1 <= len(references) <= 20, "records")
    manifest = []
    for reference in references:
        record = await get_record(db, scope, profile, reference["id"], revision=reference["revision"])
        require(await current_record(db, scope, profile, record), "records", "wiki_record_unavailable")
        manifest.append({"id": record.id, "revision": record.revision, "content_hash": record.content_hash})
    if not apply:
        return {"name": payload["name"], "content_hash": text_hash(payload["body"])}
    row = WikiArtifact(id=ascending("wiki_artifact"), **scoped_values(scope), profile_id=profile.id,
        artifact_type=payload["type"], name=payload["name"], media_type=payload["media_type"], body=payload["body"],
        content_hash=text_hash(payload["body"]), record_manifest=manifest, run_id=run_id)
    db.add(row)
    await db.flush()
    return {"id": row.id, "revision": row.revision, "content_hash": row.content_hash, "ref": row.id + "#" + row.content_hash}


async def mutate(*, user_id, workspace_id, profile_id, profile_revision, kind, payload):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        profile = await get_profile(db, scope, profile_id, lock=True)
        if profile is None:
            return None
        if profile.revision != profile_revision:
            raise service.WikiStateError("wiki_profile_changed")
        local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=profile.project_id)
        from memory.wiki.workflow_outputs import apply_output
        stage = {"writes": list(profile.definition["entities"]), "reads": list(profile.definition["entities"]),
                 "relationWrites": list(profile.definition["relations"]), "artifactWrites": list(profile.definition["artifacts"])}
        return await apply_output(db, local, profile, stage, {"kind": kind, **payload}, apply=True)
