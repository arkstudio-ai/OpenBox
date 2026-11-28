"""Long-term memory as kernel plugins (docs/KERNEL_DSH_COMPARISON_20261128.md 9.1, stage one).

The entry point registers two units into ``agent.extensions.KernelExtensions``:

- ``MemoryCleanup`` carries the obligations that never stop with the feature
  (9.1 rule 7, docs/LONG_TERM_MEMORY_IMPLEMENTATION.md "遗忘语义"): the index
  worker delivers DELETE/REVOKE outbox events whatever the rollout flags,
  reconciles pending deletions, expires memories and purges expired debug
  snapshots; the document worker finishes removing deleted files' originals.
  It also closes the pooled provider clients at shutdown.
- ``MemoryPlugin`` is the feature: the extraction, Wiki and Wiki organization
  workers, the read-transport warm-up and the memory routes.

Leaving ``MemoryPlugin`` out keeps every cleanup obligation running.

Every method resolves the memory function when it is called, as the entry
point's call sites did, so the same function runs with the same arguments in
the same order (and a test that patches the function still reaches it).
"""
from __future__ import annotations

import asyncio

from core.log import create_logger

log = create_logger("memory.plugin")


async def _cancel(task) -> None:
    if task is not None:
        task.cancel()
    await asyncio.gather(*([task] if task is not None else []), return_exceptions=True)


class MemoryCleanup:
    """Deletion, revocation and retention work that runs whether or not ``MemoryPlugin`` is registered."""

    name = "memory.cleanup"

    def __init__(self) -> None:
        self._index_worker = None
        self._document_worker = None

    def register(self, registry) -> None:
        return None

    async def start_background(self, app) -> None:
        from core.config import get_config
        from memory.documents.worker import MemoryDocumentWorker
        from memory.outbox import MemoryIndexWorker
        config = get_config()
        self._index_worker = MemoryIndexWorker(config.memory)
        self._document_worker = MemoryDocumentWorker(config.memory)
        self._index_worker.start()
        self._document_worker.start()

    def stop_background(self, app) -> dict:
        stops = {}
        if self._document_worker is not None:
            stops["documents"] = self._document_worker.stop()
        if self._index_worker is not None:
            stops["memory_index"] = self._index_worker.stop()
        return stops

    async def shutdown(self, app) -> None:
        # Workers and agent loops may still own pooled memory requests until
        # they have drained; the entry point calls this after both.
        from memory.providers.common import close_shared_clients
        await close_shared_clients()


class MemoryPlugin:
    """The long-term memory feature."""

    name = "memory"

    def __init__(self) -> None:
        self._extraction_worker = None
        self._wiki_worker = None
        self._wiki_organization_worker = None
        self._transport_warmup = None
        self._started = False

    def register(self, registry) -> None:
        return None

    def mount(self, app) -> None:
        """The memory, Wiki and document routes, in the order main.py included them."""
        from api.memory_search import router as memory_search_router
        from api.memory_debug import router as memory_debug_router
        from api.memory_backfill import router as memory_backfill_router
        from memory.wiki.api import router as memory_wiki_router
        from memory.wiki.organization_api import router as wiki_organization_router
        from memory.wiki.exchange_api import router as wiki_exchange_router
        from memory.wiki.workflow_api import router as wiki_workflow_router
        app.include_router(memory_search_router)
        from memory.experience_api import router as memory_experience_router
        app.include_router(memory_experience_router)
        app.include_router(memory_debug_router)
        app.include_router(memory_backfill_router)
        app.include_router(memory_wiki_router)
        from memory.documents.api import router as memory_documents_router
        app.include_router(memory_documents_router)
        app.include_router(wiki_organization_router)
        app.include_router(wiki_exchange_router)
        app.include_router(wiki_workflow_router)
        from api.memories import router as memories_router
        app.include_router(memories_router)

    async def start_background(self, app) -> None:
        from core.config import get_config
        from memory.extraction import MemoryExtractionWorker
        from memory.providers.configured_read import schedule_transport_warmup
        from memory.wiki.organization_worker import WikiOrganizationWorker
        from memory.wiki.worker import MemoryWikiWorker
        config = get_config()
        self._extraction_worker = MemoryExtractionWorker()
        self._wiki_worker = MemoryWikiWorker(config.memory)
        self._wiki_organization_worker = WikiOrganizationWorker(config.memory)
        await self._extraction_worker.start()
        self._wiki_worker.start()
        self._wiki_organization_worker.start()
        # One nonblocking, input-free handshake per process; no model call.
        self._transport_warmup = schedule_transport_warmup(config.memory)
        self._started = True

    def stop_background(self, app) -> dict:
        if not self._started:
            return {}
        return {
            "transport_warmup": _cancel(self._transport_warmup),
            "wiki": self._wiki_worker.stop(),
            "wiki_organization": self._wiki_organization_worker.stop(),
            "memory_extraction": self._extraction_worker.stop(),
        }
