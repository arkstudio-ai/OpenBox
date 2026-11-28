"""Long-term memory as kernel plugins (docs/KERNEL_DSH_COMPARISON_20261128.md 9.1, stage one).

The entry point registers two units into ``agent.extensions.KernelExtensions``:

- ``MemoryCleanup`` carries the obligations that never stop with the feature
  (9.1 rule 7, docs/LONG_TERM_MEMORY_IMPLEMENTATION.md "遗忘语义"): the index
  worker delivers DELETE/REVOKE outbox events whatever the rollout flags,
  reconciles pending deletions, expires memories and purges expired debug
  snapshots; the document worker finishes removing deleted files' originals.
  It also closes the pooled provider clients at shutdown.
- ``MemoryPlugin`` is the feature: the prompt sections, the six memory tools
  and their pre-call check, extraction-job recovery, the extraction, Wiki and
  Wiki organization workers, the read-transport warm-up and the memory routes.

Leaving ``MemoryPlugin`` out keeps every cleanup obligation running. What this
stage does not move stays where it was, unchanged: the per-turn read scope and
history cut, the ephemeral recall injection, settlement-time registration,
proposal confirmation, ``strip_memory_text`` and the nested-read refusal in
Batch, and the ``memory_policy`` isolation checks.

Every method resolves the memory function when it is called, as the kernel's
and the entry point's call sites did, so the same function runs with the same
arguments in the same order (and a test that patches the function still
reaches it).
"""
from __future__ import annotations

import asyncio

from core.log import create_logger

log = create_logger("memory.plugin")
# These lines kept the loggers they had at their kernel call sites.
_loop_log = create_logger("agent.loop")
_recovery_log = create_logger("agent.recovery_service")

#: Agents whose system prompt carries the memory sections (the build agent and
#: the plan agent sharing its prompt).
PROMPT_AGENTS = frozenset({"build", "plan"})


# ---------------------------------------------------------------------------
# Prompt sections (agent.extensions.PromptSection)
# ---------------------------------------------------------------------------


class MemoryUseGuidance:
    """The shared evidence contract: policy, not a remembered fact.

    It also applies to later tool reads and to empty prefetch results, so it
    renders whenever the session may use memory at all.
    """

    name = "memory.use_guidance"
    order = 400  # after the account identity, before the project brief

    async def render(self, request) -> str | None:
        if request.memory_isolated or not request.user_id or request.agent_name not in PROMPT_AGENTS:
            return None
        from memory.presentation import MEMORY_USE_GUIDANCE
        return MEMORY_USE_GUIDANCE


class AutomaticSavingGuidance:
    """Saving happens after every turn; a model "remember" call adds nothing."""

    name = "memory.automatic_saving"
    order = 410

    async def render(self, request) -> str | None:
        if request.memory_isolated or not request.user_id or request.agent_name not in PROMPT_AGENTS:
            return None
        from memory.jobs import automatic_saving
        if not automatic_saving(request.user_id):
            return None
        from memory.presentation import AUTOMATIC_SAVING_GUIDANCE
        return AUTOMATIC_SAVING_GUIDANCE


class UserMemoryContext:
    """Creator memory, the last part: the most volatile piece stays after the cached prefix.

    Only the legacy path renders it (the loop sets ``include_user_memory`` when
    per-turn recall is off); recall itself rides on the newest user message
    and is not a prompt section.
    """

    name = "memory.user_context"
    order = 900  # after the project brief

    async def render(self, request) -> str | None:
        if not (request.include_user_memory and request.user_id and request.agent_name in PROMPT_AGENTS):
            return None
        try:
            from memory.context import assemble_user_context
            memory_args = {"user_id": request.user_id, "project_id": request.project_id or None}
            if request.workspace_id:
                memory_args["workspace_id"] = request.workspace_id
            assembled = await assemble_user_context(**memory_args)
            if assembled["context"]:
                return "<user_memory>\n" + assembled["context"] + "\n</user_memory>"
        except Exception as e:
            _loop_log.debug(f"Could not assemble user memory context: {e}")
        return None


# ---------------------------------------------------------------------------
# Tool pack (agent.extensions.ToolPack)
# ---------------------------------------------------------------------------


class MemoryTools:
    """The six memory tools, which of them a step may use, and the memory pre-call check."""

    name = "memory"
    capability = "memory"

    def tools(self):
        from memory import tools as memory_tools
        return {tool.id: tool for tool in (
            memory_tools.memory_prepare_tool, memory_tools.memory_search_tool,
            memory_tools.memory_read_sources_tool, memory_tools.memory_read_more_tool,
            memory_tools.current_task_state_tool, memory_tools.memory_forget_tool,
        )}

    def admit(self, tools, *, user_id):
        """Withhold the memory tools (the assistant's two memory reads included) the flags turn off."""
        from memory.tools import (MEMORY_ASSISTANT_TOOL_IDS, MEMORY_PREPARE_TOOL_IDS, MEMORY_TOOL_IDS,
                                  MEMORY_WRITE_TOOL_IDS)
        memory_tool_ids = MEMORY_TOOL_IDS | MEMORY_WRITE_TOOL_IDS | MEMORY_PREPARE_TOOL_IDS | MEMORY_ASSISTANT_TOOL_IDS
        if memory_tool_ids.intersection(tools):
            from core.config import get_config

            memory_config = getattr(get_config(), "memory", None)
            if not memory_config or not memory_config.enabled("retrieval_v2", user_id):
                tools = {tool_id: tool for tool_id, tool in tools.items() if tool_id not in memory_tool_ids}
            elif not (memory_config.enabled("semantic_recall", user_id)
                      and getattr(memory_config, "lazy_read_planning", False)):
                tools = {tool_id: tool for tool_id, tool in tools.items() if tool_id not in MEMORY_PREPARE_TOOL_IDS}
        return tools

    async def before_call(self, tool_id, args, ctx):
        """Memory capabilities run only in a session the persistent isolation policy admits."""
        from memory.session_policy import MEMORY_CAPABILITIES, require_context_memory
        if tool_id in MEMORY_CAPABILITIES and ctx is not None:
            from memory.policy import MemoryAccessDenied
            try:
                await require_context_memory(ctx)
            except MemoryAccessDenied:
                from tool.tool import ToolResult
                return ToolResult(title="Memory unavailable", output="Memory is unavailable for this Session.",
                                  metadata={"blocked": True, "error_code": "memory_scope_unavailable"})
        return None


# ---------------------------------------------------------------------------
# Recovery (agent.extensions.RecoveryHook)
# ---------------------------------------------------------------------------


class ExtractionJobRecovery:
    """Replay durable completion receipts into extraction jobs (``memory.jobs.recover_extraction_jobs``).

    Receipts survive schedule/commit gaps, so this runs on every recovery pass
    whether or not a Driver lease expired. Model calls stay in the extraction
    worker and never hold an Agent execution lease.
    """

    name = "memory.extraction_jobs"

    async def recover(self) -> int:
        try:
            from memory.jobs import recover_extraction_jobs

            return await recover_extraction_jobs(limit=100)
        except Exception as exc:
            _recovery_log.warning("Memory scheduling recovery deferred error_type=%s", type(exc).__name__)
            return 0


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


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
        for section in (MemoryUseGuidance(), AutomaticSavingGuidance(), UserMemoryContext()):
            registry.add_prompt_section(section)
        registry.add_tool_pack(MemoryTools())
        registry.add_recovery_hook(ExtractionJobRecovery())

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
