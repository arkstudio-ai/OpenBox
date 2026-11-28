"""Aggregate per-file JUnit XML + runs.jsonl into one JSON baseline.

Usage: python aggregate.py <baseline_dir> <selected.json> <out.json> [rerun_dir]
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_xml(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    first = None
    failing = []
    for s in suites:
        for k in ("tests", "failures", "errors", "skipped"):
            tot[k] += int(s.get(k, 0))
        tot["time"] += float(s.get("time", 0))
        for case in s.iter("testcase"):
            for tag in ("failure", "error"):
                el = case.find(tag)
                if el is not None:
                    name = f"{case.get('classname', '')}::{case.get('name', '')}"
                    msg = (el.get("message") or (el.text or "")).strip().splitlines()
                    entry = {"test": name, "kind": tag, "message": (msg[0] if msg else "")[:300]}
                    failing.append(entry)
                    if first is None:
                        first = entry
    tot["passed"] = tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"]
    tot["first_failure"] = first
    tot["failing_tests"] = failing
    return tot


def collect(base: Path) -> dict:
    runs = {}
    for out in sorted(base.glob("out*")):
        if not (out / "runs.jsonl").exists():
            continue
        for line in (out / "runs.jsonl").read_text().splitlines():
            r = json.loads(line)
            r["xml"] = str(out / "xml" / (r["file"].replace("/", "__") + ".xml"))
            r["log"] = str(out / "log" / (r["file"].replace("/", "__") + ".log"))
            runs[r["file"]] = r
    return runs


def main() -> None:
    base = Path(sys.argv[1])
    selected = json.loads(Path(sys.argv[2]).read_text())
    runs = collect(base)
    rerun = collect(Path(sys.argv[4])) if len(sys.argv) > 4 else {}
    files = []
    for s in selected["selected"]:
        f = s["file"]
        r = runs.get(f)
        rec = {"file": f, "kernel_imports": s["kernel_imports"]}
        if r is None:
            rec["status"] = "not_run"
            files.append(rec)
            continue
        x = parse_xml(Path(r["xml"]))
        rec.update({"exit_code": r["rc"], "wall_seconds": r["wall_s"], "timed_out": r["timed_out"]})
        if x is None:
            tail = Path(r["log"]).read_text(errors="replace").strip().splitlines()[-3:] if Path(r["log"]).exists() else []
            rec.update({"status": "timeout" if r["timed_out"] else "no_report", "log_tail": tail})
        else:
            rec.update({k: x[k] for k in ("passed", "failures", "errors", "skipped", "tests")})
            rec["pytest_seconds"] = round(x["time"], 2)
            rec["status"] = "pass" if r["rc"] in (0, 5) and x["failures"] == 0 and x["errors"] == 0 else "fail"
            if x["first_failure"]:
                rec["first_failure"] = x["first_failure"]
                rec["failing_tests"] = [t["test"] for t in x["failing_tests"]]
        rr = rerun.get(f)
        if rr is not None:
            xr = parse_xml(Path(rr["xml"]))
            rec["rerun"] = {"exit_code": rr["rc"], "wall_seconds": rr["wall_s"],
                            **({k: xr[k] for k in ("passed", "failures", "errors", "skipped")} if xr else {}),
                            "failing_tests": [t["test"] for t in xr["failing_tests"]] if xr else None}
        files.append(rec)
    Path(sys.argv[3]).write_text(json.dumps({"files": files}, ensure_ascii=False, indent=1))
    agg = {k: sum(f.get(k, 0) for f in files) for k in ("passed", "failures", "errors", "skipped", "tests")}
    print("files", len(files), agg, "status counts",
          {s: sum(1 for f in files if f["status"] == s) for s in {f["status"] for f in files}})
    print("sum wall", round(sum(f.get("wall_seconds", 0) for f in files), 1))


if __name__ == "__main__":
    main()
