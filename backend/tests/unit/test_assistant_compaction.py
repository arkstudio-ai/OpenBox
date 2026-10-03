"""Real summary streams, source checkpoints, atomic replacement and replay."""
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from agent.compaction import create_compaction, process_compaction
from assistant.compaction import COMMITTED, CONSUMED, REQUESTED
from assistant.evidence import validate_message_sources
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_decisions import arguments, proposed, start
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import cursor_key  # noqa: F401


async def initial(*, manual_answer=True):
    prompt = 'ORIGINAL_BLUE: use blue for local summaries. Never publish without approval. ' + 'Background data. ' * 300
    ctx, lease, answer = await start(prompt)
    decision_id, _, _ = await proposed(ctx, await arguments(ctx, answer))
    await consume_context(ctx)
    await finish(ctx, lease, answer, 'Saved the human constraint.')
    original_id = answer.parent_id
    prompt = 'CURRENT_HUMAN: review this conversation; do not publish or delete anything.'
    if manual_answer:
        ctx, lease, answer = await next_turn(ctx, prompt)
    else:
        from agent.inbox import accept_inbox_item
        from agent.driver import reserve_run
        await accept_inbox_item(session_id=ctx.session_id, user_id=ctx.user_id, delivery='followup', prompt=prompt,
            origin='human', origin_ref={'actor_user_id': ctx.user_id})
        lease = await reserve_run(ctx.session_id, ctx.user_id)
        ctx = replace(ctx, run_id=lease.run_id, run_generation=lease.generation, message_id='', part_id='')
    return ctx, lease, answer, original_id, decision_id


async def compact(ctx):
    await create_compaction(ctx.session_id, auto=False, user_id=ctx.user_id, model_id='test/model', run_fence=ctx.run_fence)
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return await process_compaction(ctx.session_id, list(surface.messages), 'test/model', auto=False,
                                    user_id=ctx.user_id, run_fence=ctx.run_fence)


async def events(ctx, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == kind).order_by(AgentEvent.sequence))).all())


async def mutate_original(ctx, identity):
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == identity, Part.type == 'text'))
        part.data = {**part.data, 'text': 'Original evidence changed'}


async def test_multiple_compactions_expand_originals_and_preserve_current_input_and_newer_decision(monkeypatch):
    ctx, lease, answer, original_id, decision_id = await initial()
    calls = []

    async def provider(**kwargs):
        calls.append(json.dumps(kwargs['messages'], ensure_ascii=False))
        yield {'type': 'text_delta', 'text': f'SUMMARY_SENTINEL_{len(calls)}: use the original history and current constraints.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}

    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        await compact(ctx)
        first = (await events(ctx, COMMITTED))[0]
        assert answer.parent_id not in first.payload['source']['covered_message_ids']
        assert original_id in first.payload['original_message_ids']
        assert first.payload['source_spans'] and first.payload['context']['decision_refs']
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'SUMMARY_SENTINEL_1' in wire and 'CURRENT_HUMAN' in wire
        await finish(ctx, lease, answer, 'Reviewed the provided history only.')
        ctx, lease, answer = await next_turn(ctx, 'Correction: use green instead of blue. The publication prohibition remains.')
        green_id, _, _ = await proposed(ctx, await arguments(ctx, answer,
            summary='Use green instead of blue; publication still needs approval.', supersedes=[decision_id]))
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'Saved the newer human correction.')
        ctx, lease, answer = await next_turn(ctx, 'CURRENT_HUMAN_2: keep all original records and check the effective preference.')
        await compact(ctx)
        saved = await events(ctx, COMMITTED)
        assert len(saved) == len(calls) == 2
        assert 'SUMMARY_SENTINEL_1' not in calls[1] and 'ORIGINAL_BLUE' in calls[1]
        assert 'Use green instead of blue' in calls[1] and green_id in calls[1]
        assert first.message_id not in saved[1].payload['original_message_ids']
        assert original_id in saved[1].payload['original_message_ids']
        assert answer.parent_id not in saved[1].payload['source']['covered_message_ids']
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'SUMMARY_SENTINEL_2' in wire and 'SUMMARY_SENTINEL_1' not in wire and 'CURRENT_HUMAN_2' in wire
        assert 'Use green instead of blue' in wire
        await finish(ctx, lease, answer, 'The effective preference is green; nothing was published.')
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
        assert len(await events(ctx, REQUESTED)) == len(await events(ctx, CONSUMED)) == 2
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('change', ['original', 'summary'])
async def test_changed_source_or_summary_cannot_reenter_later_context_or_history(monkeypatch, change):
    ctx, lease, answer, original_id, _ = await initial()
    async def provider(**kwargs):
        yield {'type': 'text_delta', 'text': 'PRIVATE_SUMMARY_SENTINEL'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        await compact(ctx)
        saved = (await events(ctx, COMMITTED))[0]
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'A verified summary was available.')
        await mutate_original(ctx, original_id if change == 'original' else saved.message_id)
        async with get_db_session() as db:
            summary = await db.get(Message, saved.message_id)
            with pytest.raises(AssistantError):
                await validate_message_sources(db, summary, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx, 'Read only currently available evidence.')
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'PRIVATE_SUMMARY_SENTINEL' not in wire
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('failure', ['missing_response', 'changed_source', 'rollback'])
async def test_failed_provenance_or_transaction_never_applies_a_summary(monkeypatch, failure):
    ctx, lease, _, original_id, _ = await initial()
    async def provider(**kwargs):
        yield {'type': 'text_delta', 'text': 'DIAGNOSTIC_ONLY_SUMMARY'}
        if failure == 'changed_source':
            await mutate_original(ctx, original_id)
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    if failure == 'missing_response':
        async def omit(self, sequence):
            pass
        monkeypatch.setattr('assistant.compaction.CompactionProof.consumed', omit)
    if failure == 'rollback':
        from session import event_range
        original_append = event_range.append_agent_event_locked
        async def fail_replacement(*args, **kwargs):
            if kwargs['kind'] == 'surface.replacement':
                raise RuntimeError('Crash before compaction commit')
            return await original_append(*args, **kwargs)
        monkeypatch.setattr(event_range, 'append_agent_event_locked', fail_replacement)
    try:
        if failure == 'rollback':
            with pytest.raises(RuntimeError, match='Crash before'):
                await compact(ctx)
        else:
            assert await compact(ctx) == 'stop'
        assert not await events(ctx, COMMITTED) and not await events(ctx, 'surface.replacement')
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        assert original_id in {item.id for item in surface.messages}
    finally:
        await lease.release(session_status='idle')


async def test_chunk_provider_rechecks_sources_before_every_request(monkeypatch):
    ctx, lease, _, original_id, _ = await initial()
    calls = []
    async def provider(**kwargs):
        calls.append(kwargs['billing_kind'])
        yield {'type': 'text_delta', 'text': 'A partial chunk summary.'}
        await mutate_original(ctx, original_id)
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    monkeypatch.setattr('agent.compaction.get_model_context_limit', lambda _: 1000)
    monkeypatch.setattr('agent.compaction.summary_output_tokens', lambda *args: 100)
    try:
        assert await compact(ctx) == 'stop'
        assert calls == ['compaction_chunk']
        assert not await events(ctx, COMMITTED) and not await events(ctx, 'surface.replacement')
    finally:
        await lease.release(session_status='idle')


async def test_partial_window_records_omissions_instead_of_claiming_full_history(monkeypatch):
    ctx, lease, answer, original_id, _ = await initial()
    await consume_context(ctx)
    await finish(ctx, lease, answer, 'The earlier task remains constrained.')
    ctx, lease, _ = await next_turn(ctx, 'CURRENT: retain my constraints and read older details only when needed.')
    calls = []
    async def provider(**kwargs):
        calls.append(json.loads(kwargs['messages'][0]['content']))
        yield {'type': 'text_delta', 'text': 'Partial historical window; consult original history for omitted details.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    monkeypatch.setattr('assistant.projection.MAX_RECENT_MESSAGES', 1)
    try:
        await compact(ctx)
        saved = (await events(ctx, COMMITTED))[0]
        assert saved.payload['partial_coverage'] and saved.payload['omitted_original_messages'] > 0
        assert calls[0]['partial_coverage'] and calls[0]['omitted_original_messages'] > 0
        # The lasting human constraint is still supplied in full by the
        # separately protected decision projection outside this tiny window.
        spans = [entry for entry in saved.payload['source_spans'] if entry['message_id'] == original_id]
        assert spans and spans[0]['read_chars'] == spans[0]['total_chars']
        assert 'Never publish without approval' in json.dumps(calls[0])
        wire = json.dumps(await consume_context(ctx))
        assert 'Coverage metadata' in wire and 'partial_coverage' in wire and 'omitted_original_messages' in wire
    finally:
        await lease.release(session_status='idle')


async def test_real_loop_continues_after_compaction_with_verified_summary_and_current_input(monkeypatch):
    from agent import loop, processor
    ctx, lease, answer, _, _ = await initial(manual_answer=False)
    config = _loop_config()
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    calls = []
    async def summary(**kwargs):
        calls.append('summary')
        yield {'type': 'text_delta', 'text': 'LOOP_SUMMARY: a human preference and publication prohibition remain in force.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    async def provider(**kwargs):
        calls.append('ordinary')
        wire = json.dumps(kwargs['messages'])
        assert 'LOOP_SUMMARY' in wire and 'CURRENT_HUMAN' in wire and 'Never publish without approval' in wire
        assert kwargs['ctx'].sandbox is None
        yield {'type': 'text_delta', 'text': 'The active preference is blue; no actions were performed.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', summary)
    monkeypatch.setattr(processor, 'stream_llm', provider)
    try:
        from agent.inbox import claim_inbox_boundary
        await claim_inbox_boundary(lease, step=1, include_next_turn=True)
        await create_compaction(ctx.session_id, auto=True, user_id=ctx.user_id,
            model_id=config.model, run_fence=ctx.run_fence)
        await loop.run_loop(ctx.session_id, ctx.user_id, lease=lease)
        assert calls == ['summary', 'ordinary']
        assert len(await events(ctx, COMMITTED)) == 1
        manifests = await events(ctx, 'assistant.message.committed')
        async with get_db_session() as db:
            final = await db.get(Message, manifests[-1].message_id)
            assert final.finish == 'stop' and not final.error and final.id != answer.id
            await validate_message_sources(db, final, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
    finally:
        await lease.release(session_status='idle')
