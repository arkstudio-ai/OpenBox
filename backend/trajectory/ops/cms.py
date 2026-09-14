"""CloudMonitor custom metrics for the trajectory deployment (SPEC §8.13, §12).

The host timer (deploy/gw2/scripts/push-metrics.sh) measures what only the host
can see: disk usage, the spool volume, kernel OOM kills and database size. It
pipes those numbers into ``python -m trajectory.ops.cms push`` inside the worker
container, which adds the worker's /health and /metrics, turns counters into
per-sample increments and trailing alarm windows, and reports everything.

The reporting operation is ``PutCustomMetric`` (API 2019-01-01) with its
``MetricList`` parameter. SPEC §8.13 calls it PutCustomMetricList after that
parameter; CloudMonitor has no action by that name. One call carries at most 21
entries. Signing is Aliyun RPC signature v1 written out with the standard
library, the way core/oss.py hand-writes OSS V1: one operation does not justify
an SDK.

``push`` reads the host metrics object from the first stdin line and the
previous state object from the rest (both optional), and prints the next state
as one line on stdout, also when the report fails, so counter samples survive a
CloudMonitor outage. Everything else goes to stderr.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx

from core.aliyun import AliyunCredentialsError, load_credentials

API_VERSION = "2019-01-01"
ACTION = "PutCustomMetric"
MAX_METRICS_PER_CALL = 21
DEFAULT_REGION = "cn-shanghai"
DEFAULT_INSTANCE = "gw2"
STATE_VERSION = 1

# Worker /metrics names (SPEC §8.13, plus the wave-3 names of contract 5). A name
# the worker does not serve is simply not reported.
COUNTERS = (
    "ingest_lines", "ingest_events", "duplicates", "idempotency_conflicts", "deleted_drops", "ownership_drops",
    "gaps_recorded", "producer_loss_events", "quarantined_files", "blob_puts", "blob_put_bytes",
    "blob_put_failures", "segment_uploads", "segment_failures", "gc_deleted", "gc_failures", "exports_built",
    "blob_put_raw_bytes", "audit_dead_letters", "analytics_exports", "analytics_export_failures", "failed_batches",
)
GAUGES = (
    "spool_bytes", "spool_files", "spool_oldest_age_seconds", "ingest_lag_seconds", "projection_lag_events",
    "archive_lag_events", "gc_queue_depth", "trace_db_bytes", "hot_events_rows", "trajectories_degraded",
    "trajectories_blocked", "stale_hot_partitions",
    "events_ingested_24h", "hot_partitions", "budget_degraded_trajectories", "budget_degraded_users",
)
# Measured on the host by push-metrics.sh. They replace the worker's view of the
# same quantity because they keep flowing while the worker is down.
# backend_cpu_percent and backend_mem_percent come from `docker stats` (CPU as a
# percentage of one core, memory of the container limit);
# business_trajectory_statements counts pg_stat_statements entries of the
# business database that mention trajectory_ tables other than legacy_trajectory_*.
HOST_METRICS = (
    "host_disk_used_percent", "docker_disk_used_percent", "spool_bytes", "spool_files",
    "spool_oldest_age_seconds", "spool_quarantined_files", "oom_kills_1h", "backend_oom_kills_1h",
    "trace_db_bytes", "backend_cpu_percent", "backend_mem_percent", "business_trajectory_statements",
)
# Reported with ``put`` by jobs other than the minute timer (setup-alarms.sh has
# rules on them): analytics-export.sh sends 1 after a failed export and 0 after a
# successful one.
JOB_METRICS = ("analytics_export_failed",)
# Alarm windows used by setup-alarms.sh: counter increments summed over the
# trailing number of seconds.
WINDOWS = {"gaps_recorded_1h": ("gaps_recorded", 3600), "blob_put_failures_5m": ("blob_put_failures", 300)}
_WINDOW_COUNTERS = {counter for counter, _ in WINDOWS.values()}
_HISTORY_SECONDS = max(seconds for _, seconds in WINDOWS.values()) + 300

_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
# Dimension keys and values: at most 64 letters, digits, ".", "-", "_", "/" or "\".
_DIMENSION = re.compile(r"^[A-Za-z0-9._/\\-]{1,64}$")
_GROUP_ID = re.compile(r"^[0-9]{1,20}$")


class CmsError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str | None):
        super().__init__(f"CloudMonitor {code} (HTTP {status}): {message} [request {request_id or '-'}]")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


def percent_encode(value: str) -> str:
    """RFC 3986 encoding of RPC signatures: only A-Z a-z 0-9 - _ . ~ stay, space is %20."""
    return quote(value, safe="~")


def canonicalized_query(parameters: dict[str, str]) -> str:
    return "&".join(f"{percent_encode(key)}={percent_encode(value)}" for key, value in sorted(parameters.items()))


def string_to_sign(method: str, parameters: dict[str, str]) -> str:
    return f"{method.upper()}&{percent_encode('/')}&{percent_encode(canonicalized_query(parameters))}"


def sign(access_key_secret: str, method: str, parameters: dict[str, str]) -> str:
    """Base64 HMAC-SHA1 keyed with the secret plus "&" (RPC signature v1)."""
    digest = hmac.new(f"{access_key_secret}&".encode(), string_to_sign(method, parameters).encode(), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode()


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    dimensions: dict[str, str]
    group_id: str
    time_ms: int


def _plain(value: float) -> int | float:
    return int(value) if float(value).is_integer() and abs(value) < 2**53 else float(value)


def metric_parameters(metrics: list[Metric]) -> dict[str, str]:
    """``MetricList.N.*`` parameters of raw (Type 0) values."""
    parameters: dict[str, str] = {}
    for index, metric in enumerate(metrics, start=1):
        prefix = f"MetricList.{index}."
        parameters[prefix + "GroupId"] = metric.group_id
        parameters[prefix + "MetricName"] = metric.name
        parameters[prefix + "Dimensions"] = json.dumps(metric.dimensions, sort_keys=True, separators=(",", ":"))
        parameters[prefix + "Time"] = str(metric.time_ms)
        parameters[prefix + "Type"] = "0"
        parameters[prefix + "Values"] = json.dumps({"value": _plain(metric.value)}, separators=(",", ":"))
    return parameters


class CmsClient:
    """CloudMonitor RPC calls: POST, signed common parameters in the query, operation parameters in the form body."""

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        *,
        security_token: str | None = None,
        region: str = DEFAULT_REGION,
        endpoint: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self.region = region
        self.endpoint = endpoint or f"metrics.{region}.aliyuncs.com"
        self._key_id = access_key_id
        self._key_secret = access_key_secret
        self._security_token = security_token
        self._transport = transport
        self._timeout = timeout

    def build_request(
        self, action: str, parameters: dict[str, str], *, timestamp: str | None = None, nonce: str | None = None
    ) -> tuple[str, bytes]:
        """The signed URL and the form body of one call."""
        common = {
            "AccessKeyId": self._key_id,
            "Action": action,
            "Format": "JSON",
            "RegionId": self.region,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": nonce or uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": API_VERSION,
        }
        if self._security_token:
            common["SecurityToken"] = self._security_token
        # The signature covers the query and body parameters together.
        common["Signature"] = sign(self._key_secret, "POST", {**common, **parameters})
        return f"https://{self.endpoint}/?{canonicalized_query(common)}", canonicalized_query(parameters).encode()

    async def call(self, action: str, parameters: dict[str, str]) -> dict:
        url, body = self.build_request(action, parameters)
        async with httpx.AsyncClient(transport=self._transport, trust_env=False, timeout=self._timeout) as client:
            response = await client.post(
                url, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        code = str(payload.get("Code") or "")
        if response.status_code != 200 or code not in ("", "200"):
            raise CmsError(
                response.status_code,
                code or f"HTTP{response.status_code}",
                str(payload.get("Message") or response.reason_phrase or ""),
                payload.get("RequestId"),
            )
        return payload

    async def put_custom_metric_list(self, metrics: list[Metric]) -> list[str]:
        """Report ``metrics`` with PutCustomMetric, 21 per call; returns the request ids."""
        request_ids = []
        for start in range(0, len(metrics), MAX_METRICS_PER_CALL):
            payload = await self.call(ACTION, metric_parameters(metrics[start:start + MAX_METRICS_PER_CALL]))
            request_ids.append(str(payload.get("RequestId") or ""))
        return request_ids


def client_from_environment(
    *, region: str, endpoint: str | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> CmsClient:
    """Credentials from ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET, else the ALIYUN_CLI_CONFIG profile."""
    credentials = load_credentials()
    return CmsClient(
        credentials["access_key_id"],
        credentials["access_key_secret"],
        security_token=credentials.get("security_token"),
        region=region,
        endpoint=endpoint,
        transport=transport,
    )


def _number(value) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _numbers(payload) -> dict[str, float]:
    """Numeric values of a worker /metrics body: ``{"counters": {}, "gauges": {}}`` or one flat object."""
    if not isinstance(payload, dict):
        return {}
    sections = [payload[key] for key in ("counters", "gauges") if isinstance(payload.get(key), dict)]
    values: dict[str, float] = {}
    for source in sections or [payload]:
        for key, value in source.items():
            number = None if isinstance(value, bool) else _number(value)
            if number is not None:
                values[str(key)] = number
    return values


def _sample(item) -> tuple[float, float] | None:
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        return None
    first, second = _number(item[0]), _number(item[1])
    return None if first is None or second is None else (first, second)


def advance(
    state: dict, counters: dict[str, float], now: float, uptime: float | None = None
) -> tuple[dict[str, float], dict]:
    """``<counter>_delta`` increments since the previous sample, the alarm windows and the next state.

    A value below the previous sample, or a worker uptime shorter than the time
    since that sample, means the worker restarted and the whole value is new. The
    first sample of a counter only sets its baseline.
    """
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    derived: dict[str, float] = {}
    next_last: dict[str, list[float]] = {}
    next_history: dict[str, list[list[float]]] = {}
    for name in COUNTERS:
        previous = _sample(last.get(name))
        kept = []
        if name in _WINDOW_COUNTERS and isinstance(history.get(name), list):
            kept = [list(entry) for entry in map(_sample, history[name]) if entry and now - entry[0] < _HISTORY_SECONDS]
        if name in counters:
            value = counters[name]
            if previous is None:
                increment = 0.0
            elif value < previous[1] or (uptime is not None and uptime < now - previous[0]):
                increment = value
            else:
                increment = value - previous[1]
            derived[f"{name}_delta"] = increment
            next_last[name] = [now, value]
            if name in _WINDOW_COUNTERS:
                kept.append([now, increment])
        elif previous is not None:
            next_last[name] = list(previous)
        if kept:
            next_history[name] = kept
    for metric, (counter, seconds) in WINDOWS.items():
        derived[metric] = sum(increment for at, increment in next_history.get(counter, []) if now - at < seconds)
    return derived, {"version": STATE_VERSION, "last": next_last, "history": next_history}


def collect(
    host: dict, health: dict | None, worker_metrics: dict | None, state: dict, *, now: float
) -> tuple[dict[str, float], dict]:
    """Every value to report, keyed by metric name, and the next state."""
    values: dict[str, float] = {"worker_up": 0.0 if health is None else 1.0}
    numbers = _numbers(worker_metrics) if health is not None else {}
    uptime = None
    if health is not None:
        values["worker_writer"] = 1.0 if health.get("writer") is True else 0.0
        values["worker_degraded"] = 0.0 if health.get("status") == "ok" else 1.0
        for key in ("db", "spool", "blob_store"):
            if isinstance(health.get(key), bool):
                values[f"worker_{key}_ok"] = 1.0 if health[key] else 0.0
        uptime = _number(health.get("uptime_seconds"))
        if uptime is None:
            uptime = numbers.get("uptime_seconds")
        for name in GAUGES:
            if name in numbers:
                values[name] = numbers[name]
    counters = {name: numbers[name] for name in COUNTERS if name in numbers}
    derived, next_state = advance(state if isinstance(state, dict) else {}, counters, now, uptime)
    values.update(derived)
    for name in HOST_METRICS:
        number = _number(host.get(name)) if isinstance(host, dict) else None
        if number is not None:
            values[name] = number
    return values, next_state


def to_metrics(values: dict[str, float], *, instance: str, group_id: str, now: float) -> list[Metric]:
    for name in values:
        if not _NAME.match(name):
            raise ValueError(f"invalid metric name {name!r}")
    time_ms = int(now * 1000)
    return [Metric(name, value, {"instance": instance}, group_id, time_ms) for name, value in sorted(values.items())]


async def fetch_worker(
    base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0
) -> tuple[dict | None, dict | None]:
    """The worker's /health and /metrics bodies; health is None unless /health answers 200."""
    base = base_url.rstrip("/")

    def body(response: httpx.Response) -> dict:
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=timeout) as client:
        try:
            health = await client.get(f"{base}/health")
        except httpx.HTTPError:
            return None, None
        if health.status_code != 200:
            return None, None
        try:
            metrics = await client.get(f"{base}/metrics")
        except httpx.HTTPError:
            return body(health), None
        return body(health), body(metrics) if metrics.status_code == 200 else None


def _loads_object(text: str, label: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        print(f"cms: ignoring unreadable {label}", file=sys.stderr)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_input(text: str) -> tuple[dict, dict]:
    """Host metrics from the first line, the previous state from the rest."""
    first, _, rest = text.partition("\n")
    return _loads_object(first, "host metrics"), _loads_object(rest, "state")


def parse_assignments(items: list[str]) -> dict[str, float]:
    values = {}
    for item in items:
        name, separator, raw = item.partition("=")
        number = _number(raw) if separator else None
        if not _NAME.match(name) or number is None:
            raise ValueError(f"expected NAME=NUMBER, got {item!r}")
        values[name] = number
    return values


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=os.getenv("TRAJECTORY_CMS_REGION") or DEFAULT_REGION)
    common.add_argument("--endpoint", help="API host, default metrics.<region>.aliyuncs.com")
    common.add_argument("--group-id", default=os.getenv("TRAJECTORY_CMS_GROUP_ID") or "0",
                        help="application group id (TRAJECTORY_CMS_GROUP_ID; 0 means none)")
    common.add_argument("--instance", default=DEFAULT_INSTANCE, help="value of the instance dimension")
    common.add_argument("--dry-run", action="store_true", help="print the metrics to stderr instead of reporting them")
    parser = argparse.ArgumentParser(prog="python -m trajectory.ops.cms", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    push = commands.add_parser("push", parents=[common], help="host metrics on stdin plus the worker's /health and /metrics")
    push.add_argument("--worker-url", default="http://127.0.0.1:8090")
    put = commands.add_parser("put", parents=[common], help="report explicit values, for example to test an alarm rule")
    put.add_argument("--metric", action="append", required=True, metavar="NAME=VALUE")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin=None,
    stdout=None,
    transport: httpx.AsyncBaseTransport | None = None,
    worker_transport: httpx.AsyncBaseTransport | None = None,
    clock=time.time,
) -> int:
    args = _parser().parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if not _DIMENSION.match(args.instance) or not _GROUP_ID.match(args.group_id):
        print("cms: --instance must be a CloudMonitor dimension value and --group-id a number", file=sys.stderr)
        return 2
    now = clock()
    if args.command == "push":
        host, state = read_input(stdin.read())
        health, worker_metrics = asyncio.run(fetch_worker(args.worker_url, transport=worker_transport))
        values, next_state = collect(host, health, worker_metrics, state, now=now)
        stdout.write(json.dumps(next_state, separators=(",", ":")) + "\n")
        stdout.flush()
    else:
        try:
            values = parse_assignments(args.metric)
        except ValueError as exc:
            print(f"cms: {exc}", file=sys.stderr)
            return 2
    metrics = to_metrics(values, instance=args.instance, group_id=args.group_id, now=now)
    if args.dry_run:
        print(json.dumps([asdict(metric) for metric in metrics], indent=2), file=sys.stderr)
        return 0
    try:
        client = client_from_environment(region=args.region, endpoint=args.endpoint, transport=transport)
        request_ids = asyncio.run(client.put_custom_metric_list(metrics))
    except AliyunCredentialsError as exc:
        print(f"cms: {exc}", file=sys.stderr)
        return 2
    except CmsError as exc:
        print(f"cms: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        # Type only: the message of some transport errors carries the signed URL.
        print(f"cms: CloudMonitor request failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"cms: reported {len(metrics)} metrics in {len(request_ids)} calls", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
