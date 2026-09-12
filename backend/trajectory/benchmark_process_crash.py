"""AC15: hard-kill a real leased tool executor, then recover in a new process."""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from trajectory.benchmark_concurrency import setup


def configure(url):
    logging.disable(logging.CRITICAL)
    os.environ['TRAJECTORY_RECORDING_ENABLED'] = 'true'
    os.environ.pop('TRAJECTORY_RECORD_USER_IDS', None)
    from core import config
    config._config = config.OpenBoxConfig(model='local/crash-fixture', provider={})
    import db.base as database
    engine = create_async_engine(url, connect_args={'timeout': 30} if url.startswith('sqlite') else {})
    database._engine = engine
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, database._session_factory


def effects(path):
    source = Path(path)
    return [json.loads(line) for line in source.read_text().splitlines()] if source.exists() else []


async def execute_tool(factory, counter_path, *, phase, connection=None):
    from pydantic import BaseModel
    from agent.hooks import ToolHooks
    from agent.trajectory import RequestCapture
    from question import runtime
    from session.session import create_user_message
    from tool.tool import ToolContext, ToolResult, define_tool
    from trajectory import bind
    from db.models.trajectory import SessionTrajectory, TrajectoryEvent
    from trajectory.recorder import event_dict
    from trajectory.types import canonical

    # The shorter lease is fixture configuration; recovery waits for its actual
    # wall-clock expiry. The database timestamp is never rewritten by the test.
    runtime.LEASE_SECONDS = 2
    message = await create_user_message('shared_root', f'explicit input {phase}', user_id='writer_owner')
    ticket = await runtime.start_run('shared_root', 'writer_owner')
    assert ticket is not None
    trace = await runtime.get_run_trace(ticket)
    assert trace is not None and trace.run_id == ticket.run_id
    token = runtime.current_run.set(ticket)
    context = ToolContext(session_id='shared_root', user_id='writer_owner', workspace_id='writer_workspace',
        project_id='writer_project', message_id=message.id, run_id=ticket.run_id, trace_context=trace)
    hooks = ToolHooks('shared_root', 'writer_owner', agent_rules=[
        {'permission':'*','pattern':'*','action':'allow'}])

    class Arguments(BaseModel):
        phase: str

    async def fixture_execute(arguments, tool_context):
        # This is the deterministic external-side-effect substitute. fsync
        # occurs before output is committed and before the kill notification.
        with Path(counter_path).open('a') as output:
            output.write(json.dumps({'phase':arguments.phase,'pid':os.getpid(),'run_id':ticket.run_id}) + '\n')
            output.flush()
            os.fsync(output.fileno())
        await tool_context.update_output(f'durable tool prefix {arguments.phase}')
        if connection is not None:
            async with factory() as db:
                trajectory = await db.scalar(select(SessionTrajectory))
                rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
                assert rows[-1].type == 'tool.output'
                assert rows[-1].data['output'] == 'durable tool prefix before_kill'
                snapshot = [event_dict(row) for row in rows]
                connection.send({'pid':os.getpid(),'ticket':asdict(ticket),
                    'call_id':tool_context.trace_context.call_id,'request_id':tool_context.trace_context.request_id,
                    'through_seq':str(trajectory.committed_seq),'event_count':len(rows),
                    'prefix_sha256':hashlib.sha256(canonical(snapshot)).hexdigest()})
            # No cancellation/exception/finally path is allowed to manufacture
            # a terminal result: the parent sends SIGKILL to this exact child.
            await asyncio.Event().wait()
        return ToolResult(title='Fixture complete', output=f'final result {arguments.phase}')

    tool = define_tool('crash_fixture_effect', description='Append one local fixture receipt',
        parameters=Arguments, execute=fixture_execute, sandbox_required=False)
    try:
        with bind(trace):
            capture = await RequestCapture.start(context, purpose='chat', model_id='local/crash-fixture',
                payload={'model':'local/crash-fixture','messages':[{'role':'user','content':f'explicit input {phase}'}]},
                capture_level='deterministic_fixture_adapter')
            await capture.chunk({'type':'fixture.delta','delta':f'request prefix {phase}'},
                blocks=[{'block_id':'text:0','type':'text','delta':f'request prefix {phase}'}])
            result = await hooks.wrap_execute(tool.id, tool.execute, {'phase':phase}, context,
                part_id=f'call_{phase}', tool_info=tool, arguments_raw=json.dumps({'phase':phase}))
            assert result.output == f'final result {phase}'
            await capture.finish('completed', reason='fixture_finished')
        await runtime.finish_run(ticket, completed=True)
        return {'ticket':asdict(ticket),'request_id':capture.context.request_id,'call_id':context.trace_context.call_id}
    finally:
        runtime.current_run.reset(token)


async def verify_recovery(factory, before, counter_path):
    from question import runtime
    from db.models.question import SessionExecution
    from db.models.trajectory import SessionTrajectory, TrajectoryEvent
    from trajectory.recorder import event_dict
    from trajectory.repository import state_at
    from trajectory.types import canonical

    async with factory() as db:
        execution = await db.get(SessionExecution, 'shared_root')
        assert execution.run_id == before['ticket']['run_id'] and execution.run_progress
        lease_until = runtime.utc(execution.lease_until)
    while lease_until >= runtime.now():
        await asyncio.sleep(min(.1, max(.001, (lease_until - runtime.now()).total_seconds())))
    await runtime.recover_expired_runs()
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        prefix = [event_dict(row) for row in rows if row.seq <= int(before['through_seq'])]
        assert hashlib.sha256(canonical(prefix)).hexdigest() == before['prefix_sha256']
        assert [row.seq for row in rows] == list(range(1, len(rows)+1))
        assert rows[-1].type == 'run.interrupted'
        assert rows[-1].data['status'] == 'unknown' and rows[-1].data['reason'] == 'lease_expired'
        assert not any(row.type in {'tool.finished','run.finished'} for row in rows)
        state = await state_at(db, trajectory)
        old_tool = state['records']['tool:'+before['call_id']]
        assert old_tool['status'] == 'unknown'
        assert old_tool['data']['output'] == 'durable tool prefix before_kill'
        assert state['records']['request:'+before['request_id']]['status'] == 'unknown'
        assert state['records']['run:'+before['ticket']['run_id']]['status'] == 'unknown'
        historical = await state_at(db, trajectory, before['through_seq'])
        assert historical['records']['tool:'+before['call_id']]['data']['output'] == 'durable tool prefix before_kill'
        execution = await db.get(SessionExecution, 'shared_root')
        assert execution.run_id is None and not execution.resume_pending
        recovered_head = trajectory.committed_seq
    assert [item['phase'] for item in effects(counter_path)] == ['before_kill']
    # Repeated startup/lease recovery must not create another tool invocation.
    await runtime.recover_expired_runs()
    async with factory() as db:
        assert await db.scalar(select(SessionTrajectory.committed_seq)) == recovered_head
    assert len(effects(counter_path)) == 1
    after = await execute_tool(factory, counter_path, phase='after_recovery')
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert [row.seq for row in rows] == list(range(1, len(rows)+1))
        state = await state_at(db, trajectory)
        assert state['records']['tool:'+before['call_id']]['status'] == 'unknown'
        assert state['records']['run:'+before['ticket']['run_id']]['status'] == 'unknown'
        assert state['records']['tool:'+after['call_id']]['status'] == 'completed'
        assert state['records']['run:'+after['ticket']['run_id']]['status'] == 'completed'
        assert after['ticket']['run_id'] != before['ticket']['run_id']
        assert after['ticket']['generation'] > before['ticket']['generation']
        assert len([row for row in rows if row.type=='input.accepted']) == 2
        assert len([row for row in rows if row.type=='tool.started']) == 2
        assert [item['phase'] for item in effects(counter_path)] == ['before_kill','after_recovery']
        return {'recovery_pid':os.getpid(),'recovered_through_seq':str(recovered_head),
            'final_through_seq':str(trajectory.committed_seq),'prefix_preserved':True,
            'old_tool_status':'unknown','old_request_status':'unknown','old_run_status':'unknown',
            'automatic_reexecution_count':0,'new_input_tool_status':'completed','new_input_run_status':'completed',
            'new_generation':after['ticket']['generation'],'side_effects':effects(counter_path)}


def worker(url, counter_path, connection, before=None):
    async def run():
        engine, factory = configure(url)
        try:
            if before is None:
                await execute_tool(factory, counter_path, phase='before_kill', connection=connection)
            else:
                connection.send(await verify_recovery(factory, before, counter_path))
        finally:
            await engine.dispose()
    try:
        asyncio.run(run())
    except BaseException as exc:
        import traceback
        connection.send({'error':type(exc).__name__, 'frames':[
            {'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]]})
        raise SystemExit(1)


def receive(connection, process):
    assert connection.poll(20), 'Fixture process failed to reach its checkpoint'
    value = connection.recv()
    assert 'error' not in value, value
    return value


def main(output):
    logging.disable(logging.CRITICAL)
    directory = Path(tempfile.mkdtemp(prefix='trajectory-hard-kill-'))
    url = os.getenv('TRAJECTORY_CRASH_DATABASE_URL')
    if url:
        parsed = make_url(url)
        assert parsed.host in {'localhost','127.0.0.1'} and parsed.database.startswith('openbox_trajectory_storage_'), 'Only a dedicated disposable local database is allowed'
    else:
        url = f'sqlite+aiosqlite:///{directory}/database.sqlite'
    asyncio.run(setup(url))
    counter_path = str(directory/'side-effects.jsonl')
    processes = multiprocessing.get_context('spawn')
    parent, child = processes.Pipe()
    victim = processes.Process(target=worker,args=(url,counter_path,child))
    victim.start()
    recovery = None
    try:
        before = receive(parent,victim)
        assert before['pid'] == victim.pid and len(effects(counter_path)) == 1
        victim.kill()  # Only the Process object created immediately above.
        victim.join(timeout=10)
        assert victim.exitcode == -signal.SIGKILL
        recovery_parent, recovery_child = processes.Pipe()
        recovery = processes.Process(target=worker,args=(url,counter_path,recovery_child,before))
        recovery.start()
        verified = receive(recovery_parent,recovery)
        recovery.join(timeout=10)
        assert recovery.exitcode == 0 and verified['recovery_pid'] != victim.pid
        result = {'database':make_url(url).get_backend_name(),'kill_signal':'SIGKILL','killed_child_pid':victim.pid,
            'exitcode':victim.exitcode,'lease_seconds':2,'lease_expiry':'actual_clock_no_database_rewrite',
            'before_kill':before,**verified,
            'real_components':['create_user_message','start_run','RequestCapture','ToolHooks','define_tool',
                'tool.output transaction commit','recover_expired_runs','fixed-watermark replay','finish_run'],
            'substitutes':['Local fsynced file replaces external tool side effect','Deterministic RequestCapture adapter without model dispatch'],
            'not_covered':['Remote database failover','Machine power loss','Cloud sandbox/network side effect']}
        Path(output).write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
    finally:
        for process in (victim,recovery):
            if process is not None and process.is_alive():
                process.kill(); process.join(timeout=10)
        parent.close(); child.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',required=True)
    main(parser.parse_args().output)
