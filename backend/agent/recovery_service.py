"""Independent periodic convergence for durable Agent work.

This service is intentionally separate from Cron. Agent leases must recover
even when the scheduler is disabled, unhealthy, or still starting.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.log import create_logger

log = create_logger("agent.recovery_service")

RECOVERY_INTERVAL_SECONDS = 15.0
RECOVERY_STOP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class AgentRecoveryResult:
    expired_markers: int = 0
    completed_outboxes: int = 0
    rejoined_parents: int = 0
    repaired_sessions: int = 0
    resumed_prompts: int = 0
    resumed_unbound_tasks: int = 0
    resumed_subagent_activations: int = 0
    applied_subagent_interrupts: int = 0
    resumed_inbox_sessions: int = 0
    settled_inbox_claims: int = 0
    effect_scanned: int = 0
    effects_reconciled: int = 0
    effects_deferred: int = 0
    effects_manual_review: int = 0
    effects_failed_before_dispatch: int = 0
    effect_stale_skips: int = 0
    stale_skips: int = 0

    @property
    def changed(self) -> bool:
        return any((
            self.completed_outboxes,
            self.rejoined_parents,
            self.repaired_sessions,
            self.resumed_prompts,
            self.resumed_unbound_tasks,
            self.resumed_subagent_activations,
            self.applied_subagent_interrupts,
            self.resumed_inbox_sessions,
            self.settled_inbox_claims,
            self.effects_reconciled,
            self.effects_deferred,
            self.effects_manual_review,
            self.effects_failed_before_dispatch,
        ))


async def recover_agent_work_once() -> AgentRecoveryResult:
    """Run one ordered, idempotent Agent/Task recovery pass."""
    from agent.driver import recover_expired_driver_records
    from agent.recovery import (
        reconcile_completed_task_handoffs,
        repair_expired_sessions,
        resume_reserved_prompts,
        resume_unbound_task_children,
        resume_claimable_subagent_activations,
    )
    from agent.task_handoff import recover_task_handoff_outboxes
    from agent.subagent_runtime import (
        consume_interrupt_requests,
        has_subagent_state,
        recover_subagent_outboxes,
    )

    records = await recover_expired_driver_records()
    has_subagents = await has_subagent_state()

    # Interrupt generation fences win before any reserved takeover or accepted
    # activation scanner may reserve/wake a child.
    applied_interrupts = await consume_interrupt_requests() if has_subagents else 0

    # Child outboxes must converge before parent-tail repair. This stage also
    # scans bound idle children when ``records`` is empty.
    completed_outboxes = await recover_task_handoff_outboxes(records)
    if has_subagents:
        completed_outboxes += await recover_subagent_outboxes(records)
    rejoined_parents = set(
        await reconcile_completed_task_handoffs(
            records,
            include_subagents=has_subagents,
        )
    )

    replayable = [
        record
        for record in records
        if record.phase == "reserved" and record.trigger_message_id
    ]
    interrupted = [
        record
        for record in records
        if record not in replayable and record.session_id not in rejoined_parents
    ]
    repair_results = await repair_expired_sessions(interrupted)

    resumed, invalid = await resume_reserved_prompts(replayable)
    invalid_repairs = await repair_expired_sessions(invalid)
    all_repairs = [*repair_results, *invalid_repairs]

    # This scan closes descriptor->reserve crashes even when there were no
    # expired Driver rows in this pass.
    resumed_unbound = await resume_unbound_task_children()
    # Always scan, even when there were no expired Driver rows. This closes
    # accept->claim crashes and expired activation-claim ownership.
    resumed_subagents = (
        await resume_claimable_subagent_activations()
        if has_subagents else []
    )
    # Main-Session inbox convergence is independent of expired Drivers. It
    # closes accept->reserve crashes and the narrow terminal-release->settle
    # crash on every pass; reserve/claim remains exact and single-flight.
    from agent.inbox import (
        resume_claimable_inbox_sessions,
        settle_orphaned_claims,
    )

    settled_inbox = await settle_orphaned_claims()
    resumed_inbox = await resume_claimable_inbox_sessions()

    # External effects run last: their scanner is independently bounded and
    # query-only. It must not delay the higher-priority Driver/Inbox tail
    # repair order above, and it never replays a dispatch body.
    from agent.effect_ledger import recover_external_effects_once

    try:
        effect_recovery = await recover_external_effects_once()
    except Exception:
        # Keep Agent/Inbox convergence available during a transient effect
        # database/provider outage. The immutable ledger rows remain for the
        # next startup/periodic pass.
        log.exception("External-effect recovery pass failed")
        from agent.effect_ledger import EffectRecoveryResult

        effect_recovery = EffectRecoveryResult()

    return AgentRecoveryResult(
        expired_markers=len(records),
        completed_outboxes=completed_outboxes,
        rejoined_parents=len(rejoined_parents),
        repaired_sessions=sum(1 for result in all_repairs if not result.skipped),
        resumed_prompts=len(resumed),
        resumed_unbound_tasks=len(resumed_unbound),
        resumed_subagent_activations=len(resumed_subagents),
        applied_subagent_interrupts=applied_interrupts,
        resumed_inbox_sessions=len(resumed_inbox),
        settled_inbox_claims=settled_inbox,
        effect_scanned=effect_recovery.scanned,
        effects_reconciled=effect_recovery.reconciled,
        effects_deferred=effect_recovery.deferred,
        effects_manual_review=effect_recovery.manual_review,
        effects_failed_before_dispatch=effect_recovery.failed_before_dispatch,
        effect_stale_skips=effect_recovery.stale_skips,
        stale_skips=sum(1 for result in all_repairs if result.skipped),
    )


class AgentRecoveryService:
    """One immediate pass followed by a non-overlapping periodic sweep."""

    def __init__(
        self,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        stop_timeout_seconds: float = RECOVERY_STOP_TIMEOUT_SECONDS,
    ):
        if interval_seconds <= 0:
            raise ValueError("agent recovery interval must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("agent recovery stop timeout must be positive")
        self.interval_seconds = interval_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._pass_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> AgentRecoveryResult:
        async with self._pass_lock:
            return await recover_agent_work_once()

    @staticmethod
    def _log_result(result: AgentRecoveryResult) -> None:
        if result.changed:
            log.warning(
                "Agent recovery converged expired=%s outboxes=%s rejoined=%s "
                "repaired=%s resumed=%s unbound=%s subagents=%s interrupts=%s "
                "inbox_resumed=%s inbox_settled=%s effects=%s/%s/%s/%s "
                "effect_stale=%s stale=%s",
                result.expired_markers,
                result.completed_outboxes,
                result.rejoined_parents,
                result.repaired_sessions,
                result.resumed_prompts,
                result.resumed_unbound_tasks,
                result.resumed_subagent_activations,
                result.applied_subagent_interrupts,
                result.resumed_inbox_sessions,
                result.settled_inbox_claims,
                result.effects_reconciled,
                result.effects_deferred,
                result.effects_manual_review,
                result.effects_failed_before_dispatch,
                result.effect_stale_skips,
                result.stale_skips,
            )

    async def start(self) -> AgentRecoveryResult | None:
        if self.running:
            return None
        self._stop.clear()
        initial_result: AgentRecoveryResult | None = None
        try:
            initial_result = await self.run_once()
            self._log_result(initial_result)
        except Exception:
            # The durable markers remain intact. Keep the periodic worker alive
            # so a transient database/provider failure heals without a restart.
            log.exception("Initial Agent recovery pass failed")
        self._task = asyncio.create_task(
            self._run_loop(),
            name="agent-recovery-service",
        )
        return initial_result

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.interval_seconds,
                    )
                    continue
                except asyncio.TimeoutError:
                    pass
                try:
                    result = await self.run_once()
                    self._log_result(result)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Periodic Agent recovery pass failed")
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            from agent.recovery import quiesce_recovery_tasks

            await quiesce_recovery_tasks(timeout=self.stop_timeout_seconds)
            return
        self._stop.set()
        try:
            # If a pass is in progress, let its database transaction commit or
            # roll back cleanly instead of cancelling a pooled connection.
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.stop_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # A permanently stuck provider/database call must not hang a
            # deployment shutdown forever. Cancellation is the last resort and
            # only happens after the bounded graceful window above.
            log.error(
                "Agent recovery did not stop within %.1fs; cancelling it",
                self.stop_timeout_seconds,
            )
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        from agent.recovery import quiesce_recovery_tasks

        await quiesce_recovery_tasks(timeout=self.stop_timeout_seconds)


agent_recovery_service = AgentRecoveryService()
