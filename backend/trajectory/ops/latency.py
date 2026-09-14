"""Recording off/on comparison of the trajectory release (deploy/gw2/RUNBOOK.md §5 step 9).

Reads windows from stdin. A line ``#window NAME`` starts a window; the lines
after it are frontend access log lines, whose ``rt=`` field (nginx
$request_time) is the request duration, and backend ``docker stats`` samples
written as ``cpu=12.5% mem=20.1%``. Other lines are ignored:

    { echo '#window off'; cat access-off.log stats-off.txt
      echo '#window on'; cat access-on.log stats-on.txt; } |
      docker compose exec -T trajectory-worker python -m trajectory.ops.latency

Per route (method and path, identifier segments shown as ``{id}``) it prints
the request count and p95 duration of every window, then the mean, p95 and
maximum backend CPU and memory. The first window is the baseline and the last
the candidate: a route with at least --min-requests requests in both whose p95
grew by more than --max-increase (a ratio) and more than --slack-ms, or a mean
CPU higher by more than --max-cpu-points percentage points, is flagged with
``!``. WebSocket upgrades (status 101) are skipped: their duration is the
connection's lifetime.

Exit status: 0 nothing flagged, 1 something flagged, 2 a window without
requests (for example a frontend image that does not log rt=).
"""
import argparse
import math
import re
import sys

_WINDOW = re.compile(r"^#window\s+(\S+)")
_REQUEST = re.compile(r'"([A-Z]{3,10}) (\S+) HTTP/[0-9.]+" (\d{3}) ')
_DURATION = re.compile(r"(?:^|\s)rt=(\d+(?:\.\d+)?)(?:\s|$)")
_SAMPLE = re.compile(r"^cpu=(\d+(?:\.\d+)?)%?\s+mem=(\d+(?:\.\d+)?)%?\s*$")
_DIGIT = re.compile(r"\d")


def route(method: str, target: str) -> str:
    """Method and path; numbers and long segments containing digits (ids, hashed assets) become {id}."""
    path = target.split("?", 1)[0].split("#", 1)[0]
    segments = [
        "{id}" if segment.isdigit() or (len(segment) >= 16 and _DIGIT.search(segment)) else segment
        for segment in path.split("/")
    ]
    return f"{method} {'/'.join(segments) or '/'}"


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


class Window:
    def __init__(self, name: str):
        self.name = name
        self.durations: dict[str, list[float]] = {}
        self.cpu: list[float] = []
        self.memory: list[float] = []

    @property
    def requests(self) -> int:
        return sum(len(values) for values in self.durations.values())

    def add(self, line: str) -> None:
        sample = _SAMPLE.match(line.strip())
        if sample:
            self.cpu.append(float(sample.group(1)))
            self.memory.append(float(sample.group(2)))
            return
        request = _REQUEST.search(line)
        duration = _DURATION.search(line)
        if request is None or duration is None or request.group(3) == "101":
            return
        self.durations.setdefault(route(request.group(1), request.group(2)), []).append(float(duration.group(1)))


def read_windows(lines) -> list[Window]:
    windows: list[Window] = []
    for line in lines:
        marker = _WINDOW.match(line)
        if marker:
            windows.append(Window(marker.group(1)))
        elif windows:
            windows[-1].add(line)
    return windows


def _usage(values: list[float]) -> str:
    if not values:
        return "no samples"
    return f"mean {sum(values) / len(values):.1f} p95 {percentile(values, 0.95):.1f} max {max(values):.1f} (n={len(values)})"


def report(windows: list[Window], *, min_requests: int, max_increase: float, slack_ms: float,
           max_cpu_points: float, top: int, out) -> int:
    baseline, candidate = windows[0], windows[-1]
    compare = len(windows) > 1
    routes = sorted(
        {name for window in windows for name in window.durations},
        key=lambda name: (-sum(len(window.durations.get(name, [])) for window in windows), name),
    )
    header = "  ".join(f"{window.name + ' n':>10} {window.name + ' p95 ms':>12}" for window in windows)
    out(f"{'route':<60}  {header}")
    flagged = 0
    for name in routes[:top]:
        cells = []
        for window in windows:
            values = window.durations.get(name, [])
            p95 = f"{percentile(values, 0.95) * 1000:.0f}" if values else "-"
            cells.append(f"{len(values):>10} {p95:>12}")
        mark = ""
        before, after = baseline.durations.get(name, []), candidate.durations.get(name, [])
        if compare and len(before) >= min_requests and len(after) >= min_requests:
            old, new = percentile(before, 0.95), percentile(after, 0.95)
            if new > old * (1 + max_increase) and (new - old) * 1000 > slack_ms:
                mark = "  !"
                flagged += 1
        out(f"{name[:60]:<60}  {'  '.join(cells)}{mark}")
    if len(routes) > top:
        out(f"... {len(routes) - top} more routes")
    for window in windows:
        out(f"backend cpu % [{window.name}]: {_usage(window.cpu)}; memory %: {_usage(window.memory)}")
    if compare and baseline.cpu and candidate.cpu:
        points = sum(candidate.cpu) / len(candidate.cpu) - sum(baseline.cpu) / len(baseline.cpu)
        if points > max_cpu_points:
            out(f"! backend mean CPU is {points:.1f} percentage points higher in {candidate.name} than in {baseline.name}")
            flagged += 1
    out(f"flagged: {flagged}")
    return 1 if flagged else 0


def main(argv: list[str] | None = None, *, stdin=None, stdout=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trajectory.ops.latency", description=__doc__.splitlines()[0])
    parser.add_argument("--min-requests", type=int, default=50, help="requests a route needs in both windows (50)")
    parser.add_argument("--max-increase", type=float, default=0.2, help="allowed p95 growth ratio (0.2)")
    parser.add_argument("--slack-ms", type=float, default=50.0, help="p95 growth always allowed, in ms (50)")
    parser.add_argument("--max-cpu-points", type=float, default=10.0, help="allowed mean CPU growth (10 points)")
    parser.add_argument("--top", type=int, default=40, help="routes to print, busiest first (40)")
    args = parser.parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    windows = read_windows(stdin)
    empty = [window.name for window in windows if window.requests == 0]
    if not windows or empty:
        print(f"latency: no access log line with rt= in window(s) {', '.join(empty) or '(none: add #window lines)'}",
              file=sys.stderr)
        return 2
    return report(windows, min_requests=args.min_requests, max_increase=args.max_increase, slack_ms=args.slack_ms,
                  max_cpu_points=args.max_cpu_points, top=max(args.top, 1),
                  out=lambda line: print(line, file=stdout))


if __name__ == "__main__":
    raise SystemExit(main())
