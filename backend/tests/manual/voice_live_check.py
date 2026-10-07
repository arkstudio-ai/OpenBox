"""Manual live check: the real Bailian realtime model through the production bridge (costs a few fen).

    cd backend && .venv/bin/python tests/manual/voice_live_check.py

Not collected by pytest (no test_ prefix). The personal assistant is a canned
stand-in that answers after ASSISTANT_SECONDS, longer than LATE_AFTER, so the
"still working" phrase is exercised too. Speech comes from macOS ``say``,
streamed in 100 ms frames like a client. The key is read from backend/.env and
never printed. Prints the event sequence and the latencies the design relies on.
"""
import asyncio
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
from voice.bridge import Bridge  # noqa: E402
from voice.provider import RealtimeProvider  # noqa: E402

UTTERANCE = "你好，帮我看一下贪吃蛇项目进行得怎么样了"
REPLY = "「贪吃蛇」的收尾自检昨晚做完了，一切正常，这次没有改动文件；「配色」还在等你选一个方案。"
ASSISTANT_SECONDS, LATE_AFTER = 9.0, 4.0
FRAME = 3200  # 100 ms of 16 kHz PCM16
T0 = time.monotonic()
marks: dict[str, float] = {}
said: dict[str, str] = {}


def stamp() -> float:
    return time.monotonic() - T0


def show(text: str) -> None:
    print(f"{stamp():6.2f}s  {text}", flush=True)


def mark(name: str) -> None:
    marks.setdefault(name, stamp())


class CannedLink:
    """The personal assistant, answering after a fixed delay."""
    closed = False

    async def start(self, ref):
        ref.inbox_id = "inbox-live"

    async def wait(self, ref, *, elapsed=0.0, on_message=None):
        await asyncio.sleep(ASSISTANT_SECONDS)
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
        kind = ("delivery" if instructions and "逐字朗读" in instructions else
                "still_working" if instructions and "还在办" in instructions else
                "greeting" if instructions and "嗨，我在" in instructions else "other")
        self.pending_kind = kind
        mark(f"{kind}_request")
        show(f"→ response.create ({kind})")
        await super().create_response(instructions)

    async def send_tool_output(self, call_id, payload):
        show(f"→ function_call_output status={payload.get('status')}")
        await super().send_tool_output(call_id, payload)

    async def cancel_response(self):
        show("→ response.cancel")
        await super().cancel_response()

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
                    if kind == "model" and "ack_first_audio" not in marks and "speech_end" in marks:
                        mark("ack_first_audio")
                    show(f"← first audio ({kind})")
                audio_counts[event.response_id] = audio_counts.get(event.response_id, 0) + len(event.audio)
            elif event.kind == "assistant_transcript":
                said[self.kinds.get(event.response_id, "model")] = event.text
                show(f"← said ({self.kinds.get(event.response_id, 'model')}): {event.text}")
            elif event.kind == "response_done":
                seconds = audio_counts.get(event.response_id, 0) / 48000
                show(f"← response.done status={event.status} audio={seconds:.1f}s")
            elif event.kind == "tool_call":
                mark("tool_call")
                show(f"← function_call {event.name} text={event.text!r}")
            elif event.kind == "user_transcript":
                show(f"← user transcript: {event.text}")
            elif event.kind == "provider_error":
                show(f"← error code={event.code} reason={event.reason}")
            else:
                if event.kind == "user_stopped":
                    mark("speech_stopped")
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
    key = dotenv_values(BACKEND / ".env", interpolate=False).get("DASHSCOPE_API_KEY") or ""
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is not set in backend/.env")
    speech = synthesize(UTTERANCE)
    show(f"utterance {len(speech) / 32000:.1f}s: {UTTERANCE}")
    provider = TimedProvider(VoiceConfig(enabled=True, api_key=key))
    started = time.monotonic()
    await provider.open()
    opened = time.monotonic()
    await provider.configure(_front_prompt())
    connected = time.monotonic()
    show(f"connected: open {opened - started:.2f}s + session.update {connected - opened:.2f}s, attempts {provider.attempts}")
    bridge = Bridge(provider, CannedLink(), lang="zh", late_after=LATE_AFTER)
    mark("ready")
    talk = asyncio.Event()

    async def client():
        """A client microphone: zero frames, the utterance once asked, then zero frames again."""
        silence = bytes(FRAME)
        while True:
            if talk.is_set():
                talk.clear()
                for offset in range(0, len(speech), FRAME):
                    await bridge.feed_audio(speech[offset:offset + FRAME].ljust(FRAME, b"\0"))
                    await asyncio.sleep(0.1)
                mark("speech_end")
                show("user speech ended")
            await bridge.feed_audio(silence)
            await asyncio.sleep(0.1)

    async def pump():
        async for event in provider.events():
            await bridge.on_provider_event(event)

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
    talk.set()
    await _until(lambda: "delivery_request" in marks and bridge.state == "idle" and not bridge.deliveries, 60)
    await asyncio.sleep(1.5)
    await bridge.stop()
    await bridge.close()
    for task in tasks:
        task.cancel()
    for task, result in zip(tasks, await asyncio.gather(*tasks, return_exceptions=True)):
        if isinstance(result, Exception):  # a broken pump must not pass as a quiet run
            raise SystemExit(f"{task.get_coro().__name__} failed: {type(result).__name__}: {result}")
    await provider.close()
    bridge.meter.finish()
    cost = bridge.meter.snapshot()
    report(connected - started, cost)


def _front_prompt() -> str:
    from voice.prompt import front_instructions, local_now
    return front_instructions("", "", "zh", local_now())


async def _until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting; marks so far: {marks}")
        await asyncio.sleep(0.05)


def report(connect_seconds: float, cost: dict) -> None:
    def between(start, end):
        return f"{marks[end] - marks[start]:.2f}s" if start in marks and end in marks else "n/a"
    normalize = lambda text: "".join(char for char in text if char.isalnum())  # noqa: E731
    print("\nLatencies")
    print(f"  provider connect (open + session.updated)   {connect_seconds:.2f}s")
    print(f"  ready → greeting first audio                {between('ready', 'greeting_first_audio')}")
    print(f"  user speech end → first ack audio           {between('speech_end', 'ack_first_audio')}")
    print(f"  user speech end → speech_stopped            {between('speech_end', 'speech_stopped')}")
    print(f"  tool call → still-working request (late {LATE_AFTER:.0f}s)  {between('tool_call', 'still_working_request')}")
    print(f"  still-working request → first audio         {between('still_working_request', 'still_working_first_audio')}")
    print(f"  tool call → delivery request (assistant {ASSISTANT_SECONDS:.0f}s)  {between('tool_call', 'delivery_request')}")
    print(f"  delivery request → first delivery audio     {between('delivery_request', 'delivery_first_audio')}")
    print(f"  greeting verbatim: {normalize(said.get('greeting', '')) == normalize('嗨，我在，你说。')}; "
          f"still-working verbatim: {normalize(said.get('still_working', '')) == normalize('还在办，好了我马上告诉你。')}; "
          f"delivery verbatim: {normalize(said.get('delivery', '')) == normalize('我这边查到了' + REPLY)}")
    print(f"Cost: {cost['total_yuan']} yuan, tokens {cost['tokens']}, settled {cost['settled_rounds']}, "
          f"unreported {cost['unreported_rounds']}")


if __name__ == "__main__":
    asyncio.run(main())
