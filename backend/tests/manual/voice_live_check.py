"""Manual live check: the real Bailian realtime model through the production bridge (incurs API costs).

    cd backend && .venv/bin/python tests/manual/voice_live_check.py
    # For Audio 3.1: add --model qwen-audio-3.1-realtime-plus

Not collected by pytest (no test_ prefix). The personal assistant is a canned
stand-in: it publishes two real progress steps on the bus (tasks.list, then
history.read) and answers after ASSISTANT_SECONDS, longer than LATE_AFTER, so
a progress reply is exercised. ``tasks_overview`` is a canned direct read.
Speech comes from macOS ``say``, streamed in 100 ms frames like a client. The
key is read from backend/.env and never printed. Prints the event sequence,
what the front desk said, and the latencies the design relies on.

Scenario: free greeting → "make the snake project's colours better" (work
for the assistant: immediate acknowledgement, progress with the real step,
the result told once in the front desk's own words) → "what tasks are
running" (a direct read, no assistant) → "嗯，就是" (half a sentence: never
handed over) → a forced move to a fresh provider session (the memo comes
from the configured small model) → "what did we just talk about" (continuity).
"""
import asyncio
import argparse
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

from dotenv import dotenv_values  # noqa: E402

from core.config import VoiceConfig  # noqa: E402
from voice import tools  # noqa: E402
from voice import upkeep  # noqa: E402
from voice.bridge import Bridge  # noqa: E402
from voice.progress import Progress  # noqa: E402
from voice.prompt import FrontFacts, front_instructions, local_now  # noqa: E402
from voice.provider import RealtimeProvider  # noqa: E402
from voice.models import AUDIO_MODEL, OMNI_MODEL  # noqa: E402
from voice.meter import call_prices  # noqa: E402

UTTERANCES = [("ask", "帮我让贪吃蛇项目再优化一下界面的配色"), ("read", "我现在有哪些任务在进行"),
              ("fragment", "嗯，就是"), ("recall", "我们刚才都聊了些什么？")]
REPLY = ("已经在「贪吃蛇」里安排了配色优化，任务正在跑；方案是「暗色」，暂时不用「亮色」。"
         "另外上次的收尾自检一切正常。")
TASKS = {"status": "ok", "more": False, "tasks": [
    {"title": "贪吃蛇收尾自检", "project": "贪吃蛇", "state": "已完成", "latest": "收尾自检做完了，一切正常。"},
    {"title": "五子棋开发", "project": "五子棋", "state": "进行中"}]}
FACTS = FrontFacts(profile="用户叫 Andrew", last_call="今天 19:20，聊了贪吃蛇项目的进展，用户想优化 UI")
ASSISTANT_SECONDS, LATE_AFTER = 18.0, 8.0
FRAME = 3200  # 100 ms of 16 kHz PCM16
SCOPE = tools.CallScope(user_id="live-user", workspace_id="live-ws", main_session_id="live-main", call_id="live")
T0 = time.monotonic()
marks: dict[str, float] = {}
said: list[tuple[str, str]] = []
calls: list[tuple[float, str, str]] = []


def stamp() -> float:
    return time.monotonic() - T0


def show(text: str) -> None:
    print(f"{stamp():6.2f}s  {text}", flush=True)


def mark(name: str) -> None:
    marks.setdefault(name, stamp())


class CannedLink:
    """The personal assistant: two real bus steps, then an answer after a fixed delay."""
    closed = False

    async def start(self, ref):
        ref.inbox_id = "inbox-live"

    async def wait(self, ref, *, elapsed=0.0, on_message=None):
        from bus import bus
        await asyncio.sleep(1.5)
        bus.publish("tool.running", {"userId": "live-user", "sessionId": "live-main", "tool": "tasks.list"})
        await asyncio.sleep(4)
        bus.publish("tool.running", {"userId": "live-user", "sessionId": "live-main", "tool": "history.read"})
        await asyncio.sleep(ASSISTANT_SECONDS - 5.5)
        return {"status": "ok", "speech": REPLY}

    async def record(self, ref, **fields):
        pass

    async def done(self, ref, outcome, *, delivered=False):
        show(f"turn outcome: {outcome}")


class TimedProvider(RealtimeProvider):
    """The real adapter, with every command and event time-stamped for the report."""
    kinds: dict[str, str] = {}
    pending_kind = "model"

    async def create_response(self, instructions=None):
        kind = ("greeting" if instructions and "电话刚接通" in instructions else
                "delivery" if instructions and "个人助理的结果到了" in instructions else
                "progress" if instructions and "还在等它做什么" in instructions else
                "followup" if instructions is None else "other")
        self.pending_kind = kind
        mark(f"{kind}_request")
        show(f"→ response.create ({kind})")
        await super().create_response(instructions)

    async def send_tool_output(self, call_id, payload):
        mark(f"output_{payload.get('status')}")
        show(f"→ function_call_output {payload}")
        await super().send_tool_output(call_id, payload)

    async def create_note(self, text):
        mark("note")
        show(f"→ note: {text}")
        await super().create_note(text)

    async def update_instructions(self, instructions):
        show("→ session.update instructions: …" + instructions[-80:].replace("\n", " | "))
        await super().update_instructions(instructions)

    async def events(self):
        audio_counts: dict[str, int] = {}
        async for event in super().events():
            if event.kind == "response_started":
                self.kinds[event.response_id] = self.pending_kind
                self.pending_kind = "model"
                show(f"← response.created ({self.kinds[event.response_id]})")
            elif event.kind == "audio":
                kind = self.kinds.get(event.response_id, "model")
                if not audio_counts.get(event.response_id):
                    mark(f"{kind}_first_audio")
                    for label, _ in UTTERANCES:
                        if f"{label}_end" in marks and f"{label}_answer_audio" not in marks:
                            mark(f"{label}_answer_audio")
                    show(f"← first audio ({kind})")
                audio_counts[event.response_id] = audio_counts.get(event.response_id, 0) + len(event.audio)
            elif event.kind == "assistant_transcript":
                said.append((self.kinds.get(event.response_id, "model"), event.text))
                show(f"← said ({self.kinds.get(event.response_id, 'model')}): {event.text}")
            elif event.kind == "response_done":
                show(f"← response.done status={event.status} audio={audio_counts.get(event.response_id, 0) / 48000:.1f}s")
            elif event.kind == "tool_call":
                calls.append((stamp(), event.name, event.arguments))
                show(f"← function_call {event.name} {event.arguments}")
            elif event.kind == "user_transcript":
                show(f"← user transcript: {event.text}")
            elif event.kind == "provider_error":
                show(f"← error code={event.code} reason={event.reason}")
            elif event.kind not in ("item_created", "item_deleted"):
                show(f"← {event.kind}" + (" (invalid)" if event.invalid else ""))
            yield event


def synthesize(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "utterance.wav"
        subprocess.run(["say", "-v", "Tingting", "--data-format=LEI16@16000", "--file-format=WAVE", "-o",
                        str(path), text], check=True)
        with wave.open(str(path), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
            return audio.readframes(audio.getnframes())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=[OMNI_MODEL, AUDIO_MODEL], default=OMNI_MODEL)
    parser.add_argument("--voice", default="Tina")
    args = parser.parse_args()
    env = dotenv_values(BACKEND / ".env", interpolate=False)
    key = env.get("DASHSCOPE_API_KEY") or ""
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is not set in backend/.env")
    for name in ("OPENBOX_API_KEY", "OPENBOX_BASE_URL", "OPENBOX_CONFIG"):  # summary model, as main.py loads it
        os.environ.setdefault(name, env.get(name) or "")

    async def canned_overview(scope, arguments):
        return TASKS
    tools.DIRECT["tasks_overview"] = tools.DirectTool(tools.DIRECT["tasks_overview"].description, canned_overview)
    speech = {label: synthesize(text) for label, text in UTTERANCES}
    config = VoiceConfig(enabled=True, api_key=key, model=args.model, voice=args.voice)
    prices = call_prices(config.model)
    provider = TimedProvider(config)
    started = time.monotonic()
    await provider.open()
    opened = time.monotonic()
    instructions = front_instructions(FACTS, "zh", local_now())
    await provider.configure(instructions)
    connected = time.monotonic()
    show(f"connected: open {opened - started:.2f}s + session.update {connected - opened:.2f}s")
    progress = Progress(user_id="live-user", main_session_id="live-main")
    progress.start()
    async def opener(text):
        fresh = TimedProvider(config)
        await fresh.open()
        await fresh.configure(text)
        show("fresh provider session configured")
        return fresh
    bridge = Bridge(provider, CannedLink(), lang="zh", late_after=LATE_AFTER, scope=SCOPE, progress=progress,
                    instructions=instructions, opener=opener, rates=prices.rates, price_date=prices.date)
    queue: asyncio.Queue = asyncio.Queue()

    async def client():
        """A client microphone: zero frames, an utterance when queued, then zero frames again."""
        silence = bytes(FRAME)
        while True:
            if not queue.empty():
                label = await queue.get()
                pcm = speech[label]
                for offset in range(0, len(pcm), FRAME):
                    await bridge.feed_audio(pcm[offset:offset + FRAME].ljust(FRAME, b"\0"))
                    await asyncio.sleep(0.1)
                mark(f"{label}_end")
                show(f"user speech ended ({label})")
            await bridge.feed_audio(silence)
            await asyncio.sleep(0.1)

    async def pump():
        while True:  # like the socket: follow the bridge to a fresh session
            current = bridge.provider
            async for event in current.events():
                if current is not bridge.provider:
                    break
                await bridge.on_provider_event(event)
            if current is bridge.provider:
                return

    async def outbox():
        while True:
            item = await bridge.outbox.get()
            if isinstance(item, dict) and item["type"] not in {"cost"}:
                details = {key: value for key, value in item.items() if key in {"value", "working", "late", "state", "key"}}
                show(f"⇒ client {item['type']} {details}")

    async def timers():
        while True:
            await asyncio.sleep(1)
            await bridge.fill_idle()

    tasks = [asyncio.create_task(job()) for job in (client, pump, outbox, timers)]
    await bridge.start()
    await _until(lambda: "greeting_first_audio" in marks and bridge.state == "idle" and not bridge.greeting, 15)
    await asyncio.sleep(1.0)
    await queue.put("ask")
    await _until(lambda: "delivery_request" in marks and bridge.state == "idle" and not bridge.deliveries
                 and bridge.playback_left() == 0, 90)
    await asyncio.sleep(1.5)
    await queue.put("read")
    await _until(lambda: "read_end" in marks and "followup_first_audio" in marks and bridge.state == "idle"
                 and bridge.playback_left() == 0, 40)
    await asyncio.sleep(1.5)
    await queue.put("fragment")
    await _until(lambda: "fragment_end" in marks, 20)
    await asyncio.sleep(8)
    bridge.keeper.session_started -= upkeep.ROTATE_SECONDS  # as if 25 minutes had passed
    mark("rotation_due")
    await _until(lambda: bridge.provider is not provider, 40)
    mark("rotated")
    show(f"rotated; memo: {bridge.keeper.summary}")
    await asyncio.sleep(1.0)
    await queue.put("recall")
    await _until(lambda: "recall_end" in marks and "recall_answer_audio" in marks and bridge.state == "idle"
                 and bridge.playback_left() == 0, 40)
    await asyncio.sleep(1.5)
    await bridge.stop()
    await bridge.close()
    for task in tasks:
        task.cancel()
    for task, result in zip(tasks, await asyncio.gather(*tasks, return_exceptions=True)):
        if isinstance(result, Exception):  # a broken pump must not pass as a quiet run
            raise SystemExit(f"{task.get_coro().__name__} failed: {type(result).__name__}: {result}")
    await bridge.provider.close()
    await provider.close()
    bridge.meter.finish()
    report(connected - started, bridge.meter.snapshot())


async def _until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting; marks so far: {marks}")
        await asyncio.sleep(0.05)


def report(connect_seconds: float, cost: dict) -> None:
    def between(start, end):
        return f"{marks[end] - marks[start]:.2f}s" if start in marks and end in marks else "n/a"
    texts = {kind: [text for said_kind, text in said if said_kind == kind] for kind in
             ("greeting", "progress", "delivery", "followup", "model")}
    asks = [(at, name, arguments) for at, name, arguments in calls]
    fragment_asks = [entry for entry in asks if entry[1] == "assistant_ask" and entry[0] > marks.get("fragment_end", 1e9)]
    print("\nWhat the front desk said")
    for kind, values in texts.items():
        for value in values:
            print(f"  [{kind}] {value}")
    print("\nLatencies")
    print(f"  provider connect (open + session.updated)      {connect_seconds:.2f}s")
    print(f"  ready → greeting first audio                   {between('greeting_request', 'greeting_first_audio')}")
    print(f"  ask: speech end → first acknowledgement audio  {between('ask_end', 'ask_answer_audio')}")
    print(f"  ask: speech end → accepted output              {between('ask_end', 'output_accepted')}")
    print(f"  ask: first progress request (late {LATE_AFTER:.0f}s)      {between('ask_end', 'progress_request')}")
    print(f"  progress request → first audio                 {between('progress_request', 'progress_first_audio')}")
    print(f"  result (assistant {ASSISTANT_SECONDS:.0f}s) note → delivery audio  {between('note', 'delivery_first_audio')}")
    print(f"  read: speech end → first audio (direct read)   {between('read_end', 'read_answer_audio')}")
    print(f"  read: speech end → follow-up answer audio      {between('read_end', 'followup_first_audio')}")
    print(f"  rotation due → fresh session in use            {between('rotation_due', 'rotated')}")
    print(f"  recall (fresh session): speech end → audio     {between('recall_end', 'recall_answer_audio')}")
    delivery = " ".join(texts["delivery"])
    print("\nChecks")
    print(f"  greeting mentions time/name/last call: {any(word in ' '.join(texts['greeting']) for word in ('晚上', '下午', '上午', 'Andrew', '贪吃蛇', 'UI'))}")
    print(f"  progress replies: {len(texts['progress'])}; mention the real step: "
          f"{any(word in ' '.join(texts['progress']) for word in ('任务', '记录', '对话'))}")
    print(f"  delivery opens with 我这边查到了: {delivery.startswith('我这边查到了')}; keeps the facts "
          f"(贪吃蛇/配色/暗色): {all(word in delivery for word in ('贪吃蛇', '配色', '暗色'))}; "
          f"deliveries: {len(texts['delivery'])}")
    print(f"  tool calls: {[name for _, name, _ in asks]}; direct read used: "
          f"{any(name == 'tasks_overview' for _, name, _ in asks)}; assistant_ask after the fragment: {len(fragment_asks)}")
    recall = [text for kind, text in said if kind == "model"][-1:]
    print(f"  after the move, recall mentions the call: {bool(recall) and any(word in recall[0] for word in ('贪吃蛇', '配色', '任务'))}"
          f" — {recall[0] if recall else ''}")
    print(f"Cost: {cost['total_yuan']} yuan, tokens {cost['tokens']}, settled {cost['settled_rounds']}, "
          f"unreported {cost['unreported_rounds']}")


if __name__ == "__main__":
    asyncio.run(main())
