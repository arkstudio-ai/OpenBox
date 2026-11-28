"""Recorded-session replay for the agent loop (docs/kernel-refactor/replay-feasibility.md).

A case (``cases/<name>.json``) is a scripted session: the user turns, every
model reply in order, and the result of every scripted tool call. The harness
drives the real ``run_loop`` from it and observes three things:

- every model request body, as passed to ``litellm.acompletion``;
- the persisted ``agent_events`` of the session, in sequence order;
- the final message surface (messages, parts, tool calls and their results).

``recordings/<name>/`` holds what the current code produced. A replay run
compares against it; ``OPENBOX_REPLAY_UPDATE=1`` rewrites it, so an intended
prompt, tool or loop change shows up as a reviewable diff of those files.

Case format: ``turns`` is a list of ``{"user": text, "replies": [...],
"tools": [...], "answers": [...], "approvals": [...]}``. A reply is
``{"reasoning", "text" (a string or streamed pieces), "tool_calls": [{"id",
"name", "arguments" (an object, or a raw string)}], "finish", "usage"}``, or
``{"rate_limited": {"message", "retry_after_ms"}}`` for a provider 429; replies
are served in request order, including compaction requests. A tool entry is
``{"tool", "args", "result": {"title", "output", "metadata"}}`` or
``{"tool", "args", "raise": message}``; a scripted tool takes the first unused
entry with its name and arguments (calls from one reply run concurrently, and
their bodies may start in either order; the loop still persists them in call
order) and a call with no entry fails the case. ``answers`` reply to question cards,
``approvals`` to permission requests. Optional: ``config`` (``permission``,
``tool_exposure``, ``memory``), ``skills`` (name -> SKILL.md text),
``report_prefix``.

Seams (none is an attribute of ``agent.loop`` or ``agent.processor``):

- model: ``litellm.acompletion`` returns a stream built from the scripted reply;
- tool bodies: the built-in registry is rebuilt and each scripted tool's
  definition gets a replay body through ``define_tool``, so argument
  validation, truncation, hooks and permissions stay real. ``REAL_TOOLS``
  (``capability_search``, ``question``, ``skill``, ``todo_write`` ...) run for real;
- sandbox: ``sandbox.sandbox_manager.get_client`` returns None;
- clock: ``core.device_locale.DeviceLocale.now`` returns a fixed instant;
  prices come from ``tests/replay/rates.json`` (``BILLING_RATES_FILE``);
- configuration: ``OpenBoxConfig()`` defaults plus the case's overrides, never
  ``openbox.json`` or the environment;
- files: the process works in a fresh directory with its own XDG config home,
  so Skill discovery sees only the case's synthetic Skills;
- out of scope: next-step suggestions (``agent.suggestions.generate_suggestions``,
  a background request after the turn) are off; the memory router
  (``memory.routing.evaluate_context_needs``) answers "retrieve" offline;
- network: every httpx request fails and fails the case.

Normalisation (applied in the same way to requests, events and messages, and
only to values that differ between two runs of the same code):

- generated ids: ``<prefix>_<ULID>`` -> ``<prefix#n>``, a bare ULID -> ``<ulid#n>``,
  32-hex run ids -> ``<hex#n>``, and in events and messages 64-hex digests ->
  ``<sha#n>`` (they hash ids and times; the request's ``prompt_cache_key`` hashes
  fixed ids and stays verbatim); ``n`` counts first appearances within the case,
  so equal ids stay equal;
- ISO timestamps -> ``<time>``; wall-clock durations (``VOLATILE_NUMBERS``) -> ``<n>``;
- the case's working directory -> ``<workdir>``;
- non-JSON values in a request body (``httpx.Timeout``) -> their ``repr``.

Request bodies are otherwise compared as exact JSON text, key order included.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import difflib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

ROOT = Path(__file__).parent
CASES_DIR = ROOT / "cases"
RECORDINGS_DIR = ROOT / "recordings"
SHARED_DIR = RECORDINGS_DIR / "_shared"
UPDATE = os.environ.get("OPENBOX_REPLAY_UPDATE") == "1"
TUNING_FROM_ENV = bool(os.environ.get("OPENBOX_REPLAY_TUNING"))

MODEL = "openai/qwen3.8-flash"
SESSION, USER = "s1", "u1"  # seeded by the durable-question fixture
FROZEN_NOW = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
DEFAULT_USAGE = {"prompt_tokens": 1200, "completion_tokens": 40}

# Tools whose real body runs: kernel tools whose effect on later requests is
# what the cases check, and ones that need nothing outside the test database.
REAL_TOOLS = frozenset({"capability_search", "question", "skill", "skill_search", "invalid", "batch",
                        "todo_write", "todo_read"})
# Wall-clock measurements that land in persisted rows.
VOLATILE_NUMBERS = frozenset({"duration", "duration_ms", "elapsed_ms", "latency_ms"})
# Strings at or above this length are stored once under recordings/_shared.
SHARED_MIN_CHARS = 2000
RUN_TIMEOUT_SECONDS = 20


# -- Scripted model ----------------------------------------------------------

class ScriptMismatch(AssertionError):
    pass


class _Stream:
    """The minimal async stream litellm hands back for ``stream=True``."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self):
        return None


def reply_chunks(reply: dict) -> list:
    """OpenAI-style stream chunks for one scripted reply."""
    from litellm.types.utils import ModelResponseStream

    base = {"id": "chatcmpl-replay", "object": "chat.completion.chunk", "created": 1, "model": "replay"}

    def chunk(delta=None, finish=None, **extra):
        choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
        return {**base, "choices": choices, **extra}

    rows = []
    if reply.get("reasoning"):
        rows.append(chunk({"role": "assistant", "reasoning_content": reply["reasoning"]}))
    for piece in ([reply["text"]] if isinstance(reply.get("text"), str) else reply.get("text") or []):
        rows.append(chunk({"role": "assistant", "content": piece}))
    for index, call in enumerate(reply.get("tool_calls") or []):
        arguments = call.get("arguments", {})
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        rows.append(chunk({"role": "assistant", "tool_calls": [{
            "index": index, "id": call["id"], "type": "function",
            "function": {"name": call["name"], "arguments": raw}}]}))
    finish = reply.get("finish", "tool_calls" if reply.get("tool_calls") else "stop")
    if finish is not None:
        rows.append(chunk({}, finish))
    usage = {**DEFAULT_USAGE, **(reply.get("usage") or {})}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    rows.append(chunk(None, usage=usage))
    return [ModelResponseStream(**row) for row in rows]


class ScriptedModel:
    """Answers streaming model requests from the current turn's replies, in order.

    The loop turns exceptions from the provider and from tool bodies into
    retries and error results, so a mismatch with the script is collected in
    ``mismatches`` (shared with ScriptedTools) and raised after the turn.
    """

    def __init__(self, mismatches: list[str]):
        self.replies: list[dict] = []
        self.requests: list[dict] = []
        self.turn = 0
        self.unscripted: list[str] = []
        self.mismatches = mismatches

    async def __call__(self, **kwargs):
        if not kwargs.get("stream"):
            # Title generation and other one-shot completions are not scripted.
            self.unscripted.append(_describe(kwargs))
            raise RuntimeError("unscripted non-streaming completion")
        self.requests.append({"turn": self.turn, "body": kwargs})
        if not self.replies:
            self.mismatches.append(f"turn {self.turn}: the loop sent model request {len(self.requests)} "
                                   "but the case has no reply left for it")
            # End the run at once instead of letting the loop retry.
            return _Stream(reply_chunks({"text": "(script exhausted)"}))
        reply = self.replies.pop(0)
        if reply.get("rate_limited"):
            raise rate_limit_error(reply["rate_limited"])
        return _Stream(reply_chunks(reply))


def rate_limit_error(spec: dict):
    """The provider's 429, with the Retry-After the loop honours verbatim (so no jittered backoff)."""
    import httpx
    import litellm

    response = httpx.Response(429, headers={"retry-after-ms": str(spec.get("retry_after_ms", 1))},
                              request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions"))
    return litellm.RateLimitError(spec.get("message", "rate limited"), llm_provider="openai",
                                  model=MODEL.split("/", 1)[1], response=response)


def _describe(kwargs: dict) -> str:
    messages = kwargs.get("messages") or []
    last = messages[-1].get("content") if messages else ""
    return str(last)[:80]


# -- Scripted tool bodies ----------------------------------------------------

class ScriptedTools:
    """Serves the case's tool results in call order and checks name and arguments."""

    def __init__(self, mismatches: list[str]):
        self.results: list[dict] = []
        self.turn = 0
        self.mismatches = mismatches

    def body(self, tool_id: str):
        async def replay_body(args, ctx):
            from tool.tool import ToolResult
            actual_args = args.model_dump(mode="json", exclude_unset=True) if hasattr(args, "model_dump") else args
            call = f"{tool_id}({json.dumps(actual_args, ensure_ascii=False)})"
            # The first unused entry for this tool and these arguments: calls from
            # one reply run concurrently, so their bodies start in either order.
            expected = next((entry for entry in self.results if entry["tool"] == tool_id
                             and entry.get("args", actual_args) == actual_args), None)
            if expected is None:
                left = ", ".join(f"{e['tool']}({json.dumps(e.get('args'), ensure_ascii=False)})"
                                 for e in self.results) or "none"
                self.mismatches.append(f"turn {self.turn}: tool call {call} is not in the script "
                                       f"(unused entries: {left})")
                raise ScriptMismatch("tool call not in the script")
            self.results.remove(expected)
            if expected.get("raise"):
                raise RuntimeError(expected["raise"])
            result = expected.get("result") or {}
            return ToolResult(title=result.get("title", ""), output=result.get("output", ""),
                              metadata=copy.deepcopy(result.get("metadata") or {}))
        return replay_body


def install_registry(monkeypatch, scripted: ScriptedTools) -> None:
    """The real built-in registry, with replay bodies for every tool not in REAL_TOOLS."""
    import tool.registry as registry
    from tool.tool import define_tool

    monkeypatch.setattr(registry, "_tools", {})
    registry.register_builtin_tools(load_custom=False)
    replaced = {}
    for tool_id, info in registry._tools.items():
        if tool_id in REAL_TOOLS:
            replaced[tool_id] = info
            continue
        execute = define_tool(tool_id, description=info.description, parameters=info.parameters,
                              execute=scripted.body(tool_id), sandbox_required=info.sandbox_required).execute
        replaced[tool_id] = dataclasses.replace(info, execute=execute)
    monkeypatch.setattr(registry, "_tools", replaced)


# -- Environment -------------------------------------------------------------

def build_config(case: dict):
    from core.config import AgentTuningConfig, MemoryConfig, OpenBoxConfig

    settings = case.get("config") or {}
    config = OpenBoxConfig()
    config.model = MODEL
    config.models = []
    if "permission" in settings:
        config.permission = settings["permission"]
    config.jwt_secret = ""
    config.tool_exposure.mode = settings.get("tool_exposure", "portable")
    config.memory = MemoryConfig(**settings.get("memory", {}))
    tuning = dict(settings.get("agent_tuning", {}))
    # OPENBOX_REPLAY_TUNING=relaxed_prompt,... turns switches on for every case, to
    # see what a rollout changes: record with it, read `git diff`, then restore.
    for name in filter(None, os.environ.get("OPENBOX_REPLAY_TUNING", "").split(",")):
        tuning[name.strip()] = True
    config.agent_tuning = AgentTuningConfig(**tuning)
    return config


def install_environment(monkeypatch, case: dict, workdir: Path, model: ScriptedModel, network: list):
    import httpx
    import litellm
    from core import device_locale

    config = build_config(case)
    monkeypatch.setattr("core.config._config", config)
    monkeypatch.setattr("core.config.get_config", lambda: config)

    async def no_sandbox(*args, **kwargs):
        return None
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", no_sandbox)
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr(litellm, "acompletion", model)
    # Next-step suggestions run after the turn as a background request; out of scope.
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=None))
    monkeypatch.setattr(device_locale.DeviceLocale, "now", lambda self: FROZEN_NOW)

    async def refuse(self, request, *args, **kwargs):
        network.append(str(request.url))
        raise httpx.ConnectError("network is off in the replay tests", request=request)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)

    # Skill discovery reads the XDG config home and the working directory.
    (workdir / "config").mkdir(parents=True, exist_ok=True)
    for name, text in (case.get("skills") or {}).items():
        path = workdir / ".openbox" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workdir / "config"))
    monkeypatch.chdir(workdir)
    # The host Skill cache must neither leak another test's scan in nor this case's out.
    monkeypatch.setattr("skill.skill._skills", {})
    monkeypatch.setattr("skill.skill._loaded", False)
    monkeypatch.setattr("skill.skill._fingerprint", ())
    monkeypatch.delenv("TRAJECTORY_RECORD_USER_IDS", raising=False)
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    # Step costs are persisted; price them from a fixed list, not the product's.
    monkeypatch.setenv("BILLING_RATES_FILE", str(ROOT / "rates.json"))
    return config


def stub_memory_routing(monkeypatch) -> list[str]:
    """The JEV router answers "retrieve" offline, as in the SQL budget test; returns the routed utterances."""
    routed = []

    async def route(state, memory_config, **kwargs):
        routed.append(state["utterance"])
        sure = {"retrieve": 0.95, "skip": 0.03, "unknown": 0.02}
        return {"model": memory_config.jev_model, "usage": {"input_tokens": 32, "output_tokens": 0},
                "answers": {"memory_needed": {"choice": "retrieve", "probabilities": sure, "confidence": 0.95},
                            "task_needed": {"choice": "skip", "confidence": 0.95,
                                            "probabilities": {"read": 0.03, "skip": 0.95, "unknown": 0.02}}}}
    monkeypatch.setattr("memory.routing.evaluate_context_needs", route)
    return routed


# -- Running a case ------------------------------------------------------------

async def run_case(case: dict, monkeypatch, workdir: Path) -> dict:
    """Drive the real loop through every turn of ``case``; return the normalised observation."""
    from session.session import create_user_message

    mismatches: list[str] = []
    model, tools, network = ScriptedModel(mismatches), ScriptedTools(mismatches), []
    config = install_environment(monkeypatch, case, workdir, model, network)
    install_registry(monkeypatch, tools)
    routed = stub_memory_routing(monkeypatch)
    permissions: list[dict] = []
    for index, turn in enumerate(case["turns"], start=1):
        model.turn = tools.turn = index
        model.replies = list(turn.get("replies") or [])
        tools.results = list(turn.get("tools") or [])
        await create_user_message(SESSION, turn["user"], model=config.model, user_id=USER)
        await _run_turn(turn, permissions)
        if mismatches:
            raise ScriptMismatch("; ".join(mismatches))
        if model.replies:
            raise ScriptMismatch(f"turn {index}: the loop stopped with {len(model.replies)} scripted "
                                 "replies unused")
        if tools.results:
            raise ScriptMismatch(f"turn {index}: {len(tools.results)} scripted tool results unused")
    assert not network, f"the replay tried to reach the network: {network}"
    events = await _events()
    messages = await _messages()
    normaliser = Normaliser(workdir)
    observed = {
        "requests": [{"turn": r["turn"], "body": normaliser.request(r["body"])} for r in model.requests],
        "events": [normaliser.value(event) for event in events],
        "messages": [normaliser.value(message) for message in messages],
    }
    if model.unscripted:
        observed["unscripted_completions"] = model.unscripted
    if routed:
        observed["memory_routed"] = routed
    if permissions:
        observed["permissions"] = [normaliser.value(asked) for asked in permissions]
    if case.get("report_prefix"):
        observed["prefix"] = prefix_report(observed["requests"])
    _guard_wall_clock(observed)
    return observed


def prefix_report(requests: list[dict]) -> list[dict]:
    """For each request after the first: what it repeats of the one before it."""
    report = []
    for index in range(1, len(requests)):
        before, after = requests[index - 1]["body"], requests[index]["body"]
        old, new = before.get("messages") or [], after.get("messages") or []
        shared = 0
        while shared < min(len(old), len(new)) and _canonical(old[shared]) == _canonical(new[shared]):
            shared += 1
        report.append({"request": index + 1, "turn": requests[index]["turn"],
                       "same_tools": _canonical(before.get("tools")) == _canonical(after.get("tools")),
                       "messages_repeated": f"{shared} of {len(old)}", "messages_sent": len(new)})
    return report


async def _run_turn(turn: dict, permissions: list[dict]) -> None:
    from agent import loop

    run = asyncio.ensure_future(loop.run_loop(SESSION, user_id=USER))
    try:
        for action in turn.get("approvals") or []:
            permissions.append(await _approve(run, action))
        await asyncio.wait_for(run, timeout=RUN_TIMEOUT_SECONDS)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    for answer in turn.get("answers") or []:
        await _answer_question(answer)


async def _approve(run: asyncio.Future, action: str) -> dict:
    """Reply to the permission request the running loop is waiting on; return what was asked."""
    from permission import permission

    deadline = asyncio.get_running_loop().time() + RUN_TIMEOUT_SECONDS
    while not (pending := permission.list_pending(USER)):
        if run.done():
            raise ScriptMismatch("the run finished without asking for the scripted permission")
        if asyncio.get_running_loop().time() > deadline:
            raise ScriptMismatch("no permission request appeared")
        await asyncio.sleep(0.005)
    assert len(pending) == 1, f"expected one pending permission request, found {len(pending)}"
    asked = pending[0]
    await permission.reply(asked.id, action, user_id=USER)
    return {"tool": asked.tool, "patterns": list(asked.patterns), "input": asked.input, "reply": action}


async def _answer_question(answers: list[list[str]]) -> None:
    """Answer the pending question card and let the continuation worker resume the run."""
    from question import question as questions
    from question.continuation import QuestionContinuationWorker

    pending = await questions.list_pending(USER)
    assert len(pending) == 1, f"expected one pending question, found {len(pending)}"
    await questions.reply(pending[0].id, answers, USER)
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*worker.runs.values()), timeout=RUN_TIMEOUT_SECONDS)
    finally:
        await worker.stop()


async def _events() -> list[dict]:
    from sqlalchemy import select

    import db.base as database
    from db.models.agent_event import AgentEvent

    async with database.get_db_session() as db:
        rows = (await db.execute(select(AgentEvent).where(AgentEvent.session_id == SESSION)
                                 .order_by(AgentEvent.sequence))).scalars().all()
    return [{"seq": row.sequence, "kind": row.kind, "step": row.step_id, "message": row.message_id,
             "part": row.part_id, "call": row.tool_call_id, "payload": row.payload} for row in rows]


async def _messages() -> list[dict]:
    from session.session import get_messages

    out = []
    for message in await get_messages(SESSION, user_id=USER):
        info = message.info if hasattr(message, "info") else message
        parts = []
        for part in message.parts or []:
            data = part.model_dump(mode="json") if hasattr(part, "model_dump") else dict(part)
            for key in ("session_id", "message_id"):
                data.pop(key, None)
            parts.append(data)
        out.append({"id": getattr(info, "id", None), "role": getattr(info, "role", None),
                    "agent": getattr(info, "agent", None), "finish": getattr(info, "finish", None),
                    "error": _jsonable(getattr(info, "error", None)), "parts": parts})
    return out


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _guard_wall_clock(observed: dict) -> None:
    """Today's real date left after normalisation comes from the wall clock: the recording breaks tomorrow."""
    today = datetime.now().date()
    if today == FROZEN_NOW.date():
        return
    markers = {today.isoformat(), f"{today.year}年{today.month}月{today.day}日"}
    for section, rows in observed.items():
        for index, row in enumerate(rows):
            text = json.dumps(row, ensure_ascii=False)
            found = [marker for marker in markers if marker in text]
            assert not found, (f"{section}[{index}] contains today's date {found}: some code reads the wall "
                               "clock; freeze it (DeviceLocale.now) or normalise it in tests/replay/harness.py")


# -- Normalisation -------------------------------------------------------------

_ULID = re.compile(r"\b(?:([a-z][a-z0-9]*)_)?([0-9A-HJKMNP-TV-Z]{26})\b")
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


class Normaliser:
    def __init__(self, workdir: Path):
        self._ids: dict[str, dict[str, str]] = {}
        roots = {str(workdir), str(workdir.resolve())}
        self._roots = sorted(roots, key=len, reverse=True)

    def _placeholder(self, kind: str, raw: str) -> str:
        table = self._ids.setdefault(kind, {})
        if raw not in table:
            table[raw] = f"<{kind}#{len(table) + 1}>"
        return table[raw]

    def text(self, value: str, *, digests: bool = True) -> str:
        for root in self._roots:
            value = value.replace(root, "<workdir>")
        value = _ISO_TIME.sub("<time>", value)
        value = _ULID.sub(lambda m: self._placeholder(m.group(1) or "ulid", m.group(0)), value)
        if digests:
            value = _HEX64.sub(lambda m: self._placeholder("sha", m.group(0)), value)
        return _HEX32.sub(lambda m: self._placeholder("hex", m.group(0)), value)

    def value(self, value: Any, key: str | None = None, *, digests: bool = True) -> Any:
        if isinstance(value, dict):
            return {k: self.value(v, k, digests=digests) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v, digests=digests) for v in value]
        if isinstance(value, str):
            return self.text(value, digests=digests)
        if key in VOLATILE_NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
            return "<n>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return repr(value)

    def request(self, body: dict) -> dict:
        # Digests in a request (prompt_cache_key) are derived from fixed ids, so they stay verbatim.
        return self.value(body, digests=False)


# -- Recordings on disk ----------------------------------------------------------
#
# recordings/<case>/requests.json   one entry per model request; long strings
#                                   (system prompt, tool list) are references
#                                   into recordings/_shared/<sha12>.json
# recordings/<case>/events.json     one normalised agent_event per line
# recordings/<case>/messages.json   the final message surface
#
# Multi-line strings are stored as {"$lines": [...]} so a prompt change shows
# up as a line diff. The transformation is lossless and undone on load.

def _encode_lines(value):
    if isinstance(value, dict):
        return {k: _encode_lines(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_lines(v) for v in value]
    if isinstance(value, str) and "\n" in value and len(value) > 120:
        return {"$lines": value.split("\n")}
    return value


def _decode_lines(value):
    if isinstance(value, dict):
        if set(value) == {"$lines"}:
            return "\n".join(value["$lines"])
        return {k: _decode_lines(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_lines(v) for v in value]
    return value


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _share(value, shared: dict):
    """Replace the tool list and long strings with references to shared files."""
    key = hashlib.sha256(_canonical(value).encode()).hexdigest()[:12]
    shared[key] = value
    return {"$shared": key}


def _externalise(body: dict, shared: dict) -> dict:
    out = {}
    for key, value in body.items():
        if key == "tools" and value:
            out[key] = _share(value, shared)
        elif key == "messages":
            out[key] = [{k: (_share(v, shared) if isinstance(v, str) and len(v) >= SHARED_MIN_CHARS else v)
                         for k, v in message.items()} for message in value]
        else:
            out[key] = value
    return out


def _internalise(value):
    if isinstance(value, dict):
        if set(value) == {"$shared"}:
            path = SHARED_DIR / f"{value['$shared']}.json"
            return _decode_lines(json.loads(path.read_text(encoding="utf-8")))
        return {k: _internalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_internalise(v) for v in value]
    return value


def _dump_pretty(value) -> str:
    return json.dumps(_encode_lines(value), ensure_ascii=False, indent=1) + "\n"


def _dump_rows(rows: list) -> str:
    """A JSON array with one compact row per line, for row-level diffs."""
    if not rows:
        return "[]\n"
    return "[\n" + ",\n".join(_canonical(row) for row in rows) + "\n]\n"


def write_recording(name: str, observed: dict) -> None:
    directory = RECORDINGS_DIR / name
    directory.mkdir(parents=True, exist_ok=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    shared: dict = {}
    requests = [{"turn": r["turn"], "body": _externalise(r["body"], shared)} for r in observed["requests"]]
    for key, value in shared.items():
        (SHARED_DIR / f"{key}.json").write_text(_dump_pretty(value), encoding="utf-8")
    (directory / "requests.json").write_text(_dump_pretty(requests), encoding="utf-8")
    (directory / "events.json").write_text(_dump_rows(observed["events"]), encoding="utf-8")
    (directory / "messages.json").write_text(_dump_pretty(observed["messages"]), encoding="utf-8")
    extra = {k: v for k, v in observed.items() if k not in {"requests", "events", "messages"}}
    extra_path = directory / "extra.json"
    if extra:
        extra_path.write_text(_dump_pretty(extra), encoding="utf-8")
    elif extra_path.exists():
        extra_path.unlink()


def prune_shared() -> None:
    """Delete shared files no recording refers to any more."""
    if not SHARED_DIR.exists():
        return
    used = set()
    for path in RECORDINGS_DIR.glob("*/requests.json"):
        used.update(re.findall(r'"\$shared": "([0-9a-f]{12})"', path.read_text(encoding="utf-8")))
    for path in SHARED_DIR.glob("*.json"):
        if path.stem not in used:
            path.unlink()


def read_recording(name: str) -> dict | None:
    directory = RECORDINGS_DIR / name
    if not (directory / "requests.json").exists():
        return None
    load = lambda file: json.loads((directory / file).read_text(encoding="utf-8"))  # noqa: E731
    recording = {
        "requests": _internalise(_decode_lines(load("requests.json"))),
        "events": load("events.json"),
        "messages": _decode_lines(load("messages.json")),
    }
    if (directory / "extra.json").exists():
        recording.update(_decode_lines(load("extra.json")))
    return recording


def load_case(name: str) -> dict:
    case = json.loads((CASES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    # Recordings are the switches-off baseline; a switch belongs in a test that
    # compares against it (test_a_deliberate_prompt_change_shows_up_as_a_diff).
    assert not (case.get("config") or {}).get("agent_tuning"), f"{name}: cases record with agent_tuning off"
    return case


def case_names() -> list[str]:
    return sorted(path.stem for path in CASES_DIR.glob("*.json"))


# -- Comparison ----------------------------------------------------------------

def differences(expected: dict, observed: dict, *, limit: int = 120) -> str:
    """A readable account of the first differences, or "" when the runs match."""
    report = []
    extras = sorted((set(expected) | set(observed)) - {"requests", "events", "messages"})
    for section in ("requests", "events", "messages", *extras):
        old, new = expected.get(section), observed.get(section)
        if _canonical(old) == _canonical(new):
            continue
        if section == "requests" and old and new:
            if len(old) != len(new):
                report.append(f"requests: {len(old)} recorded, {len(new)} sent")
            for index, (a, b) in enumerate(zip(old, new)):
                if _canonical(a) != _canonical(b):
                    report.append(f"request {index + 1} (turn {b['turn']}) differs:")
                    report.extend(_unified(a, b, limit))
                    break
        else:
            report.append(f"{section} differ:")
            report.extend(_unified(old, new, limit))
    return "\n".join(report)


def _unified(old, new, limit: int) -> list[str]:
    a = _dump_pretty(old).splitlines()
    b = _dump_pretty(new).splitlines()
    lines = list(difflib.unified_diff(a, b, "recorded", "replayed", n=2, lineterm=""))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more diff lines"]
    return lines
