from types import SimpleNamespace

import pytest

from assistant.task_recovery import NOT_RUNNING, RECOVERY_ONLY, recovery_state, permits_tool


def part(tool='tasks.followup', code=NOT_RUNNING, task_id='original'):
    return SimpleNamespace(tool=tool, status='error' if code else 'completed',
        input={'task_id': task_id}, metadata={'failure_code': code} if code else {})


def test_recovery_uses_receipts_not_user_wording_and_keeps_exact_task():
    assert recovery_state([]) is None
    assert recovery_state([part(tool='bash')]) is None
    state = recovery_state([part()])
    assert permits_tool(state, 'tasks.get', {'task_id': 'original'})
    assert permits_tool(state, 'tasks.resume', {'task_id': 'original'})
    assert not permits_tool(state, 'tasks.resume', {'task_id': 'another'})
    assert not permits_tool(state, 'tasks.submit')
    assert not permits_tool(state, 'tasks.link_existing')
    assert permits_tool(state, 'question')
    assert recovery_state([part(), part(tool='tasks.resume', code=None)]) is not None
    assert recovery_state([part(), part(code=None)]) is None
    assert recovery_state([part(), part(code=None, task_id='another')]) is not None


@pytest.mark.parametrize('code', ['ASSISTANT_STILL_STOPPING', 'ASSISTANT_EFFECT_UNRESOLVED', RECOVERY_ONLY])
def test_unsafe_or_unrelated_recovery_stops_without_new_work(code):
    state = recovery_state([part(), part(tool='tasks.resume', code=code)])
    assert state.stopped and not permits_tool(state, 'tasks.resume')
    assert recovery_state([part(), part(), part()]).stopped


@pytest.mark.parametrize('next_tool,args', [
    ('tasks.submit', {'instructions': 'duplicate'}),
    ('tasks.link_existing', {'session_id': 'replacement'}),
    ('tasks.resume', {'task_id': 'different'}),
])
async def test_same_provider_batch_cannot_escape_to_replacement_task(monkeypatch, next_tool, args):
    from agent import processor as p
    from agent.agent import AgentDef
    from tests.unit.test_processor_outcomes import Info, Ctx, NotAborted, fake_stream
    from tool.tool import ToolResult
    calls = []
    async def followup(args, ctx):
        calls.append('followup')
        return ToolResult(output='Task is stopped', metadata={'error': True, 'failure_code': NOT_RUNNING})
    async def replacement(args, ctx):
        calls.append('replacement')
        raise AssertionError('A held followup must not create or resume a replacement')
    async def noop(*a, **kw): pass
    class Hooks:
        async def wrap_execute(self, name, execute, args, ctx, **kwargs):
            return await execute(args, ctx)
    monkeypatch.setattr(p, 'save_part', noop)
    monkeypatch.setattr(p, 'update_message_info', noop)
    monkeypatch.setattr(p.bus, 'publish', lambda *a, **kw: None)
    monkeypatch.setattr(p, 'stream_llm', fake_stream(events=[
        {'type': 'tool_call', 'tool': 'tasks.followup', 'args': {'task_id': 'original'}, 'call_id': 'follow', 'invalid': False},
        {'type': 'tool_call', 'tool': next_tool, 'args': args, 'call_id': 'replace', 'invalid': False},
        {'type': 'finish', 'reason': 'tool_calls', 'usage': {}},
    ]))
    result = await p.process_step(session_id='s1', user_id='u1', session=None,
        agent_def=AgentDef(name='build', description='test'), system=[], llm_messages=[],
        tools={'tasks.followup': SimpleNamespace(execute=followup), next_tool: SimpleNamespace(execute=replacement)},
        model_id='test/model', ctx=Ctx(), hooks=Hooks(), assistant_info=Info(), sandbox=None,
        abort=NotAborted(), doom_loop_history=[])
    assert calls == ['followup']
    assert result.completed_tool_parts[-1].metadata['failure_code'] == RECOVERY_ONLY
