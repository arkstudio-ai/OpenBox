"""Reproducible local-only trajectory scale benchmark; never calls a model."""
import argparse
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics as stats
import tempfile
import time

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.engine import make_url
from db.base import Base
import db.base as database
from db.models.user import User
from db.models.workspace import Workspace
from db.models.project import Project
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent, TrajectoryRecord, TrajectoryPayload
from trajectory.context import TraceContext
from trajectory.recorder import append_events_in_tx
from trajectory.repository import (create_checkpoint_in_tx, get_session_header, list_records, list_sessions, state_at)
from trajectory.types import digest, now


def percentile(values, fraction=0.95):
    return sorted(values)[max(0, min(len(values) - 1, math.ceil(len(values) * fraction) - 1))]


async def main(output: str, requests: int = 5000):
    logging.disable(logging.CRITICAL)
    os.environ['TRAJECTORY_RECORDING_ENABLED'] = 'true'
    url = os.getenv('TRAJECTORY_BENCHMARK_DATABASE_URL')
    if url:
        parsed = make_url(url)
        if parsed.host not in {'localhost', '127.0.0.1'} or not parsed.database.startswith('openbox_trajectory_storage_'):
            raise RuntimeError('Only the dedicated local disposable benchmark database is allowed')
    else:
        directory = tempfile.mkdtemp(prefix='openbox-trajectory-scale-')
        url = f'sqlite+aiosqlite:///{directory}/benchmark.sqlite'
    engine = create_async_engine(url, connect_args={'timeout': 30} if url.startswith('sqlite') else {})
    import db.models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database._engine, database._session_factory = engine, factory
    stamp = now()
    async with factory.begin() as db:
        db.add(User(id='bench_user', username='bench', role='user', created_at=stamp, updated_at=stamp)); await db.flush()
        db.add(Workspace(id='bench_ws', name='Bench', owner_user_id='bench_user', created_at=stamp, updated_at=stamp)); await db.flush()
        db.add(Project(id='bench_project', name='Bench', user_id='bench_user', workspace_id='bench_ws', created_at=stamp, updated_at=stamp)); await db.flush()
        db.add(Session(id='bench_session', user_id='bench_user', workspace_id='bench_ws', project_id='bench_project', title='100,000 events', created_at=stamp, updated_at=stamp))
    ctx = TraceContext('bench_user', 'bench_session', agent_id='agent_main')
    batch_latencies, headers, pages, seeks = [], [], [], []
    checkpoints = []
    started = time.perf_counter()
    for batch in range(0, requests, 10):
        events = []
        for request in range(batch, min(batch + 10, requests)):
            req = f'request_{request:05d}'
            common = {'request_id': req, 'message_id': f'message_{request:05d}', 'agent_id': f'agent_{request % 4}'}
            events.append({**common, 'type': 'request.started', 'data': {'model': 'fixture', 'purpose': 'benchmark', 'input': {'messages': [{'role': 'system', 'content': 'System instructions. ' * 40}, {'role': 'user', 'content': f'Request {request}'}]}}})
            for chunk in range(17):
                events.append({**common, 'type': 'request.delta', 'data': {'chunk_index': chunk, 'block_id': 'text:0', 'block_type': 'text', 'delta': f'{request}:{chunk} ' + 'Observed text. ' * 6}})
            events.append({**common, 'type': 'request.usage', 'data': {'mode': 'replace', 'usage': {'input_tokens': 240, 'output_tokens': 120}}})
            events.append({**common, 'type': 'request.finished', 'data': {'status': 'completed', 'duration_ms': 32}})
        clock = time.perf_counter()
        async with factory.begin() as db:
            await append_events_in_tx(db, ctx, events)
        batch_latencies.append((time.perf_counter() - clock) * 1000)
        # Benchmark checkpoint strategy at known boundaries without including
        # background snapshot work in the critical capture receipt latency.
        if (batch + 10) % 500 == 0 or batch + 10 >= requests:
            async with factory.begin() as db:
                trajectory = await db.scalar(select(SessionTrajectory))
                clock = time.perf_counter()
                checkpoint = await create_checkpoint_in_tx(db, trajectory)
                checkpoints.append({'through_seq': str(checkpoint.through_seq), 'ms': (time.perf_counter() - clock) * 1000})
    write_seconds = time.perf_counter() - started
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        counts = {'events': await db.scalar(select(func.count()).select_from(TrajectoryEvent)), 'records': await db.scalar(select(func.count()).select_from(TrajectoryRecord))}
        expected_head = trajectory.committed_seq
    for attempt in range(10):
        async with factory() as db:
            clock = time.perf_counter(); header = await get_session_header(db, 'bench_session'); headers.append((time.perf_counter() - clock) * 1000)
            trajectory = await db.scalar(select(SessionTrajectory))
            clock = time.perf_counter(); page = await list_records(db, trajectory, limit=100); pages.append((time.perf_counter() - clock) * 1000)
            target = min(expected_head, max(1, (attempt + 1) * expected_head // 11))
            clock = time.perf_counter(); state = await state_at(db, trajectory, target); seeks.append((time.perf_counter() - clock) * 1000)
            assert int(state['through_seq']) == target
            assert all(int(item['as_of_seq']) <= target for item in state['records'].values())
    async with factory() as db:
        staging = await db.scalar(select(func.sum(TrajectoryPayload.size_bytes)))
        payload_count = await db.scalar(select(func.count()).select_from(TrajectoryPayload))
    result = {'environment': {'platform': platform.platform(), 'processor': platform.machine(), 'python': platform.python_version(), 'database': engine.dialect.name,
                              'storage': 'durable database staging; no Blob network', 'concurrent_writers': 1},
              'workload': {**counts, 'requests': requests, 'events_per_batch': 200, 'delta_characters_approx': 95, 'initial_input_characters_approx': 840, 'agent_identities': 4},
              'measurements_ms': {'capture_batch_p50': stats.median(batch_latencies), 'capture_batch_p95': percentile(batch_latencies), 'header_p95': percentile(headers), 'record_page_p95': percentile(pages), 'seek_p95': percentile(seeks), 'seek_samples': seeks, 'header_samples': headers, 'record_page_samples': pages},
              'write_seconds_including_checkpoints': write_seconds, 'checkpoint_builds': checkpoints,
              'retained_payload_bytes': int(staging or 0), 'retained_payload_count': payload_count,
              'verified_head': str(expected_head), 'statistics': header['statistics'],
              'limitations': ['One local writer; distributed network latency and remote Blob archival are not represented.', 'Capture latency is batch commit receipt, not browser paint.', 'Checkpoint schedule in this benchmark is 10,000 events; production worker defaults to an eligible interval of 1,000 events.']}
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'output': output, 'counts': counts, 'measurements_ms': result['measurements_ms'], 'write_seconds': write_seconds}, ensure_ascii=False))
    await engine.dispose()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--requests', type=int, default=5000)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.requests))
