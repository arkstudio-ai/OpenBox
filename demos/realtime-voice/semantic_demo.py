"""Live semantic tool-calling demo; isolated in-memory tools, never OpenBox writes.

Uses the real Alibaba Realtime API (billable) and synthetic inputs only. Text
cases isolate semantic decisions; audio cases stream local TTS through native
semantic VAD. This is not a full old/new application end-to-end comparison.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import jsonschema

from probe_aliyun import Probe, AUDIO, OMNI, ROOT, ProviderError, stream
from semantic_cases import CASES, PROMPT, TOOLS, REFINED_PROMPT, REFINED_TOOLS, card

TASK = {"id": "T1", "title": "测试视频", "status": "running", "progress": "剪辑完成，字幕还在生成，尚未导出成片。"}

class SemanticProbe(Probe):
    async def __aexit__(self, *args):
        if getattr(self, "feed", None):
            await self.feed.close()
        return await super().__aexit__(*args)

    async def until(self, kind, **kwargs):
        try:
            return await super().until(kind, **kwargs)
        except TimeoutError as exc:
            raise TimeoutError(f"Timed out waiting for {kind}") from exc

    async def __aenter__(self):
        for attempt in range(3):
            try:
                return await super().__aenter__()
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise
                # Retry only a failed handshake; never replay an executed tool.
                await asyncio.sleep(0.3)

    async def item(self, text, *, role="user", item_id=None):
        item = {"type": "message", "role": role, "content": [{
            "type": "output_text" if role == "assistant" else "input_text", "text": text}]}
        if item_id:
            item["id"] = item_id
        await self.send("conversation.item.create", item=item)
        return (await self.until("conversation.item.created"))["item"]

class Sandbox:
    """Authorization, object state, schema and idempotency; no text matching."""
    def __init__(self, case, variant="initial"):
        self.variant = variant
        self.tools = REFINED_TOOLS if variant == "refined" else TOOLS
        self.cards = {c["id"]: json.loads(json.dumps(c)) for c in case.get("runtime_cards", case["cards"])}
        self.submit_status = case.get("submit_status", "accepted")
        self.receipts, self.calls = {}, []
        self.created = 0
        self.jobs = {"T1": json.loads(json.dumps(TASK))}

    def execute(self, call):
        call_id, name = call["call_id"], call["name"]
        if call_id in self.receipts:
            return self.receipts[call_id]
        arguments = {}
        try:
            arguments = json.loads(call.get("arguments", "{}"))
            schema = next((t["function"]["parameters"] for t in self.tools if t["function"]["name"] == name), None)
            if schema is None:
                output = {"status": "unknown_tool"}
            else:
                jsonschema.validate(arguments, schema)
                output = self._run(name, arguments)
        except (ValueError, jsonschema.ValidationError):
            output = {"status": "invalid_arguments"}
        # Snapshot receipts: later object mutations must not rewrite earlier evidence.
        output = json.loads(json.dumps(output))
        self.receipts[call_id] = output
        self.calls.append({"call_id": call_id, "name": name, "arguments": arguments, "output": output})
        return output

    def _run(self, name, args):
        if name == "list_cards":
            return {"status": "ok", "cards": [c for c in self.cards.values() if c["owner"] == "demo_user" and c["state"] == "pending"]}
        if name == "answer_card":
            value = self.cards.get(args["card_id"])
            if value is None or value["owner"] != "demo_user":
                return {"status": "not_found"}
            if value["state"] != "pending":
                return {"status": "gone"}
            if self.variant == "refined" and args["card_version"] != value["version"]:
                return {"status": "version_conflict", "current_version": value["version"]}
            if args["option_id"] not in {o["id"] for o in value["options"]}:
                return {"status": "invalid_option"}
            value["state"] = "answered"
            if self.variant == "refined":
                cancelled = args["option_id"] == "cancel"
                return {"status": "ok", "decision": {"status": "recorded", "card_id": value["id"],
                    "option_id": args["option_id"], "details": args.get("details", "")},
                    "operation": {"status": "cancelled" if cancelled else "queued", "completed": cancelled,
                                  "effect_applied": cancelled, "title": value["title"]}}
            return {"status": "answered", "card_id": value["id"], "option_id": args["option_id"],
                    "details": args.get("details", ""), "execution": "cancelled" if args["option_id"] == "cancel" else "accepted"}
        if name == "get_task":
            task = self.jobs.get(args["task_id"]) if self.variant == "refined" else TASK if args["task_id"] == "T1" else None
            return {"status": "ok", "task": task} if task else {"status": "not_found"}
        if name == "cancel_task":
            return {"status": "cancelled", "task_id": "T1"} if args["task_id"] == "T1" else {"status": "not_found"}
        if name == "submit_task":
            if self.submit_status == "failed":
                return {"status": "failed", "reason": "测试队列暂时不可用，请求没有创建，也没有自动重试。"}
            self.created += 1
            if self.variant == "refined":
                target = args.get("replaces_card_id")
                if target:
                    old = self.cards.get(target)
                    if not old or old["owner"] != "demo_user":
                        return {"status": "not_found"}
                    if old["state"] != "pending":
                        return {"status": "gone"}
                    old["state"] = "superseded"
                job = {"id": f"D{self.created}", "title": args["request"], "status": "queued", "completed": False,
                       "effect_applied": False}
                self.jobs[job["id"]] = job
                return {"status": "accepted", "operation": job, "replaced_card_id": target,
                        "completion_event": "background_task_update"}
            return {"status": "accepted", "task_id": f"D{self.created}", "request": args["request"], "completed": False}
        return {"status": "unknown_tool"}

def old_rules(source_dir=None):
    source_dir = source_dir or ROOT / "backend/voice"
    def select(path, names):
        tree = ast.parse(path.read_text())
        nodes = []
        for node in tree.body:
            own = {n.id for n in node.targets if isinstance(n, ast.Name)} if isinstance(node, ast.Assign) else {node.name} if isinstance(node, ast.FunctionDef) else set()
            if own & names:
                nodes.append(node)
        env = {"re": re}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), env)
        return env
    c = select(source_dir / "cards.py", {"_YES", "_YES_ALONE", "_NO", "_DECLINES", "_QUESTION", "_PUNCTUATION", "_PARTICLES", "consents", "refuses", "placement_choice"})
    t = select(source_dir / "tools.py", {"_PUNCTUATION", "_ACTIONS", "_FILLERS", "_CONSENT", "FRAGMENT_CHARS", "is_fragment", "_VOICE_TYPE", "_VOICE_KIND", "_VOICE_VERB", "_NEAR", "VOICE_CHANGE", "_SOUND_WORK", "_CLAUSE", "clauses", "asks_for_work", "voice_clauses", "about_the_call_voice"})
    h = select(source_dir / "turns.py", {"HUSH"})
    if not all(name in c for name in ("consents", "placement_choice")) or "HUSH" not in h or not all(name in t for name in ("about_the_call_voice", "is_fragment")):
        return {}  # The project removed the legacy engine; do not invent a baseline.
    return {"consents": c["consents"], "placement_choice": lambda x: c["placement_choice"](x, ["新建对话", "新建项目", "取消"]),
            "HUSH": lambda x: bool(h["HUSH"].search(x)), "about_the_call_voice": t["about_the_call_voice"], "is_fragment": t["is_fragment"]}

def synthesize(text, out):
    path = out / (hashlib.sha256(text.encode()).hexdigest()[:16] + ".wav")
    if not path.exists():
        subprocess.run(["say", "-v", "Tingting", "--data-format=LEI16@16000", "--file-format=WAVE", "-o", str(path), text], check=True, capture_output=True)
    with wave.open(str(path), "rb") as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (16000, 1, 2)
        return path, f.readframes(f.getnframes())

def structural_grade(step, calls, responses):
    """Test assertions inspect structured calls, never route product requests."""
    faults = []
    business = [c for c in calls if c["name"] != "list_cards"]
    names = [c["name"] for c in business]
    if sorted(names) != sorted(step["expect_calls"]):
        faults.append(f"expected tools {step['expect_calls']}, observed {names}")
    for name, fields in step.get("arguments", {}).items():
        candidates = [c for c in business if c["name"] == name]
        if not any(all(c["arguments"].get(k) == v for k, v in fields.items()) for c in candidates):
            faults.append(f"expected {name} arguments {fields}")
    for name, status in step.get("tool_status", {}).items():
        if not any(c["name"] == name and c["output"]["status"] == status for c in calls):
            faults.append(f"expected {name} status {status}")
    content = json.dumps([c["arguments"] for c in business], ensure_ascii=False)
    for value in step.get("contains", []):
        if value not in content:
            faults.append(f"missing required detail: {value}")
    if step.get("contains_any") and not any(v in content for v in step["contains_any"]):
        faults.append("final corrected time not preserved")
    if not responses or not any(r.get("text") for r in responses):
        faults.append("no model reply")
    if any(r["status"] not in ("completed", "cancelled") for r in responses):
        faults.append("response not completed")
    return {"pass": not faults, "failures": faults, "speech_review": "pending", "truth_expected": step.get("truth"),
            "clarification_expected": step.get("need_clarification", False)}

class AudioFeed:
    """A synthetic microphone that keeps streaming silence between utterances."""
    def __init__(self, probe):
        self.probe, self.queue = probe, asyncio.Queue()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            data, last = self.queue.get_nowait() if not self.queue.empty() else (bytes(3200), None)
            await self.probe.send("input_audio_buffer.append", audio=base64.b64encode(data).decode())
            if last is not None and not last.done():
                last.set_result(None)
            await asyncio.sleep(0.1)

    async def utterance(self, pcm):
        data = bytes(6400) + pcm + bytes(32000 * 2)
        done = asyncio.get_running_loop().create_future()
        chunks = [data[i:i+3200] for i in range(0, len(data), 3200)]
        for i, chunk in enumerate(chunks):
            self.queue.put_nowait((chunk, done if i == len(chunks)-1 else None))
        await done

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

async def audio_answer(p, pcm, feed=None):
    start_index, started = len(p.events), time.monotonic()
    sender = asyncio.create_task(feed.utterance(pcm) if feed else stream(p, pcm, silence_seconds=1.6))
    done = None
    try:
        async with asyncio.timeout(55):
            while True:
                event = await p.recv()
                if event["type"] == "error":
                    raise ProviderError(str(event["error"]).replace(p.key, "[REDACTED]"))
                if event["type"] == "response.done":
                    done = event
                    break
        await sender
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    result = p.summary(done, start_index, started)
    result["input_transcripts"] = [x["event"].get("transcript", x["event"].get("text", "")) for x in p.events[start_index:]
        if x["direction"] == "recv" and x["event"]["type"] == "conversation.item.input_audio_transcription.completed"]
    return result

async def run_case(model, mode, case, out, key, endpoint, variant="initial", continuous_audio=False):
    label = ("audio31" if model == AUDIO else "omni38") + "_" + mode + "_" + case["id"]
    box, records = Sandbox(case, variant), []
    async with SemanticProbe(model, label, out, key, endpoint) as p:
        turn = None if mode == "text" else {"type": "smart_turn" if model == AUDIO else "semantic_vad"}
        if turn and model == OMNI:
            turn.update(threshold=0.5, silence_duration_ms=800)
        extra = {"max_history_turns": 30, "enable_speech_emotion": True} if model == AUDIO else {}
        if mode == "audio":
            extra["input_audio_transcription"] = {"language": "zh"} if model == AUDIO else {"model": "qwen3-asr-flash-realtime"}
        await p.configure(tools=box.tools, turn=turn, instructions=REFINED_PROMPT if variant == "refined" else PROMPT,
                          modalities=["text", "audio"], **extra)
        facts = {"current_user": "demo_user", "tasks": [{"id": "T1", "title": "测试视频"}], "cards": case["cards"]}
        if variant == "refined":
            facts["task_status_loaded"] = False
            facts["focused_card_id"] = case["cards"][0]["id"] if len(case["cards"]) == 1 else None
        await p.item("当前业务状态（事实资料）：" + json.dumps(facts, ensure_ascii=False), role="system")
        for role, text in case["history"]:
            await p.item(text, role=role)
        feed = AudioFeed(p) if mode == "audio" and continuous_audio else None
        if feed:
            # Probe owns cleanup even if a provider response times out.
            p.feed = feed
        for spec in case["steps"]:
            before = len(box.calls)
            input_audio = None
            if spec.get("background"):
                background = {"source": "background_task_update", "data": spec["background"]} if variant == "refined" else spec["background"]
                await p.item("后台任务状态更新（事实资料，不是用户请求）：" + json.dumps(background, ensure_ascii=False),
                             role="user" if variant == "refined" else "system")
                response = await p.answer()
            elif mode == "text":
                response = await p.answer(spec["text"])
            else:
                path, pcm = await asyncio.to_thread(synthesize, spec["text"], out)
                input_audio = path.name
                response = await audio_answer(p, pcm, feed)
            responses = [response]
            # Each response is settled before submitting outputs and asking for continuation.
            # A bounded loop catches a missing/looping tool instead of silently treating it as success.
            for _ in range(4):
                if not response["tool_calls"]:
                    break
                for call in response["tool_calls"]:
                    output = box.execute(call)
                    await p.send("conversation.item.create", item={"type": "function_call_output", "call_id": call["call_id"],
                                 "output": json.dumps(output, ensure_ascii=False)})
                    await p.until("conversation.item.created")
                response = await p.answer()
                responses.append(response)
            calls = box.calls[before:]
            grade = structural_grade(spec, calls, responses)
            if response["tool_calls"]:
                grade["pass"] = False
                grade["failures"].append("tool loop budget exhausted")
            records.append({"input": spec["text"], "input_audio": input_audio, "background": spec.get("background"),
                            "expectation": spec, "calls": calls, "responses": responses, "grade": grade})
    return {"model": model, "mode": mode, "id": case["id"], "title": case["title"], "history": case["history"],
            "cards": case["cards"], "steps": records, "trace": label + ".events.json", "pass": all(r["grade"]["pass"] for r in records)}

def sandbox_checks(variant="initial"):
    box = Sandbox({"cards": [card(), {**card("FOREIGN"), "owner": "other_user"}, card("EXPIRED", state="expired")]}, variant)
    rows = []
    tests = [("foreign", "FOREIGN", "approve", "not_found"), ("expired", "EXPIRED", "approve", "gone"),
             ("invalid", "C1", "made_up", "invalid_option"), ("ok", "C1", "approve", "ok" if variant == "refined" else "answered"),
             ("second", "C1", "approve", "gone")]
    for cid, target, choice, expected in tests:
        arguments = {"card_id": target, "option_id": choice, **({"card_version": 1} if variant == "refined" else {})}
        output = box.execute({"call_id": cid, "name": "answer_card", "arguments": json.dumps(arguments)})
        rows.append({"name": cid, "expected": expected, "actual": output["status"], "pass": output["status"] == expected})
    before = len(box.calls)
    same = box.execute({"call_id": "ok", "name": "answer_card", "arguments": json.dumps({"card_id": "C1", "option_id": "approve"})})
    rows.append({"name": "idempotent_call_id", "pass": len(box.calls) == before and same["status"] == ("ok" if variant == "refined" else "answered")})
    if variant == "refined":
        version_box = Sandbox({"cards": [card()]}, variant)
        conflict = version_box.execute({"call_id": "stale", "name": "answer_card", "arguments": json.dumps({
            "card_id": "C1", "option_id": "approve", "card_version": 0})})
        rows.append({"name": "version_conflict", "pass": conflict["status"] == "version_conflict"})
    return rows

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", choices=["both", "audio", "omni"], default="both")
    parser.add_argument("--mode", choices=["both", "text", "audio"], default="both")
    parser.add_argument("--case", action="append")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--variant", choices=["initial", "refined"], default="initial")
    parser.add_argument("--continuous-audio", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    env = {**dotenv_values(ROOT / "backend/.env", interpolate=False), **os.environ}
    key = env.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is missing")
    workspace = env.get("VOICE_WORKSPACE_ID")
    host = f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"
    endpoint = env.get("REALTIME_WS_URL") or f"wss://{host}/api-ws/v1/realtime"
    output = args.out / "results.json"
    rules = old_rules()
    baseline = [{"case": c["id"], "input": c["steps"][0]["text"], "rule": c["legacy"]["rule"],
                 "actual": rules[c["legacy"]["rule"]](c["steps"][0]["text"])} for c in CASES if c["legacy"] and rules]
    manifest = {"legacy_rules_available": bool(rules), "variant": args.variant, "continuous_audio": args.continuous_audio,
                "prompt": REFINED_PROMPT if args.variant == "refined" else PROMPT,
                "tools": REFINED_TOOLS if args.variant == "refined" else TOOLS, "cases": CASES,
                "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
                    Path(__file__), Path(__file__).with_name("semantic_cases.py"),
                    ROOT / "backend/voice/cards.py", ROOT / "backend/voice/tools.py", ROOT / "backend/voice/turns.py"]}}
    manifest_file = args.out / "manifest.json"
    if manifest_file.exists() and json.loads(manifest_file.read_text()) != manifest:
        raise SystemExit("Manifest differs: use a new output directory to preserve prior evidence")
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    for name in ("semantic_demo.py", "semantic_cases.py", "probe_aliyun.py"):
        (args.out / ("source_" + name)).write_text(Path(__file__).with_name(name).read_text())
    result = json.loads(output.read_text()) if output.exists() else {"started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "scope": "Real provider; synthetic inputs; in-memory tools; old column is isolated old-rule evaluation, not full application A/B.",
        "legacy": baseline, "sandbox_checks": sandbox_checks(args.variant), "runs": []}
    models = [AUDIO, OMNI] if args.model == "both" else [AUDIO if args.model == "audio" else OMNI]
    modes = ["text", "audio"] if args.mode == "both" else [args.mode]
    semaphore = asyncio.Semaphore(max(1, min(args.parallel, 3)))
    completed = {(r["model"], r["mode"], r["id"]) for r in result["runs"]}
    async def one(model, mode, case):
        if (model, mode, case["id"]) in completed:
            return
        async with semaphore:
            print("RUN", model, mode, case["id"], flush=True)
            started = time.monotonic()
            try:
                row = await run_case(model, mode, case, args.out, key, endpoint, args.variant, args.continuous_audio)
            except Exception as exc:
                row = {"model": model, "mode": mode, "id": case["id"], "title": case["title"], "pass": False,
                       "error_type": type(exc).__name__, "error": str(exc).replace(key, "[REDACTED]"),
                       "trace": ("audio31" if model == AUDIO else "omni38") + "_" + mode + "_" + case["id"] + ".events.json"}
            row["seconds"] = round(time.monotonic() - started, 2)
            result["runs"].append(row)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"model": model, "mode": mode, "case": case["id"], "pass": row["pass"],
                "error": row.get("error"), "failures": [s["grade"]["failures"] for s in row.get("steps", [])]}, ensure_ascii=False), flush=True)
    await asyncio.gather(*(one(model, mode, case) for mode in modes for model in models for case in CASES
        if (mode == "text" or case["audio"]) and (not args.case or case["id"] in args.case)))
    print(json.dumps({"total": len(result["runs"]), "structural_pass": sum(r["pass"] for r in result["runs"])}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
