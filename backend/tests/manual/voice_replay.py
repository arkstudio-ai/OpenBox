"""Replay real user utterances into a live call and measure how the front desk talks.

    backend/.venv/bin/python backend/tests/manual/voice_replay.py \\
        [--base http://127.0.0.1:8081] [--utterances .local-dev/voice-qa/replay_utterances.txt] \\
        [--log .local-dev/assistant-backend.log] [--with-actions] [--out report.json]

docs/ASSISTANT_VOICE_FIX_PLAN.md §5.7. Logs in as the QA account in tem.md
(read here, never printed), opens one call and plays each line (one per line
in the file) synthesized with macOS ``say -v Tingting`` as 100 ms frames, like
a microphone, waiting until the call is quiet in between (or a maximum). Then
it reads the server's debug transcripts of that call from the backend log
(the backend must run with VOICE_DEBUG_TRANSCRIPTS=true) and prints:

- the repeated-sentence ratio among the front desk's lines,
- lines opening with "我这边查到了",
- duplicate deliveries (the same result told more than once),
- tool calls on fragments (utterances under 6 characters),
- each utterance's first-audio latency.

Lines asking for work that changes things (projects, tasks, video, cards,
publishing) are skipped unless --with-actions: on a real account they start
real, paid or public work.
"""
import argparse
import asyncio
import contextlib
import json
import re
import subprocess
import tempfile
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[3]
FRAME = 3200  # 100 ms of 16 kHz mono PCM16
FRAGMENT_CHARS = 6
RESULT_CHARS, SAME_RESULT = 25, 0.6  # a line long enough to carry a result; how alike two tellings are
QUIET_SECONDS, MAX_WAIT_SECONDS, FIRST_AUDIO_SECONDS = 3.0, 45.0, 20.0
ACTION_WORDS = ("优化", "创建", "新建一个项目", "建好了", "继续吧", "生成", "改名", "选择", "发到", "抖音", "制作", "去做")
FOUND = "我这边查到了"
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[openbox\.voice\.bridge\] INFO voice transcript "
                       r"(?:call=(\S*) )?(user|assistant|note) text=(.*)$")
_SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]?")
_NOISE = re.compile(r"[\s，。！？、；：,.!?;:…~～“”\"'‘’（）()「」《》【】—-]+")


def credentials() -> tuple[str, str]:
    text = (ROOT / "tem.md").read_text()
    return re.search(r"账号：\s*(\S+)", text).group(1), re.search(r"密码：\s*(\S+)", text).group(1)


def synthesize(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "line.wav"
        subprocess.run(["say", "-v", "Tingting", "--data-format=LEI16@16000", "--file-format=WAVE", "-o", str(path),
                        text], check=True)
        with wave.open(str(path)) as audio:
            return audio.readframes(audio.getnframes())


def is_action(text: str) -> bool:
    return any(word in text for word in ACTION_WORDS)


def plain(text: str) -> str:
    return _NOISE.sub("", text).lower()


class Call:
    """Client side of one call: a microphone that always sends frames, and every event with its time."""

    def __init__(self, ws):
        self.ws, self.t0 = ws, time.monotonic()
        self.events: list[tuple[float, str, dict]] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.last_audio: float | None = None
        self.pending: set[str] = set()  # turns accepted and not yet told, timed out or failed
        self.ready: dict = {}
        self.closed = asyncio.Event()

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def mic(self):
        silence, pending, label = bytes(FRAME), b"", None
        while not self.closed.is_set():
            if not pending and not self.queue.empty():
                pending, label = await self.queue.get()
            frame, pending = (pending[:FRAME].ljust(FRAME, b"\0"), pending[FRAME:]) if pending else (silence, b"")
            try:
                await self.ws.send(frame)
            except websockets.ConnectionClosed:
                return
            if label is not None and not pending:
                self.events.append((self.now(), "speech_end", {"index": label}))
                label = None
            await asyncio.sleep(0.1)

    async def receive(self):
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    if self.last_audio is None or self.now() - self.last_audio > 0.4:
                        self.events.append((self.now(), "audio", {}))
                    self.last_audio = self.now()
                    continue
                event = json.loads(message)
                kind = event.get("type")
                if kind == "ready":
                    self.ready = event
                if kind == "turn":
                    if event["state"] == "accepted":
                        self.pending.add(event["turn_id"])
                    elif event["state"] in ("delivered", "timeout", "failed"):
                        self.pending.discard(event["turn_id"])
                if kind not in ("heartbeat", "cost"):
                    self.events.append((self.now(), kind, {k: v for k, v in event.items() if k != "type"}))
                if kind == "ended":
                    break
        except websockets.ConnectionClosed:
            pass
        self.closed.set()

    def quiet(self, since: float) -> bool:
        heard = self.last_audio is None or self.now() - self.last_audio >= QUIET_SECONDS
        return heard and self.now() - since >= QUIET_SECONDS

    async def settle(self, since: float, limit: float):
        """Until nothing plays and no turn is pending, at most ``limit`` seconds after ``since``."""
        while self.now() - since < limit and not self.closed.is_set():
            if self.quiet(since) and not self.pending:
                return
            await asyncio.sleep(0.2)


async def run_call(base: str, lines: list[tuple[int, str]]) -> tuple[Call, float, float]:
    user, password = credentials()
    async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as http:
        login = await http.post("/api/auth/login", json={"username": user, "password": password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        (await http.post("/api/assistant/ensure", json={}, headers=headers)).raise_for_status()
        ticket = (await http.post("/api/auth/ticket", json={"audience": "voice"}, headers=headers)).json()["ticket"]
    audio = {index: synthesize(text) for index, text in lines}
    started_wall = time.time()
    url = base.replace("http", "ws", 1) + f"/ws/assistant/voice?ticket={ticket}"
    async with websockets.connect(url, open_timeout=15, max_size=8 << 20, proxy=None) as ws:
        call = Call(ws)
        receiver, mic = asyncio.create_task(call.receive()), asyncio.create_task(call.mic())
        while not call.ready and not call.closed.is_set():
            await asyncio.sleep(0.05)
        print(f"call {call.ready.get('call_id')} max_seconds={call.ready.get('max_seconds')}", flush=True)
        await call.settle(call.now(), 15)  # the greeting
        for index, text in lines:
            if call.closed.is_set():  # the server ended the call (a limit, an error): report what was played
                print(f"{call.now():7.1f}s  call ended by the server; {len(lines)} lines planned", flush=True)
                break
            print(f"{call.now():7.1f}s  #{index} {text}", flush=True)
            await call.queue.put((audio[index], index))
            while not call.closed.is_set() and not any(
                    kind == "speech_end" and data["index"] == index for _, kind, data in call.events):
                await asyncio.sleep(0.05)
            await call.settle(call.now(), MAX_WAIT_SECONDS)
        await call.settle(call.now(), MAX_WAIT_SECONDS)
        if not call.closed.is_set():
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.send(json.dumps({"type": "stop"}))
            await asyncio.wait_for(call.closed.wait(), 15)
        mic.cancel()
        receiver.cancel()
    return call, started_wall, time.time()


def server_lines(log: Path, call_id: str, started: float, ended: float) -> list[tuple[str, str]]:
    """The call's debug transcripts: by call id when the log has it, else by the call's time window."""
    first, last = (datetime.fromtimestamp(started - 1).strftime("%Y-%m-%d %H:%M:%S"),
                   datetime.fromtimestamp(ended + 5).strftime("%Y-%m-%d %H:%M:%S"))
    lines = []
    with log.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            match = _LOG_LINE.match(raw.rstrip("\n"))
            if not match:
                continue
            stamp, logged_call, speaker, text = match.groups()
            if (logged_call and logged_call == call_id) or (not logged_call and first <= stamp <= last):
                lines.append((speaker, text))
    return lines


def bigrams(text: str) -> set[str]:
    value = plain(text)
    return {value[index:index + 2] for index in range(len(value) - 1)}


def similar(a: str, b: str) -> float:
    first, second = bigrams(a), bigrams(b)
    return len(first & second) / len(first | second) if first and second else 0.0


def metrics(call: Call, lines: list[tuple[int, str]], spoken: list[tuple[str, str]]) -> dict:
    assistant = [text for speaker, text in spoken if speaker == "assistant"]
    sentences = [plain(part) for text in assistant for part in _SENTENCE.findall(text)]
    sentences = [value for value in sentences if len(value) >= 4]
    repeated = sum(value in sentences[:index] for index, value in enumerate(sentences))
    # A result told again: a line long enough to carry one that nearly repeats an earlier one.
    # Short stock sentences ("好的，你稍等一下") are the repeated-sentence ratio's business.
    results = [text for text in assistant if len(plain(text)) >= RESULT_CHARS]
    duplicates = []
    for index, text in enumerate(results):
        earlier = next((results[before] for before in range(index) if similar(results[before], text) >= SAME_RESULT),
                       None)
        if earlier is not None:
            duplicates.append((earlier, text))
    duplicate_lines = len(duplicates)
    ends = {data["index"]: at for at, kind, data in call.events if kind == "speech_end"}
    order = [index for index, _ in lines]
    latency, fragment_calls = {}, []
    for position, index in enumerate(order):
        start = ends.get(index)
        stop = ends.get(order[position + 1], float("inf")) if position + 1 < len(order) else float("inf")
        if start is None:
            continue
        first = next((at for at, kind, _ in call.events if kind == "audio" and start <= at < start + FIRST_AUDIO_SECONDS),
                     None)
        latency[index] = round(first - start, 2) if first is not None else None
        text = dict(lines)[index]
        accepted = [data for at, kind, data in call.events
                    if kind == "turn" and data.get("state") == "accepted" and start <= at < stop]
        if len(plain(text)) < FRAGMENT_CHARS and accepted:
            fragment_calls.append((index, text, len(accepted)))
    known = [value for value in latency.values() if value is not None]
    return {
        "assistant_lines": len(assistant), "sentences": len(sentences), "repeated_sentences": repeated,
        "repeated_ratio": round(repeated / len(sentences), 3) if sentences else 0.0,
        "found_openings": sum(text.strip().startswith(FOUND) for text in assistant),
        "duplicate_deliveries": duplicate_lines, "duplicate_examples": duplicates[:5],
        "fragment_tool_calls": sum(count for _, _, count in fragment_calls), "fragment_calls": fragment_calls,
        "latency": latency, "latency_median": sorted(known)[len(known) // 2] if known else None,
        "turns": sum(1 for _, kind, data in call.events if kind == "turn" and data.get("state") == "accepted"),
        "notes": sum(speaker == "note" for speaker, _ in spoken),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="http://127.0.0.1:8081")
    parser.add_argument("--utterances", default=str(ROOT / ".local-dev/voice-qa/replay_utterances.txt"))
    parser.add_argument("--log", default=str(ROOT / ".local-dev/assistant-backend.log"))
    parser.add_argument("--with-actions", action="store_true", help="also play requests that start real work")
    parser.add_argument("--limit", type=int, default=0, help="play only the first N lines")
    parser.add_argument("--out", default="")
    parser.add_argument("--rescore", default="", help="measure a saved --out report again instead of calling")
    args = parser.parse_args()
    if args.rescore:
        return rescore(Path(args.rescore))
    every = [(index, line.strip()) for index, line in enumerate(Path(args.utterances).read_text().splitlines(), 1)
             if line.strip()]
    lines = [(index, text) for index, text in every if args.with_actions or not is_action(text)]
    skipped = [(index, text) for index, text in every if (index, text) not in lines]
    lines = lines[:args.limit] if args.limit else lines
    if skipped:
        print("skipped (they would start real work; --with-actions plays them):")
        for index, text in skipped:
            print(f"  #{index} {text}")
    call, started, ended = asyncio.run(run_call(args.base, lines))
    spoken = server_lines(Path(args.log), call.ready.get("call_id", ""), started, ended)
    if not spoken:
        print("no debug transcripts found: the backend must run with VOICE_DEBUG_TRANSCRIPTS=true")
    result = metrics(call, lines, spoken)
    print("\ncall transcript (server)")
    for speaker, text in spoken:
        print(f"  [{speaker}] {text}")
    print("\nper-utterance first-audio latency (s)")
    for index, text in lines:
        print(f"  #{index:<3} {str(result['latency'].get(index)):>6}  {text}")
    print(f"\nrepeated sentences: {result['repeated_sentences']}/{result['sentences']} "
          f"({result['repeated_ratio']:.1%}) across {result['assistant_lines']} front-desk lines")
    print(f"lines opening with {FOUND}: {result['found_openings']}")
    print(f"duplicate deliveries: {result['duplicate_deliveries']}")
    for first, second in result["duplicate_examples"]:
        print(f"  - {first[:60]} ≈ {second[:60]}")
    print(f"tool calls on fragments (<{FRAGMENT_CHARS} chars): {result['fragment_tool_calls']} "
          f"{[(index, text) for index, text, _ in result['fragment_calls']]}")
    print(f"assistant turns: {result['turns']}; notes: {result['notes']}; median first-audio latency: "
          f"{result['latency_median']}s")
    if args.out:
        Path(args.out).write_text(json.dumps({"lines": lines, "skipped": skipped, "spoken": spoken, **result},
                                             ensure_ascii=False, indent=1, default=str))


def rescore(path: Path):
    """The text measures again from a saved report (latency and fragments come from the run itself)."""
    saved = json.loads(path.read_text())
    call = Call(None)
    result = {**saved, **{key: value for key, value in metrics(call, [tuple(line) for line in saved["lines"]],
                                                               [tuple(line) for line in saved["spoken"]]).items()
                          if key not in ("latency", "latency_median", "fragment_tool_calls", "fragment_calls", "turns")}}
    print(f"repeated sentences: {result['repeated_sentences']}/{result['sentences']} ({result['repeated_ratio']:.1%}) "
          f"across {result['assistant_lines']} front-desk lines")
    print(f"lines opening with {FOUND}: {result['found_openings']}")
    print(f"duplicate deliveries: {result['duplicate_deliveries']}")
    for first, second in result["duplicate_examples"]:
        print(f"  - {first[:50]} ≈ {second[:50]}")
    print(f"tool calls on fragments: {result['fragment_tool_calls']}; median first-audio latency: "
          f"{result['latency_median']}s; assistant turns: {result['turns']}; notes: {result['notes']}")


if __name__ == "__main__":
    main()
