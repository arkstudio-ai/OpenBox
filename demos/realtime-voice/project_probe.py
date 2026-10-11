"""Live synthetic-audio regression through the project's Provider, Bridge, prompt and card tools.

Only the business store/executor is replaced by a sandbox. No production user,
recording, microphone, task or project is touched. Requires the configured voice
provider key; never prints credentials. Audio and structured traces are retained.
"""
import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
import wave

from dotenv import dotenv_values
from probe_aliyun import AUDIO, OMNI, ROOT, VOICES
from semantic_demo import synthesize
sys.path.insert(0, str(ROOT / "backend"))
from core.config import VoiceConfig
from voice import cards, phrases, tools
from voice.bridge import Bridge
from voice.prompt import FrontFacts, front_instructions
from voice.provider import RealtimeProvider
from tests.support.voice_fakes import FakeLink

CASES = [
    {"id": "quoted_hush", "text": "闭嘴是什么意思？解释一下这个词。", "allowed": []},
    {"id": "style", "text": "语气温柔一些，别这么机械。", "allowed": []},
    {"id": "natural_confirmation", "text": "就按你刚才那个方案办。", "cards": "send", "allowed": ["cards_answer"]},
    {"id": "negative_confirmation", "text": "不用给我解释原理了，消息就按原样发出去。", "cards": "send", "allowed": ["cards_answer"]},
    {"id": "named_project", "text": "不要第一个，第二个就行，项目叫春季上新。", "cards": "placement", "allowed": ["cards_answer"]},
    {"id": "ambiguous_cards", "text": "就按那个来。", "cards": "multiple", "allowed": []},
    {"id": "short_name", "text": "春雨。", "history": "已确定每天早上九点提醒喝水，还没提交。这个提醒叫什么名字？", "allowed": ["assistant_ask"]},
    {"id": "accepted_is_not_done", "text": "帮我整理一份新品宣传计划。", "allowed": ["assistant_ask"]},
]
BOXES = {}


def fixture(case, uid):
    value = {"card_id": uid + ":card", "kind": "assistant_send", "action": "task_send", "header": "发送消息",
        "prompt": "向测试对话发送：样片已收到。", "impact": "收件人会看到消息", "options": ["确认发送", "取消"], "high_risk": True}
    if case.get("cards") == "placement":
        value.update(kind="assistant_placement", header="安排位置", prompt="新品宣传计划放在哪里？",
            options=["在默认空间新建对话", "新建项目", "取消"], high_risk=False, impact=None)
    values = [value] if case.get("cards") else []
    if case.get("cards") == "multiple":
        values.append({**value, "card_id": uid + ":second", "header": "另一条消息", "prompt": "向另一个测试对话发送不同消息"})
    return {"cards": values, "answers": []}


async def waiting(user_id, workspace_id, main_id):
    return [c for c in BOXES[user_id]["cards"] if c["card_id"] not in {a[0] for a in BOXES[user_id]["answers"]}]


async def reply(card_id, answers, user_id="default", **fields):
    BOXES[user_id]["answers"].append((card_id, answers, fields))
    return {"ok": True, "applied": True, "status": "answered"}


async def empty(**kwargs):
    return []


async def before(*args):
    return "sandbox-inbox-before"


class LoggedProvider(RealtimeProvider):
    def __init__(self, config):
        super().__init__(config)
        self.trace, self.pcm = [], defaultdict(bytearray)
        self.started = time.monotonic()
        self.last_done = 0.0
        self.done_count = 0
        self.input_frames = 0

    async def _send_many(self, *events):
        for event in events:
            if event["type"] == "input_audio_buffer.append":
                self.input_frames += 1
            else:
                self.trace.append({"at": round(time.monotonic() - self.started, 3), "direction": "send", "event": event})
        await super()._send_many(*events)

    async def events(self):
        async for event in super().events():
            if event.kind == "audio":
                self.pcm[event.response_id] += event.audio
            else:
                data = asdict(event)
                data.pop("audio", None)
                self.trace.append({"at": round(time.monotonic() - self.started, 3), "direction": "receive", "event": data})
            if event.kind == "response_done":
                self.last_done = time.monotonic()
                self.done_count += 1
            yield event


async def run(case, model, out, env):
    uid = ("audio" if model == AUDIO else "omni") + "_" + case["id"]
    BOXES[uid] = fixture(case, uid)
    config = VoiceConfig(model=model, voice=VOICES[model], api_key=env["DASHSCOPE_API_KEY"],
        workspace_id=env.get("VOICE_WORKSPACE_ID") or "", proxy="none",
        endpoint=env.get("REALTIME_WS_URL") or "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
    provider, link = LoggedProvider(config), FakeLink()
    prompt = front_instructions(FrontFacts(address="测试员"), "zh", datetime.now(timezone.utc))
    bridge = Bridge(provider, link, scope=tools.CallScope(uid, "sandbox", uid, uid), instructions=prompt)
    bridge.greeting = False
    bridge.greeted.set()
    source, pcm = await asyncio.to_thread(synthesize, case["text"], out)
    tasks = []
    failure = None
    try:
        await provider.open()
        await provider.configure(prompt)
        bridge.answered()
        async def pump():
            async for event in provider.events():
                await bridge.on_provider_event(event)
        tasks.append(asyncio.create_task(pump()))
        async def history(role, text):
            await provider._send({"type": "conversation.item.create", "item": {"type": "message", "role": role,
                "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}})
            bridge._said(role, text, "history-" + role)
        if BOXES[uid]["cards"]:
            await history("user", {
                "placement": "帮我整理新品宣传计划。",
                "send": "请帮我向测试对话发送消息：样片已收到。",
                "multiple": "我准备发两条消息，分别给两个测试对话，先都准备好等我决定。",
            }[case["cards"]])
            shown = [cards.spoken_card(c, bridge.desk.show(c)) for c in BOXES[uid]["cards"]]
            await provider.create_note(phrases.card_note("", "卡片已向用户介绍，等待这轮用户作出决定。", shown, "zh"))
            await history("assistant", {
                "send": "即将向测试对话发送“样片已收到”，收件人会看到这条消息。确认发送吗？",
                "placement": "新品宣传计划可以在默认空间新建对话，也可以新建项目。你希望放在哪里？",
                "multiple": "现在有两条待发送消息：一条给测试对话，内容是样片已收到；另一条给另一个测试对话，内容不同。你决定先处理哪一条？",
            }[case["cards"]])
        if case.get("history"):
            await history("user", "每天早上九点提醒我喝水，名字我来取。")
            await history("assistant", case["history"])
        sent = asyncio.Event()
        async def feed():
            # Match a connected microphone: silence precedes speech, then remains continuous.
            stream_pcm = b"\x00" * 9600 + pcm
            for offset in range(0, len(stream_pcm), 3200):
                await bridge.feed_audio(stream_pcm[offset:offset + 3200].ljust(3200, b"\x00"))
                await asyncio.sleep(.1)
            sent.set()
            while True:
                await bridge.feed_audio(b"\x00" * 3200)
                await bridge.fill_idle()
                await asyncio.sleep(.1)
        tasks.append(asyncio.create_task(feed()))
        async with asyncio.timeout(55):
            await sent.wait()
            while not (provider.done_count and time.monotonic() - provider.last_done > 4
                       and not bridge.active_response and not bridge.requested and not bridge._direct):
                if tasks[0].done():
                    tasks[0].result()
                    raise RuntimeError("Provider stream ended")
                await asyncio.sleep(.15)
    except Exception as exc:
        failure = type(exc).__name__ + ": " + str(exc).replace(env["DASHSCOPE_API_KEY"], "[REDACTED]")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bridge.close()
        await provider.close()
    audio_paths = []
    for number, data in enumerate(provider.pcm.values()):
        path = out / f"{uid}-{number}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(data)
        audio_paths.append(path.name)
    calls = [t["event"] for t in provider.trace if t["direction"] == "receive" and t["event"].get("kind") == "tool_call"]
    results = [t["event"] for t in provider.trace if t["direction"] == "send" and t["event"]["type"] == "conversation.item.create"
               and t["event"].get("item", {}).get("type") == "function_call_output"]
    row = {"id": uid, "model": model, "input": case["text"], "input_wav": source.name, "outputs": audio_paths,
           "transcripts": [{"speaker": line.role, "text": line.text} for line in bridge.spoken.lines],
           "calls": calls, "tool_results": results, "answers": BOXES[uid]["answers"],
           "requests": [{"request": ref.text, "human": ref.transcript, "lane": ref.lane} for ref in link.started],
           "error": failure, "input_frames": provider.input_frames}
    names = [c["name"] for c in calls if c["name"] != "cards_pending"]
    row["structural_pass"] = failure is None and sorted(names) == sorted(case["allowed"])
    if case["id"] == "named_project":
        row["structural_pass"] &= bool(row["answers"] and row["answers"][0][2].get("placement_project_name") == "春季上新")
    if case.get("cards") == "send":
        row["structural_pass"] &= bool(row["answers"] and row["answers"][0][1] == [["确认发送"]])
    (out / (uid + ".trace.json")).write_text(json.dumps(provider.trace, ensure_ascii=False, indent=2))
    (out / (uid + ".result.json")).write_text(json.dumps(row, ensure_ascii=False, indent=2))
    print(json.dumps({"id": uid, "structural_pass": row["structural_pass"], "error": failure}, ensure_ascii=False), flush=True)
    return row


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--model", choices=["audio", "omni"], action="append")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    # Both models use the same WAV. Complete generation before concurrent readers open it.
    for case in CASES:
        if not args.case or case["id"] in args.case:
            await asyncio.to_thread(synthesize, case["text"], args.out)
    env = {**dotenv_values(ROOT / "backend/.env", interpolate=False), **os.environ}
    files = [ROOT / "backend" / "voice" / (name + ".py") for name in ("bridge", "turns", "cards", "tools", "prompt", "phrases", "provider")]
    manifest = {"cases": CASES, "tools": tools.schemas(), "source_hashes": {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for file in files:
        (args.out / ("source_" + file.name)).write_text(file.read_text())
    semaphore = asyncio.Semaphore(2)
    async def bounded(case, model):
        async with semaphore:
            return await run(case, model, args.out, env)
    with patch("assistant.confirmations.pending_cards", waiting), patch("assistant.request_answers.list_waiting", empty), patch("question.question.reply", reply), patch("voice.assistant_link.latest_inbox_id", before):
        results = await asyncio.gather(*(bounded(case, model) for case in CASES if not args.case or case["id"] in args.case
                                         for model in (AUDIO, OMNI) if not args.model or ("audio" if model == AUDIO else "omni") in args.model))
    (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
