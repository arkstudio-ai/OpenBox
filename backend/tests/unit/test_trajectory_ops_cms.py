"""CloudMonitor metric push (trajectory.ops.cms): RPC signature v1, request shapes and metric collection."""
import base64
import hashlib
import hmac
import io
import json
import re
from urllib.parse import parse_qsl

import httpx
import pytest

from trajectory.ops import cms

# The request-signature example of the Alibaba Cloud RPC documentation.
PUBLISHED_PARAMETERS = {
    "AccessKeyId": "testid",
    "Action": "DescribeRegions",
    "Format": "XML",
    "SignatureMethod": "HMAC-SHA1",
    "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
    "SignatureVersion": "1.0",
    "Timestamp": "2016-02-23T12:46:24Z",
    "Version": "2014-05-26",
}
PUBLISHED_STRING_TO_SIGN = (
    "GET&%2F&AccessKeyId%3Dtestid%26Action%3DDescribeRegions%26Format%3DXML%26SignatureMethod%3DHMAC-SHA1"
    "%26SignatureNonce%3D3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf%26SignatureVersion%3D1.0"
    "%26Timestamp%3D2016-02-23T12%253A46%253A24Z%26Version%3D2014-05-26"
)
PUBLISHED_SIGNATURE = "OLeaidS1JvxuMvnyHOwuJ+uX5qY="
NOW = 1757836800.0


def independent_percent_encode(text: str) -> str:
    """The documented rule, byte by byte: only A-Z a-z 0-9 - _ . ~ stay literal."""
    unreserved = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(chr(byte) if byte in unreserved else f"%{byte:02X}" for byte in text.encode("utf-8"))


def independent_signature(secret: str, method: str, parameters: dict[str, str]) -> str:
    pairs = sorted((independent_percent_encode(key), independent_percent_encode(value)) for key, value in parameters.items())
    canonical = "&".join(f"{key}={value}" for key, value in pairs)
    string_to_sign = f"{method}&{independent_percent_encode('/')}&{independent_percent_encode(canonical)}"
    return base64.b64encode(hmac.new(f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()


def test_signature_reproduces_the_published_example():
    assert cms.string_to_sign("GET", PUBLISHED_PARAMETERS) == PUBLISHED_STRING_TO_SIGN
    assert cms.sign("testsecret", "GET", PUBLISHED_PARAMETERS) == PUBLISHED_SIGNATURE
    assert independent_signature("testsecret", "GET", PUBLISHED_PARAMETERS) == PUBLISHED_SIGNATURE


@pytest.mark.parametrize(
    "text",
    ["a b", "a*b", "a~b", "a+b", "a/b", "%7E", "=&?#", '{"instance":"gw2"}', "云账号报警联系人", "2026-09-14T08:00:00Z"],
)
def test_percent_encoding_follows_the_documented_rules(text):
    assert cms.percent_encode(text) == independent_percent_encode(text)


def test_space_asterisk_and_tilde_encoding():
    assert cms.percent_encode(" *~") == "%20%2A~"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_signatures_agree_with_an_independent_implementation(method):
    parameters = {
        "AccessKeyId": "LTAI-example",
        "Action": "PutCustomMetric",
        "SecurityToken": "CAIS+token/with=padding",
        "MetricList.1.Dimensions": '{"instance":"gw2"}',
        "MetricList.1.MetricName": "spool_bytes",
        "MetricList.1.Values": '{"value":1.5}',
        "MetricList.10.GroupId": "0",
        "EmailSubject": "轨迹 worker * down ~",
    }
    assert cms.sign("secret/with+chars", method, parameters) == independent_signature(
        "secret/with+chars", method, parameters
    )


def metrics(count: int) -> list[cms.Metric]:
    return [cms.Metric(f"metric_{index}", float(index), {"instance": "gw2"}, "0", 1757836800000) for index in range(count)]


def parse_request(request: httpx.Request) -> tuple[dict[str, str], dict[str, str]]:
    query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
    body = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
    return query, body


async def test_put_custom_metric_list_sends_signed_batches_of_21():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Code": "200", "Message": "", "RequestId": f"req-{len(seen)}"})

    client = cms.CmsClient("ak-id", "ak-secret", region="cn-shanghai", transport=httpx.MockTransport(handler))
    assert await client.put_custom_metric_list(metrics(45)) == ["req-1", "req-2", "req-3"]

    sizes = []
    for request in seen:
        assert request.method == "POST"
        assert (request.url.scheme, request.url.host, request.url.path) == ("https", "metrics.cn-shanghai.aliyuncs.com", "/")
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        query, body = parse_request(request)
        signature = query.pop("Signature")
        assert {key: query[key] for key in ("Action", "Version", "Format", "RegionId", "AccessKeyId")} == {
            "Action": "PutCustomMetric", "Version": "2019-01-01", "Format": "JSON", "RegionId": "cn-shanghai",
            "AccessKeyId": "ak-id",
        }
        assert (query["SignatureMethod"], query["SignatureVersion"]) == ("HMAC-SHA1", "1.0")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", query["Timestamp"])
        assert query["SignatureNonce"]
        assert "SecurityToken" not in query
        assert not set(query) & set(body)
        assert signature == independent_signature("ak-secret", "POST", {**query, **body})
        indexes = {int(key.split(".")[1]) for key in body}
        assert indexes == set(range(1, len(indexes) + 1))
        sizes.append(len(indexes))
    assert sizes == [21, 21, 3]
    assert len({parse_request(request)[0]["SignatureNonce"] for request in seen}) == 3

    _, first = parse_request(seen[0])
    assert {key: value for key, value in first.items() if key.startswith("MetricList.2.")} == {
        "MetricList.2.GroupId": "0",
        "MetricList.2.MetricName": "metric_1",
        "MetricList.2.Dimensions": '{"instance":"gw2"}',
        "MetricList.2.Time": "1757836800000",
        "MetricList.2.Type": "0",
        "MetricList.2.Values": '{"value":1}',
    }


async def test_security_token_is_sent_and_signed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"Code": "200", "RequestId": "r"})

    client = cms.CmsClient("ak", "sk", security_token="STS+token/=", transport=httpx.MockTransport(handler))
    await client.put_custom_metric_list(metrics(1))
    query, body = parse_request(seen[0])
    signature = query.pop("Signature")
    assert query["SecurityToken"] == "STS+token/="
    assert signature == independent_signature("sk", "POST", {**query, **body})


def test_values_keep_integers_and_fractions():
    parameters = cms.metric_parameters([
        cms.Metric("trace_db_bytes", 21474836480.0, {"instance": "gw2"}, "0", 1),
        cms.Metric("host_disk_used_percent", 43.25, {"instance": "gw2"}, "7", 1),
    ])
    assert parameters["MetricList.1.Values"] == '{"value":21474836480}'
    assert parameters["MetricList.2.Values"] == '{"value":43.25}'
    assert parameters["MetricList.2.GroupId"] == "7"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"Code": "SignatureDoesNotMatch", "Message": "Specified signature is not matched.", "RequestId": "r-1"}),
        (200, {"Code": "403", "Message": "throttled", "RequestId": "r-1"}),
    ],
)
async def test_errors_raise_with_code_and_request_id(status, payload):
    client = cms.CmsClient(
        "ak-id", "very-secret", transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    )
    with pytest.raises(cms.CmsError) as raised:
        await client.put_custom_metric_list(metrics(2))
    assert (raised.value.code, raised.value.request_id) == (payload["Code"], "r-1")
    assert "very-secret" not in str(raised.value)


def test_counters_become_increments_and_alarm_windows():
    host = {"host_disk_used_percent": 41.5, "spool_bytes": 2048, "oom_kills_1h": 0, "ignored": "x"}
    health = {"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": False}
    worker = {
        "counters": {"gaps_recorded": 5, "blob_put_failures": 1, "ingest_events": 100},
        "gauges": {"projection_lag_events": 12, "spool_bytes": 999},
    }

    values, state = cms.collect(host, health, worker, {}, now=NOW)
    assert values["worker_up"] == 1 and values["worker_writer"] == 1 and values["worker_degraded"] == 0
    assert values["worker_blob_store_ok"] == 0 and values["worker_db_ok"] == 1
    assert values["projection_lag_events"] == 12
    assert values["spool_bytes"] == 2048  # the host measurement wins
    assert values["host_disk_used_percent"] == 41.5 and "ignored" not in values
    assert values["gaps_recorded_delta"] == 0 and values["gaps_recorded_1h"] == 0  # first sample is the baseline

    worker["counters"].update(gaps_recorded=7, blob_put_failures=12, ingest_events=160)
    values, state = cms.collect(host, health, worker, json.loads(json.dumps(state)), now=NOW + 60)
    assert (values["gaps_recorded_delta"], values["blob_put_failures_delta"], values["ingest_events_delta"]) == (2, 11, 60)
    assert (values["gaps_recorded_1h"], values["blob_put_failures_5m"]) == (2, 11)

    values, state = cms.collect(host, health, worker, state, now=NOW + 400)
    assert values["blob_put_failures_delta"] == 0
    assert (values["gaps_recorded_1h"], values["blob_put_failures_5m"]) == (2, 0)

    restarted = {**health, "uptime_seconds": 30}
    worker["counters"].update(gaps_recorded=9, blob_put_failures=0)
    values, state = cms.collect(host, restarted, worker, state, now=NOW + 460)
    assert values["gaps_recorded_delta"] == 9  # uptime shorter than the sample gap: all new
    assert values["blob_put_failures_delta"] == 0
    assert values["gaps_recorded_1h"] == 11

    worker["counters"].update(gaps_recorded=4)
    values, state = cms.collect(host, health, worker, state, now=NOW + 520)
    assert values["gaps_recorded_delta"] == 4  # a lower value is a restart too

    values, down_state = cms.collect(host, None, None, state, now=NOW + 580)
    assert values["worker_up"] == 0
    assert "worker_writer" not in values and "projection_lag_events" not in values
    assert "gaps_recorded_delta" not in values
    assert values["gaps_recorded_1h"] == 15
    assert down_state["last"]["gaps_recorded"] == state["last"]["gaps_recorded"]

    values, _ = cms.collect(host, health, worker, down_state, now=NOW + 3700)
    assert values["gaps_recorded_1h"] == 13  # the increments at +460 and +520 are still inside the hour
    values, _ = cms.collect(host, health, worker, down_state, now=NOW + 4200)
    assert values["gaps_recorded_1h"] == 0


def test_flat_metric_bodies_and_unreadable_state_are_accepted():
    values, state = cms.collect({}, {"status": "degraded"}, {"gaps_recorded": "3", "ingest_lag_seconds": 1.5,
                                                            "writer": True}, {"last": "garbage"}, now=NOW)
    assert values["worker_degraded"] == 1 and values["ingest_lag_seconds"] == 1.5
    assert state["last"]["gaps_recorded"] == [NOW, 3.0]


def test_read_input_splits_host_metrics_and_state():
    assert cms.read_input('{"a": 1}\n{"version": 1}\n') == ({"a": 1}, {"version": 1})
    assert cms.read_input("not json\n[1]") == ({}, {})
    assert cms.read_input("") == ({}, {})


def worker_transport(counters=None, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": True})
        return httpx.Response(200, json={"counters": counters or {"gaps_recorded": 0}, "gauges": {"ingest_lag_seconds": 0.2}})

    return httpx.MockTransport(handler)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "ak-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ak-secret")
    monkeypatch.delenv("TRAJECTORY_CMS_GROUP_ID", raising=False)
    monkeypatch.delenv("TRAJECTORY_CMS_REGION", raising=False)


def test_push_command_reports_host_and_worker_metrics(credentials):
    bodies = []

    def handler(request):
        query, body = parse_request(request)
        assert query.pop("Signature") == independent_signature("ak-secret", "POST", {**query, **body})
        bodies.append(body)
        return httpx.Response(200, json={"Code": "200", "RequestId": "r"})

    stdout = io.StringIO()
    host = {"host_disk_used_percent": 43.2, "spool_bytes": 10, "spool_oldest_age_seconds": 0, "oom_kills_1h": 0}
    code = cms.main(
        ["push", "--instance", "gw2", "--region", "cn-shanghai"],
        stdin=io.StringIO(json.dumps(host) + "\n"),
        stdout=stdout,
        transport=httpx.MockTransport(handler),
        worker_transport=worker_transport(),
        clock=lambda: NOW,
    )
    assert code == 0
    reported = {
        body[key]: json.loads(body[key.replace("MetricName", "Values")])["value"]
        for body in bodies for key in body if key.endswith(".MetricName")
    }
    assert reported["worker_up"] == 1 and reported["host_disk_used_percent"] == 43.2
    assert reported["ingest_lag_seconds"] == 0.2 and reported["gaps_recorded_1h"] == 0
    assert all(value == '{"instance":"gw2"}' for body in bodies for key, value in body.items() if key.endswith(".Dimensions"))
    state = json.loads(stdout.getvalue())
    assert state["last"]["gaps_recorded"] == [NOW, 0.0]


def test_push_keeps_the_state_when_the_report_fails(credentials):
    stdout = io.StringIO()
    code = cms.main(
        ["push"],
        stdin=io.StringIO("{}\n"),
        stdout=stdout,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable")),
        worker_transport=worker_transport({"gaps_recorded": 2}),
        clock=lambda: NOW,
    )
    assert code == 1
    assert json.loads(stdout.getvalue())["last"]["gaps_recorded"] == [NOW, 2.0]


def test_push_reports_a_worker_that_does_not_answer(credentials):
    bodies = []

    def handler(request):
        bodies.append(parse_request(request)[1])
        return httpx.Response(200, json={"Code": "200", "RequestId": "r"})

    code = cms.main(
        ["push"], stdin=io.StringIO(""), stdout=io.StringIO(), transport=httpx.MockTransport(handler),
        worker_transport=worker_transport(fail=True), clock=lambda: NOW,
    )
    assert code == 0
    names = {value for body in bodies for key, value in body.items() if key.endswith(".MetricName")}
    assert "worker_up" in names and "worker_writer" not in names


def test_put_dry_run_needs_no_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALICLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("ALIYUN_CLI_CONFIG", str(tmp_path / "missing.json"))
    assert cms.main(["put", "--dry-run", "--metric", "worker_up=0", "--metric", "spool_bytes=12"], clock=lambda: NOW) == 0
    printed = json.loads(capsys.readouterr().err)
    assert [(item["name"], item["value"]) for item in printed] == [("spool_bytes", 12.0), ("worker_up", 0.0)]


def test_missing_credentials_and_bad_arguments_exit_2(monkeypatch, tmp_path):
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALICLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("ALIYUN_CLI_CONFIG", str(tmp_path / "missing.json"))
    assert cms.main(["put", "--metric", "worker_up=0"]) == 2
    assert cms.main(["put", "--metric", "worker up=zero"]) == 2
    assert cms.main(["put", "--instance", "gw2 prod", "--metric", "worker_up=0"]) == 2
