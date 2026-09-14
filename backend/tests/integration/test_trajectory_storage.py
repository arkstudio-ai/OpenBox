import asyncio, hashlib, io, json, os, zipfile
import pytest, httpx
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from db.base import Base
import db.base as database
from db.models.user import User
from db.models.workspace import Workspace
from db.models.project import Project
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent, TrajectoryPayload
from trajectory import TraceContext, append_events_in_tx, ensure_trajectory_in_tx, record, record_stream, flush
from trajectory.payload import set_storage
from trajectory.projector import replay, statistics
from trajectory.repository import create_checkpoint_in_tx, read_events, state_at
from trajectory.types import IdempotencyConflict, OwnershipError, now

class MemoryBlob:
    def __init__(self): self.objects = {}
    async def upload(self, key, data, content_type=None): self.objects[key] = data
    async def download(self, key): return self.objects[key]
    async def delete(self, key): self.objects.pop(key, None)
    async def exists(self, key): return key in self.objects
    async def list_keys(self, prefix): return [key for key in self.objects if key.startswith(prefix)]

@pytest.fixture
async def tracedb(tmp_path, monkeypatch):
    url = os.getenv("TRAJECTORY_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'trajectory.sqlite'}"
    if not url.startswith("sqlite"):
        from sqlalchemy.engine import make_url
        assert make_url(url).database.startswith("openbox_trajectory_storage_"), "Only the dedicated disposable test database is allowed"
    engine = create_async_engine(url, connect_args={"timeout": 30} if url.startswith("sqlite") else {})
    import db.models
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    for key in ("TRAJECTORY_RECORDING_ENABLED", "TRAJECTORY_ADMIN_ENABLED"): monkeypatch.setenv(key, "true")
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_INTERVAL", "1000")
    for key in ("TRAJECTORY_RECORD_USER_IDS", "TRAJECTORY_ADMIN_USER_IDS"): monkeypatch.delenv(key, raising=False)
    blob = MemoryBlob(); set_storage(blob); timestamp = now()
    async with factory.begin() as db:
        for owner, role in (("admin", "admin"), ("a", "user"), ("b", "user"), ("workspace_admin", "user")):
            db.add(User(id=owner, username=owner, role=role, is_active=True, is_deleted=False, created_at=timestamp, updated_at=timestamp))
            await db.flush()
            db.add(Workspace(id=f"ws_{owner}", name=f"Workspace {owner}", owner_user_id=owner, created_at=timestamp, updated_at=timestamp))
            await db.flush()
            db.add(Project(id=f"prj_{owner}", user_id=owner, workspace_id=f"ws_{owner}", name=f"Project {owner}", created_at=timestamp, updated_at=timestamp))
            await db.flush()
            for suffix in ("1", "2"):
                db.add(Session(id=f"session_{owner}_{suffix}", user_id=owner, workspace_id=f"ws_{owner}", project_id=f"prj_{owner}", title=f"Session {owner} {suffix}", status="idle", created_at=timestamp, updated_at=timestamp))
    yield factory, blob
    set_storage(None); await engine.dispose()

async def append(factory, ctx, events):
    async with factory.begin() as db: return await append_events_in_tx(db, ctx, events)

async def test_transactions_rollback_retry_notifications(tracedb):
    factory, _ = tracedb
    from bus.bus import subscribe
    notifications = []; stop = subscribe("trajectory.available", lambda event: notifications.append(event["data"]))
    ctx = TraceContext("a", "session_a_1")
    try:
        async with factory() as db:
            await ensure_trajectory_in_tx(db, ctx, baseline={"legacy_session": True, "history": [{"role": "user", "content": "old"}]})
            result = await append_events_in_tx(db, ctx, [{"type": "input.accepted", "event_id": "input_a", "message_id": "msg_a", "data": {"text": "new"}}])
            assert result.through_seq == "3"; assert notifications == []; await db.rollback()
        async with factory() as db: assert await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == 0
        assert notifications == []
        event = {"type": "input.accepted", "event_id": "input_a", "message_id": "msg_a", "data": {"text": "new"}}
        assert (await append(factory, ctx, [event])).through_seq == "2"
        assert notifications[-1]["committed_seq"] == "2"
        assert (await append(factory, ctx, [event])).through_seq == "2"
        with pytest.raises(IdempotencyConflict): await append(factory, ctx, [{**event, "data": {"text": "different"}}])
        async with factory() as db:
            trajectory = await db.scalar(select(SessionTrajectory)); assert trajectory.committed_seq == 2; assert trajectory.next_seq == 3
    finally: stop()

async def test_concurrent_append_contiguous_sqlite(tracedb):
    factory, _ = tracedb; ctx = TraceContext("a", "session_a_1")
    async def worker(worker_id):
        for chunk in range(8): await append(factory, ctx, [{"type": "input.accepted", "event_id": f"input_{worker_id}_{chunk}", "data": {"text": f"{worker_id}:{chunk}"}}])
    await asyncio.gather(*(worker(i) for i in range(4)))
    async with factory() as db:
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert [row.seq for row in rows] == list(range(1, 34))
        trajectory = await db.scalar(select(SessionTrajectory)); assert trajectory.committed_seq == trajectory.projected_seq == 33

async def test_historical_checkpoint_and_stream(tracedb):
    factory, _ = tracedb; ctx = TraceContext("a", "session_a_1", request_id="request_a", run_id="run_a")
    await record("request.started", {"model": "fixture"}, context=ctx)
    receipts = [record_stream(ctx, {"type": "request.delta", "event_id": f"delta_{i}", "data": {"chunk_index": i, "block_id": "text:0", "block_type": "text", "delta": str(i)}}) for i in range(5)]
    await asyncio.gather(*receipts); assert await flush(ctx) == "7"
    async with factory.begin() as db:
        trajectory = await db.scalar(select(SessionTrajectory)); await create_checkpoint_in_tx(db, trajectory)
    await record("request.finished", {"status": "completed", "usage": {"input_tokens": 12, "output_tokens": 5}}, context=ctx)
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory)); historic = await state_at(db, trajectory, "4")
        assert historic["records"]["assistant:request_a"]["blocks"][0]["text"] == "01"
        assert historic["records"]["request:request_a"]["status"] == "streaming"
        final = await state_at(db, trajectory); events = (await read_events(db, trajectory, until_seq=trajectory.committed_seq))["events"]
        assert final == replay(events); assert statistics(final)["output_tokens"] == 5
        trajectory.projected_seq -= 1; assert await state_at(db, trajectory, trajectory.committed_seq) == final

async def test_child_ownership_deleted_session_unknown_result(tracedb):
    factory, _ = tracedb; stamp = now()
    async with factory.begin() as db: db.add(Session(id="child", user_id="a", workspace_id="ws_a", project_id="prj_a", parent_id="session_a_1", created_at=stamp, updated_at=stamp))
    ctx = TraceContext("a", "session_a_1", source_session_id="child", run_id="run_a", call_id="call_a", agent_id="child_agent")
    await append(factory, ctx, [{"type": "tool.started", "data": {"name": "fixture"}}, {"type": "run.interrupted", "data": {"status": "unknown", "reason": "lease_expired"}}])
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory)); state = await state_at(db, trajectory)
        assert state["records"]["tool:call_a"]["status"] == "unknown"; assert state["records"]["tool:call_a"]["source_session_id"] == "child"
    for source in ("session_b_1", "session_a_2"):
        with pytest.raises(OwnershipError): await append(factory, ctx.derive(source_session_id=source), [{"type": "tool.started", "data": {}}])
    async with factory.begin() as db:
        row = await db.get(Session, "session_a_1"); row.is_deleted = True
    with pytest.raises(OwnershipError): await append(factory, ctx, [{"type": "operation.late_result", "data": {"result": "late"}}])

@pytest.fixture
async def client(tracedb):
    from auth.middleware import get_current_user
    from api.admin_trajectories import router
    app = FastAPI(); app.include_router(router)
    async def viewer(request: Request): return {"user_id": request.headers.get("X-Test-Viewer", "admin"), "role": "admin"}
    app.dependency_overrides[get_current_user] = viewer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client: yield client

async def test_admin_read_paths_live_role_and_target(client, tracedb):
    factory, _ = tracedb
    for owner in ("a", "b"): await append(factory, TraceContext(owner, f"session_{owner}_1"), [{"type": "input.accepted", "message_id": f"msg_{owner}", "data": {"text": f"hello {owner}"}}])
    listing = await client.get("/api/admin/trajectories/sessions", headers={"X-Workspace-Id": "ws_a"})
    assert listing.status_code == 200; assert {row["user_id"] for row in listing.json()["items"]} == {"a", "b"}
    page = await client.get("/api/admin/trajectories/sessions?limit=1")
    next_page = await client.get("/api/admin/trajectories/sessions", params={"limit": 1, "cursor": page.json()["next_cursor"]})
    assert page.json()["items"][0]["session_id"] != next_page.json()["items"][0]["session_id"]
    paths = ("", "/events", "/records", "/records/user:msg_a", "/checkpoint", "/search?q=hello", "/payloads/missing", "/exports/missing", "/exports/missing/download")
    for viewer in ("a", "b", "workspace_admin"):
        for path in paths:
            response = await client.get(f"/api/admin/trajectories/sessions/session_a_1{path}", headers={"X-Test-Viewer": viewer})
            assert response.status_code == 403, (viewer, path, response.text)
        assert (await client.get("/api/admin/trajectories/sessions", headers={"X-Test-Viewer": viewer})).status_code == 403
        assert (await client.post("/api/admin/trajectories/sessions/session_a_1/export", json={}, headers={"X-Test-Viewer": viewer})).status_code == 403
    assert (await client.get("/api/admin/trajectories/sessions/session_a_1/search?q=hello&through_seq=2")).json()["items"][0]["record_id"] == "user:msg_a"
    async with factory() as db: before = await db.scalar(select(func.count()).select_from(SessionTrajectory))
    assert (await client.get("/api/admin/trajectories/sessions/session_a_2")).json()["recording_status"] == "not_recorded"
    async with factory() as db: assert await db.scalar(select(func.count()).select_from(SessionTrajectory)) == before
    async with factory.begin() as db:
        admin = await db.get(User, "admin"); admin.role = "user"
    assert (await client.get("/api/admin/trajectories/sessions")).status_code == 403

async def test_payload_history_export_deletion(client, tracedb, monkeypatch):
    factory, blob = tracedb; monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "200")
    ctx = TraceContext("a", "session_a_1", request_id="request_a")
    result = await append(factory, ctx, [{"type": "request.prepared", "data": {"input": {"text": "L" * 500, "api_key": "secret-value-to-remove"}}}])
    payload_id = result.events[-1]["data"]["$payload"]["payload_id"]
    assert not any(b"secret-value-to-remove" in content for content in blob.objects.values())
    url = f"/api/admin/trajectories/sessions/session_a_1/payloads/{payload_id}"
    assert (await client.get(url + "?through_seq=1")).status_code == 404
    assert (await client.get(url + "?through_seq=2")).status_code == 200
    await append(factory, TraceContext("b", "session_b_1"), [])
    assert (await client.get(f"/api/admin/trajectories/sessions/session_b_1/payloads/{payload_id}")).status_code == 404
    response = await client.post("/api/admin/trajectories/sessions/session_a_1/export", json={"through_seq": "2"})
    assert response.status_code == 202; export_id = response.json()["export_id"]
    result = await client.get(f"/api/admin/trajectories/sessions/session_a_1/exports/{export_id}")
    assert result.json()["status"] == "completed", result.text
    download = await client.get(result.json()["download_url"]); assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest = json.loads(archive.read("manifest.json")); assert manifest["through_seq"] == "2"; assert manifest["complete"]
        for item in manifest["files"]: assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]
    async with factory.begin() as db:
        row = await db.get(TrajectoryPayload, payload_id); row.availability, row.deleted_at = "deleted", now()
    assert (await client.get(url)).status_code == 410
    assert (await client.get(result.json()["download_url"])).status_code == 410
    history = await client.get("/api/admin/trajectories/sessions/session_a_1/records/request:request_a?through_seq=2")
    assert "L" * 500 not in history.text

async def test_durable_payload_staging_archives_without_transaction_lock(tracedb, monkeypatch):
    factory, blob = tracedb
    from trajectory.payload import drain_payloads, read_payload
    monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "100")
    result = await append(factory, TraceContext("a", "session_a_1"), [{"type": "input.accepted", "data": {"text": "retained " * 1000}}])
    payload_id = result.events[-1]["data"]["$payload"]["payload_id"]
    assert blob.objects == {}  # No network/disk adapter touched inside write transaction.
    async with factory() as db:
        row = await db.get(TrajectoryPayload, payload_id)
        assert row.storage_status == "pending" and row.content
        _, before = await read_payload(db, result.trajectory_id, payload_id, through_seq=2)
    async def fail(*args, **kwargs): raise OSError("simulated blob outage")
    original = blob.upload; blob.upload = fail
    assert (await drain_payloads())["failed"] > 0
    async with factory() as db:
        _, still_readable = await read_payload(db, result.trajectory_id, payload_id, through_seq=2)
        assert still_readable == before
    blob.upload = original
    assert (await drain_payloads())["archived"] > 0
    async with factory() as db:
        row = await db.get(TrajectoryPayload, payload_id)
        assert row.storage_status == "stored" and row.content is None
        _, after = await read_payload(db, result.trajectory_id, payload_id, through_seq=2)
        assert after == before

async def test_stream_barrier_deletion_tombstone_and_gc(tracedb):
    factory, blob = tracedb
    from trajectory import delete_trajectory_in_tx
    from trajectory.lifecycle import purge_deleted_content
    ctx = TraceContext("a", "session_a_1", request_id="req")
    await record("request.started", {}, context=ctx)
    # Do not await the chunk task before the structural event: the recorder's
    # flush barrier must still commit that scheduled chunk first.
    task = record_stream(ctx, {"type": "request.delta", "data": {"chunk_index": 0, "delta": "prefix"}})
    result = await record("request.finished", {"status": "completed"}, context=ctx)
    assert int((await task)["seq"]) < int(result["seq"])
    async with factory.begin() as db: await delete_trajectory_in_tx(db, ctx.session_id, ctx.user_id)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == 0
        trajectory = await db.scalar(select(SessionTrajectory)); assert trajectory.deleted_at is not None
    with pytest.raises(OwnershipError): await append(factory, ctx, [{"type": "input.accepted", "data": {"text": "late"}}])
    await purge_deleted_content()

async def test_read_committed_cache_race_does_not_leak_future(tracedb):
    factory, _ = tracedb
    from trajectory.repository import list_records
    ctx = TraceContext('a', 'session_a_1', request_id='req')
    await append(factory, ctx, [{'type':'request.started','data':{}}, {'type':'request.delta','data':{'chunk_index':0,'delta':'prefix'}}])
    async with factory() as reader:
        stale_header = await reader.scalar(select(SessionTrajectory))
        assert stale_header.committed_seq == 3
        await append(factory, ctx, [{'type':'request.delta','data':{'chunk_index':1,'delta':' future'}}, {'type':'request.finished','data':{'status':'completed','usage':{'input_tokens':1,'output_tokens':2}}}])
        state = await state_at(reader, stale_header, '3')
        assert state['records']['assistant:req']['blocks'][0]['text'] == 'prefix'
        page = await list_records(reader, stale_header, through_seq='3')
        assert all('future' not in (row.get('preview') or '') for row in page['items'])
        assert statistics(state)['output_tokens'] is None

async def test_pause_resume_baseline_and_export_gaps(client, tracedb, monkeypatch):
    factory, _ = tracedb
    from trajectory import mark_capture_paused_in_tx
    ctx = TraceContext('a', 'session_a_1')
    async with factory.begin() as db:
        await ensure_trajectory_in_tx(db, ctx, baseline={'legacy_session':False,'history':[]})
    monkeypatch.setenv('TRAJECTORY_RECORDING_ENABLED','false')
    await record('input.accepted', {'text':'MUST_NOT_CAPTURE'}, context=ctx)
    await record('input.accepted', {'text':'ALSO_NOT_CAPTURE'}, context=ctx)
    async with factory() as db:
        rows = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert len([row for row in rows if row.type=='recording.gap'])==1
        assert 'MUST_NOT_CAPTURE' not in json.dumps([row.data for row in rows])
    monkeypatch.setenv('TRAJECTORY_RECORDING_ENABLED','true')
    async with factory.begin() as db:
        await ensure_trajectory_in_tx(db, ctx)
        await ensure_trajectory_in_tx(db, ctx, baseline={'legacy_session':True,'history':[{'role':'user','text':'actual later context'}]})
        await append_events_in_tx(db, ctx, [{'type':'input.accepted','data':{'text':'new recorded'}}])
    async with factory() as db:
        baselines = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.type=='baseline.captured'))).all()
        assert len(baselines)==2
    export = await client.post('/api/admin/trajectories/sessions/session_a_1/export',json={})
    info = await client.get('/api/admin/trajectories/sessions/session_a_1/exports/'+export.json()['export_id'])
    download = await client.get(info.json()['download_url'])
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert not manifest['complete'] and len(manifest['gaps'])==2

async def test_failed_stream_retires_for_fresh_run_and_records_gap(tracedb, monkeypatch):
    factory, _ = tracedb
    import trajectory.recorder as recorder
    from trajectory import RecordingError
    old = TraceContext('a','session_a_1',run_id='old_run',request_id='old_req')
    await record('request.started',{},context=old)
    original = recorder.append_events_in_tx
    async def failed(*args,**kwargs): raise OSError('simulated database outage')
    monkeypatch.setattr(recorder,'append_events_in_tx',failed)
    with pytest.raises(RecordingError):
        await record_stream(old,{'type':'request.delta','data':{'chunk_index':0,'delta':'uncommitted'}})
    monkeypatch.setattr(recorder,'append_events_in_tx',original)
    new = old.derive(run_id='new_run',request_id='new_req')
    await record('request.started',{},context=new)
    async with factory() as db:
        rows=(await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        assert [row.type for row in rows]==['trajectory.started','request.started','recording.gap','request.started']
        assert rows[2].data['last_committed_seq']=='2'
        assert 'uncommitted' not in json.dumps(rows[-1].data)
    assert recorder._capacity().used==0

async def test_checkpoint_pages_reuse_unchanged_history(tracedb):
    factory, _ = tracedb
    from db.models.trajectory import TrajectoryCheckpoint
    ctx=TraceContext('a','session_a_1')
    events=[]
    for number in range(51):
        for typ,data in [('request.started',{}),('request.delta',{'delta':'long output '*100,'chunk_index':0}),('request.finished',{'status':'completed'})]:
            events.append({'type':typ,'data':data,'request_id':f'req_{number}'})
    await append(factory,ctx,events)
    async with factory.begin() as db:
        trajectory=await db.scalar(select(SessionTrajectory)); first=await create_checkpoint_in_tx(db,trajectory)
        first_pages=first.state['record_pages']
    await append(factory,ctx,[{'type':'request.started','request_id':'last','data':{}}])
    async with factory.begin() as db:
        trajectory=await db.scalar(select(SessionTrajectory)); second=await create_checkpoint_in_tx(db,trajectory)
        assert first_pages[0]==second.state['record_pages'][0]
        assert first_pages[-1]!=second.state['record_pages'][-1]


async def test_global_stream_capacity_backpressures_independent_sessions(tracedb, monkeypatch):
    factory, _ = tracedb
    import trajectory.recorder as recorder
    first=TraceContext('a','session_a_1',request_id='req_a')
    second=TraceContext('b','session_b_1',request_id='req_b')
    await record('request.started',{},context=first)
    await record('request.started',{},context=second)
    monkeypatch.setenv('TRAJECTORY_TOTAL_PENDING_BYTES','1500')
    monkeypatch.setenv('TRAJECTORY_BATCH_MS','1')
    entered=asyncio.Event(); release=asyncio.Event(); writes=[]
    original=recorder.append_events_in_tx
    async def blocked(db,context,events):
        writes.append(context.session_id)
        if context.session_id==first.session_id:
            entered.set(); await release.wait()
        return await original(db,context,events)
    monkeypatch.setattr(recorder,'append_events_in_tx',blocked)
    event={'type':'request.delta','data':{'chunk_index':0,'delta':'x'*600}}
    receipt_a=record_stream(first,event)
    await asyncio.wait_for(entered.wait(),1)
    receipt_b=record_stream(second,event)
    await asyncio.sleep(.01)
    assert writes==[first.session_id]
    assert not receipt_b.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(receipt_a,receipt_b),2)
    await flush(first); await flush(second)
    assert writes==[first.session_id,second.session_id]
    assert recorder._capacity().used==0
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryEvent).where(TrajectoryEvent.type=='request.delta'))==2


async def test_malformed_cursors_are_validation_errors(client, tracedb):
    factory,_=tracedb
    from trajectory.repository import cursor_encode
    await record('input.accepted',{'text':'visible'},context=TraceContext('a','session_a_1'))
    for suffix in ('sessions', 'sessions/session_a_1/records', 'sessions/session_a_1/search'):
        key='before' if suffix.endswith('records') else 'cursor'
        path='/api/admin/trajectories/'+suffix
        response=await client.get(path,params={key:cursor_encode({'unexpected':'shape'}),'q':'visible'})
        assert response.status_code==400


async def test_export_shutdown_resumes_and_deletion_wins(tracedb, monkeypatch):
    factory,blob=tracedb
    import trajectory.export as exports
    from db.models.trajectory import TrajectoryExport
    from trajectory import delete_trajectory_in_tx
    await record('input.accepted',{'text':'snapshot'},context=TraceContext('a','session_a_1'))
    async with factory.begin() as db:
        trajectory=await db.scalar(select(SessionTrajectory))
        row=await exports.create_export(db,trajectory,'admin',trajectory.committed_seq)
        export_id=row.id
    original=exports.upload_bytes
    entered=asyncio.Event(); release=asyncio.Event()
    async def delayed(*args,**kwargs):
        entered.set(); await release.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(exports,'upload_bytes',delayed)
    task=asyncio.create_task(exports.build_export(export_id))
    await asyncio.wait_for(entered.wait(),1)
    await exports.stop_exports()
    assert task.cancelled() and not exports._tasks
    async with factory() as db:
        assert (await db.get(TrajectoryExport,export_id)).status=='running'
    entered.clear(); await exports.resume_exports()
    await asyncio.wait_for(entered.wait(),1)
    async with factory.begin() as db:
        await delete_trajectory_in_tx(db,'session_a_1','a')
    running=list(exports._tasks)
    release.set()
    await asyncio.wait_for(asyncio.gather(*running),2)
    async with factory() as db:
        row=await db.get(TrajectoryExport,export_id)
        assert row.status=='deleted' and row.storage_key is None
    assert not any('/exports/' in key for key in blob.objects)
