"""The main assistant compacts through the ordinary summary path (V2).

PERSONAL_ASSISTANT_DESIGN_V2.md 4.3: there is no assistant-specific compaction
proof, coverage manifest or source re-check. A committed summary is used as
written; current watch-list and decision blocks are read from SQL each request.
"""
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from agent.compaction import create_compaction, process_compaction
from assistant.projection import project_main_messages
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.assistant_helpers import finish, next_turn
from tests.unit.test_assistant_decisions import arguments, block, proposed, start
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import cursor_key  # noqa: F401

REPLACED = "surface.replacement"
BLUE = "Use blue; publication requires human approval."


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


async def no_assistant_compaction_events(ctx):
    async with get_db_session() as db:
        kinds = set((await db.scalars(select(AgentEvent.kind).where(AgentEvent.session_id == ctx.session_id))).all())
    return not any(kind.startswith('assistant.compaction') for kind in kinds)


async def mutate_original(ctx, identity):
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == identity, Part.type == 'text'))
        part.data = {**part.data, 'text': 'Original evidence changed'}


async def test_repeated_compactions_keep_latest_summary_current_input_and_effective_decision(monkeypatch):
    ctx, lease, answer, original_id, decision_id = await initial()
    calls = []

    async def provider(**kwargs):
        calls.append(json.dumps(kwargs['messages'], ensure_ascii=False))
        yield {'type': 'text_delta', 'text': f'SUMMARY_SENTINEL_{len(calls)}: use the original history and current constraints.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}

    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        assert await compact(ctx) == 'stop'
        assert len(await events(ctx, REPLACED)) == 1 and await no_assistant_compaction_events(ctx)
        assert 'ORIGINAL_BLUE' in calls[0]
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'SUMMARY_SENTINEL_1' in wire and 'CURRENT_HUMAN' in wire and BLUE in wire
        assert 'ORIGINAL_BLUE' not in wire
        await finish(ctx, lease, answer, 'Reviewed the provided history only.')
        ctx, lease, answer = await next_turn(ctx, 'Correction: use green instead of blue. The publication prohibition remains.')
        green_id, _, _ = await proposed(ctx, await arguments(ctx, answer,
            summary='Use green instead of blue; publication still needs approval.', supersedes=[decision_id]))
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'Saved the newer human correction.')
        ctx, lease, answer = await next_turn(ctx, 'CURRENT_HUMAN_2: keep all original records and check the effective preference.')
        assert await compact(ctx) == 'stop'
        assert len(await events(ctx, REPLACED)) == len(calls) == 2
        # The ordinary path summarizes the earlier summary and the later turns.
        assert 'SUMMARY_SENTINEL_1' in calls[1] and 'Correction: use green instead of blue' in calls[1]
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        notes = block(projected, 'assistant:current-decisions')['decisions']
        assert [note['decision_id'] for note in notes] == [green_id] and notes[0]['supersedes'] == [decision_id]
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'SUMMARY_SENTINEL_2' in wire and 'SUMMARY_SENTINEL_1' not in wire and 'CURRENT_HUMAN_2' in wire
        assert 'Use green instead of blue; publication still needs approval.' in wire and BLUE not in wire
        assert original_id not in {message.id for message in surface.messages}
        await finish(ctx, lease, answer, 'The effective preference is green; nothing was published.')
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
        assert await no_assistant_compaction_events(ctx)
    finally:
        await lease.release(session_status='idle')


async def test_compacted_business_observation_stays_historical_while_the_watch_list_is_current(monkeypatch):
    from tests.unit.assistant_helpers import add_task
    from tests.unit.test_assistant_reads import call_tool, read_turn
    ctx, lease, answer, accepted, _ = await read_turn()
    calls = []
    async def provider(**kwargs):
        calls.append(json.dumps(kwargs['messages'], ensure_ascii=False))
        yield {'type': 'text_delta', 'text': 'HISTORICAL_INVENTORY_SUMMARY: the earlier SQL observation contained one task.'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        listed, ctx, _ = await call_tool(ctx, 'tasks.list', {})
        assert accepted['task_id'] in listed.output
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'The SQL list currently contains one task.')
        ctx, lease, answer = await next_turn(ctx, 'Keep the previous observation as history.')
        assert await compact(ctx) == 'stop'
        # An earlier read reaches the summarizer only as a stub, never as replayed current data.
        assert 'fresh_read_required' in calls[0] and accepted['task_id'] not in calls[0]
        created = await add_task(ctx)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        tasks = block(projected, 'assistant:current-tasks')['items']
        assert {item['task_id'] for item in tasks} == {accepted['task_id'], created['task_id']}
        wire = json.dumps(await consume_context(ctx))
        assert 'HISTORICAL_INVENTORY_SUMMARY' in wire
        await finish(ctx, lease, answer, 'The earlier one-task observation remains historical; current SQL has two tasks.')
    finally:
        await lease.release(session_status='idle')


async def test_committed_summary_is_not_revalidated_when_its_original_changes(monkeypatch):
    ctx, lease, answer, original_id, _ = await initial()
    async def provider(**kwargs):
        yield {'type': 'text_delta', 'text': 'COMMITTED_SUMMARY_SENTINEL'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        assert await compact(ctx) == 'stop'
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'A summary was available.')
        await mutate_original(ctx, original_id)
        # D1: the saved summary is history; changing its source does not rewrite or hide it.
        ctx, lease, _ = await next_turn(ctx, 'Continue from the summary.')
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'COMMITTED_SUMMARY_SENTINEL' in wire and 'Original evidence changed' not in wire
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('failure', ['incomplete', 'drift', 'rollback'])
async def test_failed_summary_or_transaction_never_applies_a_summary(monkeypatch, failure):
    ctx, lease, _, original_id, _ = await initial()
    async def provider(**kwargs):
        yield {'type': 'text_delta', 'text': 'DIAGNOSTIC_ONLY_SUMMARY'}
        yield {'type': 'finish', 'reason': 'length' if failure == 'incomplete' else 'stop', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    from session import event_range
    if failure == 'drift':
        async def drift(**kwargs):
            raise event_range.StableEventRangeDriftError('The frozen range changed')
        monkeypatch.setattr(event_range, 'finalize_compaction_replacement', drift)
    if failure == 'rollback':
        original_append = event_range.append_agent_event_locked
        async def fail_replacement(*args, **kwargs):
            if kwargs['kind'] == REPLACED:
                raise RuntimeError('Crash before compaction commit')
            return await original_append(*args, **kwargs)
        monkeypatch.setattr(event_range, 'append_agent_event_locked', fail_replacement)
    try:
        if failure == 'rollback':
            with pytest.raises(RuntimeError, match='Crash before'):
                await compact(ctx)
        else:
            assert await compact(ctx) == 'stop'
        assert not await events(ctx, REPLACED)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        assert original_id in {item.id for item in surface.messages}
    finally:
        await lease.release(session_status='idle')


async def test_failed_summary_attempt_is_never_replayed_as_history(monkeypatch):
    ctx, lease, _, _, _ = await initial()
    async def provider(**kwargs):
        yield {'type': 'text_delta', 'text': 'DIAGNOSTIC_ONLY_SUMMARY'}
        yield {'type': 'finish', 'reason': 'length', 'usage': {}}
    monkeypatch.setattr('agent.llm.stream_llm', provider)
    try:
        assert await compact(ctx) == 'stop'
        assert not await events(ctx, REPLACED)
        wire = json.dumps(await consume_context(ctx), ensure_ascii=False)
        assert 'CURRENT_HUMAN' in wire and 'ORIGINAL_BLUE' in wire
        assert 'DIAGNOSTIC_ONLY_SUMMARY' not in wire
    finally:
        await lease.release(session_status='idle')


async def test_real_loop_continues_after_compaction_with_summary_decision_and_current_input(monkeypatch):
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
        assert 'LOOP_SUMMARY' in wire and 'CURRENT_HUMAN' in wire and BLUE in wire
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
        assert len(await events(ctx, REPLACED)) == 1 and await no_assistant_compaction_events(ctx)
        async with get_db_session() as db:
            final = await db.scalar(select(Message).where(Message.session_id == ctx.session_id,
                Message.role == 'assistant', Message.summary.is_(False)).order_by(Message.id.desc()).limit(1))
            assert final.finish == 'stop' and not final.error and final.id != answer.id
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
    finally:
        await lease.release(session_status='idle')
