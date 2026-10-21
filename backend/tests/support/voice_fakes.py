"""Scripted stand-ins for the realtime provider and the assistant link (voice tests)."""
import asyncio
import json
import time

from voice.provider import ProviderEvent

USAGE = {"input_tokens": 1200, "output_tokens": 60,
         "input_tokens_details": {"text_tokens": 1180, "audio_tokens": 20},
         "output_tokens_details": {"text_tokens": 10, "audio_tokens": 50}}
# A silent keepalive reply: text only (measured: "嗯", no audio).
TEXT_USAGE = {"input_tokens": 1300, "output_tokens": 2,
              "input_tokens_details": {"text_tokens": 1300, "audio_tokens": 0},
              "output_tokens_details": {"text_tokens": 2, "audio_tokens": 0}}
# An audio frame starting with this makes the scripted provider hear a request for the assistant.
ASK_MARKER = b"ASK!"
CHAT_MARKER = b"CHAT"
# ... makes the provider close the session (as after 180 s without a response); with CUT the user was speaking.
DROP_MARKER, CUT_MARKER = b"DRP!", b"CUT!"
IDLE_CLOSE = "Your session was closed because no response was generated for 180 seconds."


def event(kind, **fields):
    return ProviderEvent(kind, **fields)


def started(response_id):
    return event("response_started", response_id=response_id)


def audio(response_id, size=4800, event_id=""):
    return event("audio", response_id=response_id, audio=b"\x01\x00" * (size // 2), event_id=event_id)


def done(response_id, status="completed", usage=USAGE):
    return event("response_done", response_id=response_id, status=status, usage=usage)


def tool_call(call_id, text="帮我看看贪吃蛇进展", name="assistant_ask", response_id=None, arguments=None):
    arguments = arguments if arguments is not None else json.dumps({"request": text}, ensure_ascii=False)
    return event("tool_call", call_id=call_id, name=name, arguments=arguments, text=text, response_id=response_id)


def item(item_id, role="user", item_type="message", text=""):
    return event("item_created", item_id=item_id, role=role, item_type=item_type, text=text)


def transcript(text, item_id="", speaker="user"):
    return event("user_transcript" if speaker == "user" else "assistant_transcript", text=text, item_id=item_id)


def provider_error(reason, code="invalid_request_error", param="", text=""):
    return event("provider_error", code=code, reason=reason, param=param, text=text)


def silent_reply(response_id, item_id, status="completed"):
    """What the provider sends for a keepalive: a text-only reply with one output item."""
    return [started(response_id),
            event("output_item", response_id=response_id, item_id=item_id, role="assistant", item_type="message"),
            item(item_id, role="assistant"),
            event("assistant_transcript", text="嗯", item_id=item_id, response_id=response_id),
            done(response_id, status=status, usage=TEXT_USAGE)]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedProvider:
    """Records every command; with ``auto_reply`` each response.create is answered like the provider does."""

    def __init__(self, config=None, *, debug=False, auto_reply=False, fail_open=False):
        self.config, self.auto_reply, self.fail_open = config, auto_reply, fail_open
        self.sent: list[tuple] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.attempts = [{"mode": "scripted", "seconds": 0.0, "result": "ok"}]
        self.instructions = ""
        self.count = 0
        self.items = 0
        self.closed = False
        # How the session ended (RealtimeProvider sets these once its stream is over).
        self.close_code, self.close_reason, self.last_error = None, "", ""

    # -- what the bridge and the socket call --
    async def open(self):
        if self.fail_open:
            from voice.provider import ProviderUnavailable
            raise ProviderUnavailable("scripted failure")

    async def configure(self, instructions):
        self.instructions = instructions

    async def events(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def send_audio(self, pcm):
        if self.closed:  # like a closed websocket
            raise ConnectionError("provider session closed")
        self.sent.append(("audio", len(pcm), bytes(pcm[:4])))
        if self.auto_reply and pcm.startswith(ASK_MARKER):
            self.count += 1
            response_id, call_id = f"resp-{self.count}", f"call-{self.count}"
            self.push(event("user_started"), event("user_stopped"), transcript("帮我看看贪吃蛇进展", f"item-{call_id}"),
                      started(response_id), audio(response_id), tool_call(call_id), done(response_id))
        elif self.auto_reply and pcm.startswith(CHAT_MARKER):
            self.count += 1
            response_id = f"resp-{self.count}"
            self.push(event("user_started", item_id="chat-item"),
                      transcript("你好，能听到我说话吗？", "chat-item"),
                      started(response_id), audio(response_id), done(response_id))
        elif self.auto_reply and pcm.startswith(CUT_MARKER):
            self.push(event("user_started"))
            self.drop()
        elif self.auto_reply and pcm.startswith(DROP_MARKER):
            self.drop()

    async def keepalive(self, empty=False):
        self.sent.append(("keepalive", empty))
        if self.auto_reply:
            self.count += 1
            self.push(*silent_reply(f"resp-{self.count}", f"keep-{self.count}"))

    async def send_tool_output(self, call_id, payload):
        self.sent.append(("output", call_id, payload))

    async def create_note(self, text):
        self.sent.append(("note", text))
        if self.auto_reply:  # the provider names the item and confirms it
            self.items += 1
            self.push(item(f"note-{self.items}", text=text))

    async def delete_item(self, item_id):
        self.sent.append(("delete", item_id))
        if self.auto_reply:
            self.push(event("item_deleted", item_id=item_id))

    async def update_instructions(self, instructions):
        self.sent.append(("instructions", instructions))
        self.instructions = instructions

    async def create_response(self, instructions=None):
        self.sent.append(("create", instructions))
        if self.auto_reply:  # the reply starts, speaks and finishes at once
            self.count += 1
            response_id = f"resp-{self.count}"
            self.push(started(response_id), audio(response_id), done(response_id))

    async def cancel_response(self):
        self.sent.append(("cancel",))

    async def close(self):
        if not self.closed:
            self.closed = True
            self.end()  # like a closed socket: its event stream ends

    # -- test helpers --
    def push(self, *items):
        for item in items:
            self.queue.put_nowait(item)

    def end(self):
        self.queue.put_nowait(None)

    def drop(self, code=1007, reason=IDLE_CLOSE, error="response_idle_timeout"):
        """The provider closes the session under the call: its stream ends, later audio cannot be sent."""
        self.close_code, self.close_reason, self.last_error = code, reason, error
        self.closed = True
        self.end()

    def commands(self, kind):
        return [item for item in self.sent if item[0] == kind]

    def response_purposes(self):
        """Transport tests compare the per-response purpose; role preservation is tested separately."""
        return [(kind, text.rsplit("\n\n", 1)[-1] if text else text)
                for kind, text in self.commands("create")]


class FakeLink:
    """Accepts every turn; each result is released by the test with ``finish``."""

    def __init__(self):
        self.closed = False
        self.fail_start = False
        self.started, self.records, self.followed = [], [], []
        self.results: dict[str, asyncio.Future] = {}
        self.on_message = None
        self.waiting_cards: list[dict] = []  # the main session's pending cards (assistant.confirmations shape)

    async def start(self, ref):
        if self.fail_start:
            raise ValueError("Input text is required")
        self.started.append(ref)
        ref.inbox_id = f"inbox-{len(self.started)}"

    async def wait(self, ref, *, elapsed=0.0, on_message=None):
        self.on_message = on_message
        future = self.results.setdefault(ref.provider_call_id, asyncio.get_running_loop().create_future())
        return await future

    async def follow(self, ref, *, after, on_message=None):
        self.followed.append((ref, after))
        return await self.wait(ref, on_message=on_message)

    async def cards(self):
        return list(self.waiting_cards)

    def finish(self, call_id, status="ok", speech="贪吃蛇的收尾自检做完了。", cards=None, running=None,
               decision_versions=None, speech_stale=False):
        future = self.results.setdefault(call_id, asyncio.get_running_loop().create_future())
        future.set_result({"status": status, "speech": speech, **({"cards": cards} if cards else {}),
                           **({"running": running} if running else {}),
                           **({"decision_versions": decision_versions, "speech_stale": speech_stale}
                              if decision_versions is not None else {})})

    async def record(self, ref, **fields):
        self.records.append((ref.id, fields))

    async def answered(self, ref):
        self.records.append((ref.id, {"answered": ref.lane}))

    async def done(self, ref, outcome, *, delivered=False):
        await self.record(ref, outcome=outcome, delivered=delivered)




async def drain(rounds=10):
    """Let background turn tasks run their next steps."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def outbox(bridge):
    """Everything queued for the client so far (removes it)."""
    items = []
    while not bridge.outbox.empty():
        items.append(bridge.outbox.get_nowait())
    return items


def of_type(items, kind):
    return [item for item in items if isinstance(item, dict) and item.get("type") == kind]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False
