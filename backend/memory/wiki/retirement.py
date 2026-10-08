"""Retire automatic topic pages that no longer earn a page of their own.

Retiring hides a page from the library and from recall; it never deletes. The
page keeps its text, citations and history, and the next compile for the same
topic republishes it in place once the topic has enough support again.
"""
from sqlalchemy import select

from db.models.memory_wiki import MemoryWikiCandidate

RETIRED = "RETIRED"
NO_SOURCES = "retired_no_sources"
LOW_SUPPORT = "retired_low_support"


async def automatically_published(db, page) -> bool:
    """Only pages automation published may be retired by automation; anything
    a person approved stays until a person changes it."""
    candidate = await db.get(MemoryWikiCandidate, page.candidate_id) if page.candidate_id else None
    return bool(candidate and candidate.approved_by is None and candidate.reason_code == "automatic_grounded")


async def retire_page(db, page, config, reason: str) -> bool:
    if page.status == RETIRED or page.deleted_at:
        return False
    from memory.wiki.service import enqueue_page_outbox, now
    was_published = page.status == "PUBLISHED"
    page.status, page.invalidation_reason = RETIRED, reason
    page.revision, page.updated_at = page.revision + 1, now()
    if was_published:
        # Leave the search index; SQL already stops serving it as current.
        await enqueue_page_outbox(db, page, config, operation="REVOKE")
    return True


async def published_pages_for(db, scope, page_ids) -> set[str]:
    from db.models.memory_wiki import MemoryWikiPage
    if not page_ids:
        return set()
    return set((await db.scalars(select(MemoryWikiPage.id).where(MemoryWikiPage.id.in_(list(page_ids)),
        *scope.predicates(MemoryWikiPage), MemoryWikiPage.status == "PUBLISHED",
        MemoryWikiPage.deleted_at.is_(None)))).all())
