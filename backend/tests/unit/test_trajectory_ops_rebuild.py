"""Trace database rebuild drill (trajectory.ops.rebuild) against SQLite trace databases and fake blob stores."""
import asyncio
import base64
import hashlib
import hmac
import io
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl

import httpx
import pytest
import zstandard
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, insert, select, update,
)
from sqlalchemy.ext.asyncio import create_async_engine

from trajectory.ops import rebuild

TRAJECTORY = "trj_1"
SEGMENT_KEY = "trajectories/trj_1/segments/000000000001-000000000003.jsonl.zst"
STARTED = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


def schema() -> MetaData:
    """The columns of SPEC §6.3 that the drill reads, as a SQLite trace database stores them."""
    metadata = MetaData()
    Table(
        "session_trajectories", metadata,
        Column("id", String(64), primary_key=True), Column("user_id", String(64)), Column("session_id", String(64)),
        Column("workspace_id", String(64)), Column("started_at", DateTime(timezone=True)),
        Column("last_activity_at", DateTime(timezone=True)), Column("next_seq", BigInteger),
        Column("committed_seq", BigInteger), Column("projected_seq", BigInteger), Column("archived_seq", BigInteger),
        Column("checkpoint_seq", BigInteger), Column("recording_status", String(32)), Column("event_count", BigInteger),
        Column("deleted_at", DateTime(timezone=True)), Column("content_expired_at", DateTime(timezone=True)),
    )
    Table(
        "trajectory_events", metadata,
        Column("trajectory_id", String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True),
        Column("seq", BigInteger, primary_key=True), Column("recorded_on", Date), Column("event_id", String(128)),
        Column("type", String(64)), Column("version", Integer), Column("user_id", String(64)),
        Column("session_id", String(64)), Column("source_session_id", String(64)), Column("request_id", String(128)),
        Column("call_id", String(128)), Column("agent_id", String(128)), Column("context", Text), Column("data", Text),
        Column("hints", Text), Column("content_hash", String(64)), Column("occurred_at", DateTime(timezone=True)),
        Column("recorded_at", DateTime(timezone=True)),
    )
    Table(
        "trajectory_segments", metadata,
        Column("trajectory_id", String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True),
        Column("from_seq", BigInteger, primary_key=True), Column("to_seq", BigInteger), Column("storage_key", Text),
        Column("event_count", Integer), Column("raw_bytes", BigInteger), Column("stored_bytes", BigInteger),
        Column("sha256", String(64)), Column("compression", String(16)), Column("created_at", DateTime(timezone=True)),
    )
    Table(
        "trajectory_event_keys", metadata,
        Column("event_id", String(128), primary_key=True), Column("trajectory_id", String(64)),
        Column("seq", BigInteger), Column("content_hash", String(64)), Column("recorded_at", DateTime(timezone=True)),
    )
    return metadata


def stored_event(seq: int) -> dict:
    return {
        "event_id": f"evt_{seq}", "trajectory_id": TRAJECTORY, "seq": seq, "type": "request.delta", "version": 1,
        "user_id": "user_1", "session_id": "session_1", "source_session_id": "session_1", "request_id": "req_1",
        "call_id": None, "agent_id": "agent_1", "context": {"run_id": "run_1", "generation": 2},
        "data": {"text": f"chunk {seq} 你好", "ratio": 1.5, "nested": {"b": [1, 2], "a": None}},
        "hints": {"preview": {"text": f"chunk {seq}"}} if seq % 2 else None,
        "content_hash": hashlib.sha256(f"content {seq}".encode()).hexdigest(),
        "occurred_at": f"2026-09-14T08:00:{seq:02d}.123Z", "recorded_at": f"2026-09-14T08:00:{seq:02d}.456Z",
    }


def hot_row(event: dict) -> dict:
    """A hot event the way the worker stores it on SQLite: JSON as text, timestamps as datetimes."""
    row = dict(event)
    for key in ("context", "data", "hints"):
        row[key] = None if event[key] is None else json.dumps(event[key], ensure_ascii=False)
    for key in ("occurred_at", "recorded_at"):
        row[key] = datetime.fromisoformat(event[key].replace("Z", "+00:00"))
    row["recorded_on"] = row["recorded_at"].date()
    return row


def oss_signature(verb: str, expires: str, resource: str) -> str:
    string_to_sign = f"{verb}\n\n\n{expires}\n{resource}"
    return base64.b64encode(hmac.new(b"oss-secret", string_to_sign.encode(), hashlib.sha1).digest()).decode()


class Drill:
    def __init__(self, tmp_path):
        self.source_url = f"sqlite+aiosqlite:///{tmp_path / 'openbox_trace.db'}"
        self.scratch_url = f"sqlite+aiosqlite:///{tmp_path / 'openbox_trace_rebuild_test.db'}"
        self.blobs = tmp_path / "blobs"
        self.events = [stored_event(seq) for seq in range(1, 6)]
        self.raw = b"".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for event in self.events[:3]
        )
        self.stored = zstandard.ZstdCompressor(level=3).compress(self.raw)
        self.environ = {
            "TRAJECTORY_DATABASE_URL": self.source_url,
            "REBUILD_SCRATCH_URL": self.scratch_url,
            "TRAJECTORY_BLOB_PROVIDER": "local",
            "TRAJECTORY_BLOB_LOCAL_PATH": str(self.blobs),
        }
        asyncio.run(self._create())
        (self.blobs / SEGMENT_KEY).parent.mkdir(parents=True)
        (self.blobs / SEGMENT_KEY).write_bytes(self.stored)

    async def _create(self) -> None:
        for url in (self.source_url, self.scratch_url):
            engine = create_async_engine(url)
            async with engine.begin() as connection:
                await connection.run_sync(schema().create_all)
            await engine.dispose()
        tables = schema().tables
        # executemany takes its columns from the first row, so both rows name every column.
        trajectory = {
            "user_id": "user_1", "workspace_id": "ws_1", "started_at": STARTED, "last_activity_at": STARTED,
            "checkpoint_seq": 0, "deleted_at": None,
        }
        await self.execute(
            insert(tables["session_trajectories"]),
            [
                {**trajectory, "id": TRAJECTORY, "session_id": "session_1", "next_seq": 6, "committed_seq": 5,
                 "projected_seq": 5, "archived_seq": 3, "recording_status": "recording", "event_count": 5},
                {**trajectory, "id": "trj_deleted", "session_id": "session_2", "next_seq": 1, "committed_seq": 0,
                 "projected_seq": 0, "archived_seq": 0, "recording_status": "deleted", "event_count": 0,
                 "deleted_at": STARTED},
            ],
        )
        await self.execute(insert(tables["trajectory_segments"]).values(
            trajectory_id=TRAJECTORY, from_seq=1, to_seq=3, storage_key=SEGMENT_KEY, event_count=3,
            raw_bytes=len(self.raw), stored_bytes=len(self.stored), sha256=hashlib.sha256(self.raw).hexdigest(),
            compression="zstd", created_at=STARTED,
        ))
        await self.execute(insert(tables["trajectory_events"]), [hot_row(event) for event in self.events[3:]])
        await self.execute(insert(tables["trajectory_event_keys"]), [
            {"event_id": event["event_id"], "trajectory_id": TRAJECTORY, "seq": event["seq"],
             "content_hash": event["content_hash"], "recorded_at": STARTED}
            for event in self.events
        ])

    async def execute(self, statement, parameters=None) -> None:
        engine = create_async_engine(self.source_url)
        async with engine.begin() as connection:
            await connection.execute(statement, parameters)
        await engine.dispose()

    def run(self, *argv: str, environ: dict | None = None, transport=None) -> tuple[int, dict | None]:
        stdout = io.StringIO()
        code = rebuild.main(list(argv), environ=self.environ if environ is None else environ, stdout=stdout,
                            transport=transport)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None

    def scratch_rows(self, table: str) -> list[dict]:
        async def read():
            engine = create_async_engine(self.scratch_url)
            async with engine.connect() as connection:
                rows = (await connection.execute(select(schema().tables[table]))).mappings().all()
            await engine.dispose()
            return [dict(row) for row in rows]

        return asyncio.run(read())


@pytest.fixture
def drill(tmp_path):
    return Drill(tmp_path)


def test_rebuild_restores_archived_segments_and_the_hot_tail(drill):
    code, report = drill.run()
    assert code == 0, report
    assert (report["ok"], report["trajectories"], report["segments"], report["events"]) == (True, 1, 1, 5)
    detail = report["reports"][0]
    assert detail["source_digest"] == detail["scratch_digest"]
    assert (detail["segment_errors"], detail["missing_seqs"], detail["key_mismatches"]) == ([], 0, [])
    rows = sorted(drill.scratch_rows("trajectory_events"), key=lambda row: row["seq"])
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 5]
    assert [rebuild.fingerprint(row) for row in rows] == [rebuild.fingerprint(event) for event in drill.events]
    assert rows[0]["recorded_on"] == date(2026, 9, 14) and json.loads(rows[0]["data"]) == drill.events[0]["data"]
    (trajectory,) = drill.scratch_rows("session_trajectories")
    assert (trajectory["id"], trajectory["committed_seq"], trajectory["archived_seq"], trajectory["projected_seq"]) == (
        TRAJECTORY, 5, 0, 0
    )

    code, again = drill.run()
    assert code == 0 and again["reports"][0]["scratch_digest"] == detail["scratch_digest"]
    assert len(drill.scratch_rows("trajectory_events")) == 5


def test_fingerprint_is_the_same_for_a_segment_line_and_a_database_row():
    event = stored_event(7)
    row = hot_row(event)
    row["occurred_at"] = row["occurred_at"].replace(tzinfo=None)
    assert rebuild.fingerprint(event) == rebuild.fingerprint(row)
    changed = {**event, "data": {**event["data"], "ratio": 2}}
    assert rebuild.fingerprint(changed) != rebuild.fingerprint(event)


def test_a_corrupted_segment_fails_the_drill(drill):
    tampered = drill.raw.replace(b"chunk 1", b"chunk 9")
    (drill.blobs / SEGMENT_KEY).write_bytes(zstandard.ZstdCompressor().compress(tampered))
    code, report = drill.run()
    detail = report["reports"][0]
    assert code == 1 and not report["ok"]
    assert "sha256" in detail["segment_errors"][0]
    assert (detail["missing_seqs"], detail["first_missing_seqs"]) == (3, [1, 2, 3])
    assert sorted(row["seq"] for row in drill.scratch_rows("trajectory_events")) == [4, 5]


def test_a_missing_segment_object_fails_the_drill(drill):
    (drill.blobs / SEGMENT_KEY).unlink()
    code, report = drill.run()
    assert code == 1
    assert report["reports"][0]["segment_errors"] == ["1-3: object is missing"]


def test_a_segment_that_does_not_cover_its_range_fails_the_drill(drill):
    raw = b"".join(json.dumps(event).encode() + b"\n" for event in drill.events[:2])
    (drill.blobs / SEGMENT_KEY).write_bytes(zstandard.ZstdCompressor().compress(raw))
    asyncio.run(drill.execute(update(schema().tables["trajectory_segments"]).values(sha256=hashlib.sha256(raw).hexdigest())))
    code, report = drill.run()
    assert code == 1 and "contiguously" in report["reports"][0]["segment_errors"][0]


def test_an_event_key_with_another_content_hash_fails_the_drill(drill):
    keys = schema().tables["trajectory_event_keys"]
    asyncio.run(drill.execute(update(keys).where(keys.c.event_id == "evt_2").values(content_hash="0" * 64)))
    code, report = drill.run()
    assert code == 1 and report["reports"][0]["key_mismatches"] == ["evt_2"]


def test_segments_come_from_oss_through_internal_presigned_gets(drill, monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "oss-key-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "oss-secret")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"/{SEGMENT_KEY}":
            return httpx.Response(200, content=drill.stored)
        return httpx.Response(404)

    environ = {**drill.environ, "TRAJECTORY_BLOB_PROVIDER": "oss", "TRAJECTORY_OSS_BUCKET": "traces", "OSS_REGION": "cn-shanghai"}
    code, report = drill.run(environ=environ, transport=httpx.MockTransport(handler))
    assert code == 0, report
    (request,) = requests
    assert request.method == "GET" and request.url.host == "traces.oss-cn-shanghai-internal.aliyuncs.com"
    query = dict(parse_qsl(request.url.query.decode()))
    assert query["OSSAccessKeyId"] == "oss-key-id"
    assert query["Signature"] == oss_signature("GET", query["Expires"], f"/traces/{SEGMENT_KEY}")


def test_unknown_trajectories_fail_and_deleted_ones_are_not_rebuilt(drill):
    code, report = drill.run("--only", "trj_missing", "trj_deleted")
    assert code == 1
    assert (report["trajectories"], report["not_found"]) == (0, ["trj_deleted", "trj_missing"])


def test_scratch_database_guard():
    live = "postgresql+asyncpg://openbox:secret-pw@postgres:5432/openbox_trace"
    with pytest.raises(rebuild.RebuildError, match="must start with"):
        rebuild.check_urls(live, "postgresql+asyncpg://openbox:secret-pw@postgres:5432/openbox_trace_copy")
    with pytest.raises(rebuild.RebuildError, match="must differ"):
        rebuild.check_urls(
            "postgresql+asyncpg://openbox:secret-pw@other:5432/openbox_trace_rebuild_1",
            "postgresql+asyncpg://openbox:secret-pw@postgres:5432/openbox_trace_rebuild_1",
        )
    rebuild.check_urls(
        live,
        "postgresql+asyncpg://openbox:secret-pw@postgres:5432/openbox_trace_rebuild_20260915",
        "postgresql+asyncpg://openbox:secret-pw@postgres:5432/openbox",
    )
    with pytest.raises(rebuild.RebuildError) as raised:
        rebuild.check_urls(live, "not a url :: secret-pw")
    assert "secret-pw" not in str(raised.value)


def test_cli_refuses_unsafe_or_incomplete_configuration(drill, tmp_path):
    assert drill.run(environ={**drill.environ, "REBUILD_SCRATCH_URL": drill.source_url})[0] == 2
    assert drill.run(environ={k: v for k, v in drill.environ.items() if k != "REBUILD_SCRATCH_URL"})[0] == 2
    assert drill.run("--limit", "0")[0] == 2
    assert drill.run(environ={**drill.environ, "TRAJECTORY_BLOB_PROVIDER": "gcs"})[0] == 2
    empty = f"sqlite+aiosqlite:///{tmp_path / 'openbox_trace_rebuild_empty.db'}"
    assert drill.run(environ={**drill.environ, "REBUILD_SCRATCH_URL": empty})[0] == 2
