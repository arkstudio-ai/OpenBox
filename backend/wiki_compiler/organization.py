"""Source-grounded concept contracts and deterministic identity reconciliation.

No database, credentials or provider IO. Inspired by upstream extraction-phase,
extraction-merge and resolver, with host-scoped stable identities and citations.
"""
from dataclasses import dataclass
import re
import unicodedata

from wiki_compiler.contracts import SourceSnapshot
from wiki_compiler.hashing import canonical_hash

ORGANIZATION_VERSION = "concept-extraction-v2"
RELATION_TYPES = frozenset({"related", "part_of", "supports", "contradicts"})


class OrganizationError(ValueError):
    pass


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def concept_slug(key: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "-", normalize_name(key)).strip("-")[:45]
    return (ascii_part or "concept") + "-" + canonical_hash(key)[:12]


@dataclass(frozen=True)
class OrganizationRequest:
    memory_id: str
    sources: tuple[SourceSnapshot, ...]
    existing: tuple[dict, ...]
    model: str
    prompt_version: str = ORGANIZATION_VERSION


def _text(value, *, maximum, field):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OrganizationError("invalid_concept_" + field)
    return value.strip()


def validate_evidence(value, sources):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise OrganizationError("concept_evidence_required")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"source_id", "quote"}:
            raise OrganizationError("invalid_concept_evidence")
        if not isinstance(entry["source_id"], str):
            raise OrganizationError("invalid_concept_evidence")
        source = sources.get(entry["source_id"])
        quote = _text(entry["quote"], maximum=2400, field="quote")
        if source is None or quote not in source.text:
            raise OrganizationError("concept_quote_not_in_source")
        record = {"source_id": source.id, "revision": source.revision,
                  "content_hash": source.content_hash, "quote": quote}
        if record not in result:
            result.append(record)
    return result


def validate_concepts(output, request: OrganizationRequest) -> list[dict]:
    if not isinstance(output, dict) or set(output) != {"concepts"}:
        raise OrganizationError("invalid_concept_output")
    raw = output["concepts"]
    if not isinstance(raw, list) or len(raw) > 12:
        raise OrganizationError("concept_count_exceeded")
    sources = {source.id: source for source in request.sources}
    known = {item["id"]: item for item in request.existing}
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"title", "aliases", "category", "description", "evidence", "relations", "existing_id"}:
            raise OrganizationError("invalid_concept_fields")
        title = _text(item.get("title"), maximum=160, field="title")
        category = _text(item.get("category"), maximum=80, field="category")
        description = _text(item.get("description"), maximum=800, field="description")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 12:
            raise OrganizationError("invalid_concept_aliases")
        aliases = list(dict.fromkeys(_text(alias, maximum=160, field="alias") for alias in aliases))
        existing_id = item.get("existing_id")
        if existing_id is not None and (not isinstance(existing_id, str) or existing_id not in known):
            raise OrganizationError("unknown_existing_concept")
        relations = item.get("relations", [])
        if not isinstance(relations, list) or len(relations) > 12:
            raise OrganizationError("invalid_concept_relations")
        checked = []
        for relation in relations:
            if not isinstance(relation, dict) or set(relation) != {"target", "type", "evidence"}:
                raise OrganizationError("invalid_concept_relation")
            if not isinstance(relation["type"], str) or relation["type"] not in RELATION_TYPES:
                raise OrganizationError("unknown_concept_relation_type")
            checked.append({"target": _text(relation["target"], maximum=160, field="relation_target"),
                            "type": relation["type"], "evidence": validate_evidence(relation["evidence"], sources)})
        result.append({"title": title, "canonical_key": normalize_name(title), "aliases": aliases,
                       "category": category, "description": description, "existing_id": existing_id,
                       "memory_id": request.memory_id, "evidence": validate_evidence(item.get("evidence"), sources),
                       "relations": checked})
    return result


def reconcile_concepts(extractions: list[dict], existing: list[dict]) -> list[dict]:
    """Aliases are scoped by the host. Ambiguous names require an explicit choice.

    A model may select a provided existing identity, but cannot manufacture one.
    New names are merged transitively within this run using an alias union, so
    source ordering does not split A(alias B) and B(alias C) into separate pages.
    """
    by_id = {item["id"]: item for item in existing}
    owners: dict[str, set[str]] = {}
    for item in existing:
        for name in [item["title"], *item.get("aliases", [])]:
            owners.setdefault(normalize_name(name), set()).add(item["id"])
    parent = list(range(len(extractions)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    named: dict[str, int] = {}
    for index, item in enumerate(extractions):
        for name in [item["title"], *item["aliases"]]:
            key = normalize_name(name)
            if key in named:
                parent[root(index)] = root(named[key])
            named[key] = index
    groups: dict[int, list[dict]] = {}
    for index, item in enumerate(extractions):
        groups.setdefault(root(index), []).append(item)
    result = []
    for group in groups.values():
        choices = {item["existing_id"] for item in group if item.get("existing_id")}
        names = list(dict.fromkeys(name for item in group for name in [item["title"], *item["aliases"]]))
        choices.update(owner for name in names for owner in owners.get(normalize_name(name), set()))
        if len(choices) > 1 or choices - by_id.keys():
            raise OrganizationError("ambiguous_concept_identity")
        previous = by_id[next(iter(choices))] if choices else None
        chosen = min(group, key=lambda item: (item["canonical_key"], item["title"]))
        title = previous["title"] if previous else chosen["title"]
        result.append({"id": previous["id"] if previous else None,
                       "canonical_key": previous["canonical_key"] if previous else normalize_name(title),
                       "title": title, "aliases": sorted(set(names + (previous.get("aliases", []) if previous else [])) - {title}),
                       "category": chosen["category"], "description": chosen["description"],
                       "members": group})
    # Several differently named extractions can deliberately select the same
    # known identity. Fold those groups, too, without losing their evidence.
    merged = {}
    for group in result:
        identity = group["id"] or group["canonical_key"]
        if identity in merged:
            merged[identity]["members"].extend(group["members"])
            merged[identity]["aliases"] = sorted(set(merged[identity]["aliases"] + group["aliases"]))
        else:
            merged[identity] = group
    return sorted(merged.values(), key=lambda item: item["canonical_key"])
