"""Atomic reconciliation of a complete, freshly revalidated extraction snapshot."""
from sqlalchemy import select

from core.identifier import ascending
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiConceptBinding, WikiConceptExtraction, WikiRelation
from memory.wiki.organization import catalog_entry, identity_catalog, scoped_values
from memory.wiki.service import WikiStateError, dependencies_current, domain_for, now, target_identity
from wiki_compiler.hashing import canonical_hash
from wiki_compiler.organization import concept_slug, normalize_name, reconcile_concepts


async def apply_extractions(db, scope, run):
    extractions = []
    for extraction_id in run.result["extraction_ids"]:
        extraction = await db.get(WikiConceptExtraction, extraction_id)
        if extraction is None or extraction.status != "CURRENT" or not await dependencies_current(db, scope, extraction):
            raise WikiStateError("wiki_organization_inputs_changed")
        extractions.append(extraction)
    existing = await identity_catalog(db, scope)
    redirects = {item.id: item.merged_into for item in existing if item.status == "MERGED"}
    flattened = []
    for extraction in extractions:
        for proposal in extraction.concepts:
            item = {**proposal, "extraction_id": extraction.id}
            seen = set()
            while item.get("existing_id") in redirects:
                if item["existing_id"] in seen:
                    raise WikiStateError("wiki_concept_merge_cycle")
                seen.add(item["existing_id"])
                item["existing_id"] = redirects[item["existing_id"]]
            flattened.append(item)
    groups = reconcile_concepts(flattened, [catalog_entry(item) for item in existing if item.status != "MERGED"])
    from core.config import get_config
    if len(groups) > get_config().memory.wiki_organization_max_concepts:
        raise WikiStateError("wiki_organization_concept_limit")
    by_id = {item.id: item for item in existing}
    by_extraction = {item.id: item for item in extractions}
    domain = domain_for(scope, scope.project_id)
    # Names and aliases select identities; evidence always comes from current
    # source snapshots, including when a user has manually renamed a concept.
    current_ids = []
    for group in groups:
        if len(group["aliases"]) > 128:
            raise WikiStateError("wiki_organization_alias_limit")
        concept = by_id.get(group["id"])
        if concept is None:
            key = group["canonical_key"]
            if len(key) > 160:
                key = key[:96] + "-" + canonical_hash(key)[:32]
            identity = "wiki_concept_" + canonical_hash({"domain": domain, "key": key})[:40]
            slug = concept_slug(key)
            # An existing manually written topic can acquire a concept without
            # creating a second published page with the same exact title.
            pages = list((await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
                MemoryWikiPage.project_id == scope.project_id, MemoryWikiPage.title == group["title"],
                MemoryWikiPage.deleted_at.is_(None)))).all())
            if len(pages) == 1 and pages[0].id not in {item.page_id for item in existing}:
                slug = pages[0].slug
            concept = WikiConcept(id=identity, **scoped_values(scope), domain=domain, canonical_key=key,
                title=group["title"], aliases=group["aliases"], category=group["category"], description=group["description"],
                slug=slug, page_id="wiki_" + target_identity(scope, slug, scope.project_id)[:40],
                status="ACTIVE", user_edited=False, extra_metadata={})
            db.add(concept)
            by_id[identity] = concept
        else:
            if not concept.user_edited:
                concept.title, concept.category, concept.description = group["title"], group["category"], group["description"]
            aliases = sorted(set(concept.aliases + group["aliases"]) - {concept.title})
            if len(aliases) > 128:
                raise WikiStateError("wiki_organization_alias_limit")
            concept.aliases = aliases
            concept.revision += 1
            concept.status, concept.updated_at = "ACTIVE", now()
        group["id"] = concept.id
        current_ids.append(concept.id)
        members = {}
        for member in group["members"]:
            entry = members.setdefault(member["memory_id"], {"extraction_id": member["extraction_id"], "evidence": []})
            for quote in member["evidence"]:
                if quote not in entry["evidence"]:
                    entry["evidence"].append(quote)
        bindings = list((await db.scalars(select(WikiConceptBinding).where(WikiConceptBinding.concept_id == concept.id))).all())
        by_memory = {item.memory_id: item for item in bindings}
        for binding in bindings:
            if binding.memory_id not in members:
                binding.status, binding.evidence = "SUPERSEDED", []
        for memory_id, member in members.items():
            extraction = by_extraction[member["extraction_id"]]
            binding = by_memory.get(memory_id)
            if binding is None:
                binding = WikiConceptBinding(id=ascending("wiki_binding"), concept_id=concept.id, memory_id=memory_id)
                db.add(binding)
            binding.memory_revision = extraction.memory_revision
            binding.extraction_id, binding.source_manifest = extraction.id, extraction.source_manifest
            binding.evidence, binding.status = member["evidence"], "CURRENT"
    # Once a grounded merge assigns facts to a canonical article, a later
    # extraction must not silently shrink it merely by choosing another alias.
    # Only currently admitted, freshly validated facts can retain membership.
    by_memory = {item.memory_id: item for item in extractions}
    for concept in existing:
        if concept.status == "MERGED":
            continue
        merged_memory_ids = {memory_id for audit in concept.extra_metadata.get("consolidations", [])
                             for memory_id in audit.get("memory_ids", [])}
        retained_ids = merged_memory_ids & by_memory.keys()
        for memory_id in retained_ids:
            extraction = by_memory[memory_id]
            binding = await db.scalar(select(WikiConceptBinding).where(
                WikiConceptBinding.concept_id == concept.id, WikiConceptBinding.memory_id == memory_id))
            if binding is None:
                binding = WikiConceptBinding(id=ascending("wiki_binding"), concept_id=concept.id, memory_id=memory_id)
                db.add(binding)
            binding.memory_revision, binding.extraction_id = extraction.memory_revision, extraction.id
            binding.source_manifest, binding.status = extraction.source_manifest, "CURRENT"
            binding.evidence = [quote for proposal in extraction.concepts for quote in proposal["evidence"]]
        if retained_ids and concept.id not in current_ids:
            concept.status, concept.revision, concept.updated_at = "ACTIVE", concept.revision + 1, now()
            current_ids.append(concept.id)
    for concept in existing:
        if concept.id not in current_ids and concept.status != "MERGED":
            concept.status, concept.updated_at, concept.revision = "ORPHANED", now(), concept.revision + 1
    await db.flush()
    await _apply_relations(db, scope, groups, by_id, by_extraction)
    return current_ids


async def _apply_relations(db, scope, groups, concepts, extractions):
    domain = domain_for(scope, scope.project_id)
    lookup = {}
    for group in groups:
        concept = concepts[group["id"]]
        for label in [concept.title, *concept.aliases]:
            lookup.setdefault(normalize_name(label), set()).add(concept.id)
    proposed = {}
    for group in groups:
        for member in group["members"]:
            extraction = extractions[member["extraction_id"]]
            for relation in member["relations"]:
                matches = lookup.get(normalize_name(relation["target"]), set())
                target_id = next(iter(matches)) if len(matches) == 1 else "unresolved_" + canonical_hash(relation["target"])[:40]
                if target_id == group["id"]:
                    continue
                key = canonical_hash({"domain": domain, "from": group["id"], "to": target_id, "type": relation["type"]})
                value = proposed.setdefault(key, {"from_id": group["id"], "to_id": target_id,
                    "type": relation["type"], "target_label": relation["target"], "evidence": [], "sources": {}, "memories": {}})
                for quote in relation["evidence"]:
                    if quote not in value["evidence"]:
                        value["evidence"].append(quote)
                value["sources"].update({ref["id"]: ref for ref in extraction.source_manifest})
                value["memories"].update({ref["id"]: ref for ref in extraction.memory_manifest})
    previous = list((await db.scalars(select(WikiRelation).where(WikiRelation.domain == domain,
        WikiRelation.from_kind == "concept", WikiRelation.profile_id.is_(None)))).all())
    by_identity = {row.identity: row for row in previous}
    for row in previous:
        if row.identity not in proposed:
            row.status, row.evidence, row.updated_at = "STALE", [], now()
    for identity, value in proposed.items():
        row = by_identity.get(identity)
        sources, memories = list(value["sources"].values()), list(value["memories"].values())
        evidence_hash = canonical_hash({"sources": sources, "memories": memories, "evidence": value["evidence"]})
        if row is None:
            row = WikiRelation(id=ascending("wiki_relation"), **scoped_values(scope), domain=domain, identity=identity,
                relation_type=value["type"], from_kind="concept", from_id=value["from_id"],
                to_kind="concept", to_id=value["to_id"], acl_epoch=scope.acl_epoch)
            db.add(row)
        elif row.attributes.get("evidence_hash") != evidence_hash:
            row.status = "PROPOSED"
            row.revision += 1
        # A rejected proposal remains rejected until its actual evidence changes.
        row.status = row.status if row.status in {"ACTIVE", "REJECTED"} else "PROPOSED"
        row.attributes = {"target_label": value["target_label"], "evidence_hash": evidence_hash,
                          "resolved": not value["to_id"].startswith("unresolved_")}
        row.source_manifest, row.memory_manifest, row.evidence = sources, memories, value["evidence"]
        row.updated_at = now()
