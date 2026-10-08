"""Deterministic contracts, incremental changes and source/page closure."""
import hashlib
import json
from collections import defaultdict, deque

from wiki_compiler.contracts import CompileRequest, Dependency


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value) -> str:
    return text_hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def cache_key(request: CompileRequest) -> str:
    return canonical_hash({
        "contract": "wiki-cache-v1", "slug": request.slug, "title": request.title,
        "domain": request.domain, "policy": request.serialize()["policy"],
        "sources": [{"id": source.id, "kind": source.kind, "revision": source.revision,
                     "content_hash": source.content_hash, "acl_epoch": source.acl_epoch}
                    for source in sorted(request.sources, key=lambda source: source.id)],
    })


def source_changes(previous: tuple[Dependency, ...], current: tuple[Dependency, ...]) -> dict[str, tuple[str, ...]]:
    old = {item.id: item for item in previous}
    new = {item.id: item for item in current}
    return {
        "new": tuple(sorted(set(new) - set(old))),
        "deleted": tuple(sorted(set(old) - set(new))),
        "changed": tuple(sorted(key for key in old.keys() & new.keys() if old[key] != new[key])),
        "unchanged": tuple(sorted(key for key in old.keys() & new.keys() if old[key] == new[key])),
    }


def dependency_closure(changed_ids: set[str], pages: dict[str, tuple[str, ...]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Include every live co-contributor of every affected page to a fixed point."""
    reverse = defaultdict(set)
    for page, sources in pages.items():
        for source in sources:
            reverse[source].add(page)
    sources, affected = set(changed_ids), set()
    queue = deque(sorted(changed_ids))
    while queue:
        for page in sorted(reverse[queue.popleft()]):
            if page in affected:
                continue
            affected.add(page)
            for source in pages[page]:
                if source not in sources:
                    sources.add(source)
                    queue.append(source)
    return tuple(sorted(affected)), tuple(sorted(sources))
