"""Two independent local processes appending to one root trajectory."""
import argparse
import asyncio
import json
import logging
import multiprocessing
import os
from pathlib import Path
import tempfile

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def worker(url, index, barrier, results):
    logging.disable(logging.CRITICAL)
    async def write():
        from trajectory import TraceContext, append_events_in_tx
        factory_engine = create_async_engine(url, connect_args={"timeout": 30} if url.startswith("sqlite") else {})
        factory = async_sessionmaker(factory_engine, expire_on_commit=False)
        context = TraceContext("writer_owner", "shared_root")
        barrier.wait(timeout=20)
        for number in range(25):
            event = {"type":"input.accepted", "event_id":f"process_{index}_{number}",
                     "data":{"writer":index,"number":number,"text":"Observed local concurrency fixture"}}
            async with factory.begin() as db:
                await append_events_in_tx(db, context, [event])
            if number == 5:
                async with factory.begin() as db:
                    await append_events_in_tx(db, context, [event])
        await factory_engine.dispose()
    try:
        asyncio.run(write())
    except BaseException as exc:
        chain=[]
        current=exc
        while current is not None and len(chain)<8:
            chain.append({"type":type(current).__name__,"sqlstate":getattr(current,"sqlstate",None),
                          "constraint":getattr(current,"constraint_name",None),"table":getattr(current,"table_name",None)})
            current=current.__cause__
        results.put({"pid":os.getpid(),"error":type(exc).__name__,"causes":chain})
        raise SystemExit(1)
    results.put({"pid":os.getpid(),"events":25,"retries":1})


async def setup(url):
    from db.base import Base
    import db.models
    from db.models.user import User
    from db.models.workspace import Workspace
    from db.models.project import Project
    from db.models.session import Session
    from trajectory.types import now
    engine=create_async_engine(url)
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.drop_all)
        await db.run_sync(Base.metadata.create_all)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    timestamp=now()
    async with factory.begin() as db:
        db.add(User(id="writer_owner",username="writer_owner",role="user",created_at=timestamp,updated_at=timestamp)); await db.flush()
        db.add(Workspace(id="writer_workspace",name="Fixture",owner_user_id="writer_owner",created_at=timestamp,updated_at=timestamp)); await db.flush()
        db.add(Project(id="writer_project",name="Fixture",user_id="writer_owner",workspace_id="writer_workspace",created_at=timestamp,updated_at=timestamp)); await db.flush()
        db.add(Session(id="shared_root",title="Concurrency",user_id="writer_owner",workspace_id="writer_workspace",project_id="writer_project",created_at=timestamp,updated_at=timestamp))
    await engine.dispose()


async def verify(url):
    from db.models.trajectory import SessionTrajectory, TrajectoryEvent
    engine=create_async_engine(url)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory() as db:
        rows=(await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        trajectory=await db.scalar(select(SessionTrajectory))
        assert len(rows)==51
        assert [row.seq for row in rows]==list(range(1,52))
        assert len({row.event_id for row in rows})==51
        assert trajectory.committed_seq==51 and trajectory.next_seq==52
        result={"database":engine.dialect.name,"events":len(rows),"unique_event_ids":51,"committed_seq":"51","next_seq":"52","contiguous":True}
    await engine.dispose()
    return result


def main(output):
    logging.disable(logging.CRITICAL)
    url=os.getenv("TRAJECTORY_CONCURRENCY_DATABASE_URL")
    if url:
        parsed=make_url(url)
        assert parsed.host in {"localhost","127.0.0.1"} and parsed.database.startswith("openbox_trajectory_storage_"), "Only a dedicated local disposable database is allowed"
    else:
        directory=tempfile.mkdtemp(prefix="trajectory-processes-")
        url=f"sqlite+aiosqlite:///{directory}/database.sqlite"
    asyncio.run(setup(url))
    process_context=multiprocessing.get_context("spawn")
    barrier=process_context.Barrier(2)
    results=process_context.Queue()
    workers=[process_context.Process(target=worker,args=(url,index,barrier,results)) for index in range(2)]
    for process in workers: process.start()
    for process in workers: process.join(timeout=45)
    for process in workers:
        if process.is_alive(): process.terminate(); process.join()
    identities=[results.get(timeout=5) for _ in workers]
    assert all(process.exitcode==0 for process in workers), f"Concurrent writer failed: {identities}"
    assert len({item["pid"] for item in identities})==2
    result={**asyncio.run(verify(url)),"processes":identities,"start_method":"spawn","simultaneous_start_barrier":True}
    Path(output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",required=True)
    main(parser.parse_args().output)
