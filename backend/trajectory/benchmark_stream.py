"""Measure recorder receipt delay with four independent live root sessions."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import time

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from trajectory.benchmark import percentile
from trajectory.benchmark_concurrency import setup


async def main(output):
    logging.disable(logging.CRITICAL)
    os.environ['TRAJECTORY_RECORDING_ENABLED']='true'
    os.environ['TRAJECTORY_BATCH_MS']='50'
    url=os.getenv('TRAJECTORY_STREAM_DATABASE_URL')
    if url:
        parsed=make_url(url)
        assert parsed.host in {'localhost','127.0.0.1'} and parsed.database.startswith('openbox_trajectory_storage_')
    else:
        directory=tempfile.mkdtemp(prefix='trajectory-stream-')
        url=f'sqlite+aiosqlite:///{directory}/database.sqlite'
    await setup(url)
    from db.models.session import Session
    from db.models.user import User
    from db.models.workspace import Workspace
    from db.models.project import Project
    import db.base as database
    from trajectory import TraceContext, record, record_stream, flush
    from trajectory.types import now
    engine=create_async_engine(url,connect_args={'timeout':30} if url.startswith('sqlite') else {})
    factory=async_sessionmaker(engine,expire_on_commit=False)
    database._engine,database._session_factory=engine,factory
    timestamp=now()
    async with factory.begin() as db:
        db.add(User(id='second_owner',username='second_owner',role='user',created_at=timestamp,updated_at=timestamp)); await db.flush()
        db.add(Workspace(id='second_workspace',name='Fixture',owner_user_id='second_owner',created_at=timestamp,updated_at=timestamp)); await db.flush()
        db.add(Project(id='second_project',name='Fixture',user_id='second_owner',workspace_id='second_workspace',created_at=timestamp,updated_at=timestamp)); await db.flush()
        for owner in ('writer','second'):
            for number in range(2):
                db.add(Session(id=f'{owner}_root_{number}',title='Stream fixture',user_id=f'{owner}_owner',workspace_id=f'{owner}_workspace',project_id=f'{owner}_project',created_at=timestamp,updated_at=timestamp))
    samples=[]
    async def producer(owner,number):
        context=TraceContext(f'{owner}_owner',f'{owner}_root_{number}',request_id=f'req_{owner}_{number}')
        await record('request.started',{'model':'local-fixture'},context=context)
        for chunk in range(40):
            start=time.perf_counter()
            event=await record_stream(context,{'type':'request.delta','data':{'chunk_index':chunk,'blocks':[{'block_id':'text:0','type':'text','delta':'x'*256}]}})
            samples.append((time.perf_counter()-start)*1000)
            assert event['type']=='request.delta'
        await record('request.finished',{'status':'completed'},context=context)
        await flush(context)
    await asyncio.gather(*(producer(owner,number) for owner in ('writer','second') for number in range(2)))
    result={'database':engine.dialect.name,'concurrent_root_sessions':4,'owners':2,'chunks_per_root':40,'chunk_characters':256,
            'batch_window_ms':50,'receipt_p95_ms':percentile(samples),'receipt_max_ms':max(samples),'samples_ms':samples,
            'measurement':'Invocation to committed receipt; includes batch wait, excludes browser paint/network and model latency.'}
    Path(output).write_text(json.dumps(result,indent=2))
    print(json.dumps({key:value for key,value in result.items() if key!='samples_ms'}))
    await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',required=True)
    asyncio.run(main(parser.parse_args().output))
