"""Recording off/on comparison of the release (trajectory.ops.latency)."""
import io

from trajectory.ops import latency

HISTORY = "/api/agent/session/session_7YBXNNJ7KGM2YPWK39MXAJZXCF/history"


def access(path: str, rt: str, status: int = 200, method: str = "GET") -> str:
    """A frontend access log line: the nginx main format followed by rt= and urt=."""
    return (f'10.0.0.1 - - [15/Sep/2026:10:00:00 +0800] "{method} {path} HTTP/1.1" {status} 512 "-" "Mozilla/5.0" '
            f'"203.0.113.9" rt={rt} urt={rt}\n')


def run(lines: list[str], *argv: str) -> tuple[int, str]:
    stdout = io.StringIO()
    code = latency.main(list(argv), stdin=io.StringIO("".join(lines)), stdout=stdout)
    return code, stdout.getvalue()


def test_routes_collapse_identifiers_and_query_strings():
    assert latency.route("GET", HISTORY + "?limit=8") == "GET /api/agent/session/{id}/history"
    assert latency.route("DELETE", "/api/agent/session/12345") == "DELETE /api/agent/session/{id}"
    assert latency.route("GET", "/assets/index-B7x2k9Qa1c3d.js") == "GET /assets/{id}"
    assert latency.route("GET", "/api/environment") == "GET /api/environment"
    assert latency.route("GET", "/") == "GET /"


def test_percentile_is_nearest_rank():
    assert latency.percentile([float(value) for value in range(100, 0, -1)], 0.95) == 95.0
    assert latency.percentile([3.0], 0.95) == 3.0


def test_a_slower_route_and_more_cpu_are_flagged():
    other_session = HISTORY.replace("7YBXNNJ7KGM2YPWK39MXAJZXCF", "8ZZZZZZZZZZZZZZZZZZZZZZZZ1")
    lines = ["#window off\n"] + [access(HISTORY, "0.040") for _ in range(60)]
    lines += [access("/ws/session", "3600.000", status=101), "cpu=10.0% mem=20.0%\n", "cpu=12.0% mem=21.0%\n"]
    lines += ["#window on\n"] + [access(other_session, "0.200") for _ in range(60)]
    lines += [access("/api/environment", "0.900") for _ in range(3)]
    lines += ["cpu=30.0% mem=22.0%\n", "unrelated error log line\n"]

    code, output = run(lines)

    assert code == 1
    row = next(line for line in output.splitlines() if line.startswith("GET /api/agent/session/{id}/history"))
    assert row.split() == ["GET", "/api/agent/session/{id}/history", "60", "40", "60", "200", "!"]
    # Below --min-requests in the baseline: shown, never flagged. WebSocket lifetimes are not requests.
    assert not next(line for line in output.splitlines() if line.startswith("GET /api/environment")).endswith("!")
    assert "/ws/session" not in output
    assert "backend cpu % [off]: mean 11.0 p95 12.0 max 12.0 (n=2); memory %: mean 20.5 p95 21.0 max 21.0 (n=2)" in output
    assert "! backend mean CPU is 19.0 percentage points higher in on than in off" in output
    assert output.rstrip().endswith("flagged: 2")


def test_changes_within_the_allowance_pass():
    lines = ["#window off\n"] + [access(HISTORY, "0.040") for _ in range(60)] + ["cpu=10.0% mem=20.0%\n"]
    lines += ["#window on\n"] + [access(HISTORY, "0.080") for _ in range(60)] + ["cpu=15.0% mem=20.0%\n"]
    # p95 doubled but grew by only 40 ms, below --slack-ms; CPU grew by 5 points.
    assert run(lines)[0] == 0
    assert run(lines, "--slack-ms", "10")[0] == 1


def test_windows_without_durations_exit_2():
    without_rt = '10.0.0.1 - - [15/Sep/2026:10:00:00 +0800] "GET / HTTP/1.1" 200 1 "-" "-" "-"\n'
    assert run(["#window off\n", without_rt, "#window on\n", access(HISTORY, "0.1")])[0] == 2
    assert run([access(HISTORY, "0.1")])[0] == 2
