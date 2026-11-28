"""Opt-in behaviour evaluation of the build agent (docs/KERNEL_DSH_COMPARISON_20261128.md 25.5).

Runs the real agent loop (``agent.loop.run_loop``: system prompt, tool
resolution and exposure, processor, durable questions, real tools) on
synthetic data in a temporary SQLite database, one task at a time, ``N``
repeats each. Each run records whether the task's judge passed, steps, tool
calls per step, question calls, ``capability_search`` calls, repeated loads
of one Skill, malformed tool calls, output and total tokens, and wall time.

Live runs call the configured model provider and cost money. Run them from
``backend`` with the normal local provider configuration (the harness loads
``backend/.env`` the way ``assistant_placement_eval.py`` does)::

    .venv/bin/python -m tests.manual.agent_behavior_eval \
        --model openai/MiniMax-M3.1-Flash --repeats 3 --label baseline-1 \
        --output /tmp/agent-eval/baseline-1.json

    # The same, with the portable tool exposure replaced by the legacy one
    .venv/bin/python -m tests.manual.agent_behavior_eval --exposure legacy_eager ...

    # One agent_tuning switch on (repeatable; see core/config.py AgentTuningConfig)
    .venv/bin/python -m tests.manual.agent_behavior_eval --tuning relaxed_prompt ...

    # Decision rules of 25.5: one or two baseline rounds, then the candidate
    .venv/bin/python -m tests.manual.agent_behavior_eval --compare base-1.json base-2.json cand.json [--a1]

    # No provider call at all: a scripted fake model drives the same loop
    .venv/bin/python -m tests.manual.agent_behavior_eval --fake-model --tasks files_sales_summary

What is replaced, and why (everything else is the shipped code):

- The sandbox. ``LocalSandbox`` stands in for the Action Server: the
  sandbox's ``/workspace`` is a fresh temporary host directory. ``bash`` runs
  on the host in that directory with a minimal environment (system ``PATH``,
  no provider keys, ``HOME`` and ``TMPDIR`` inside the run directory) and
  paths under ``/workspace`` rewritten. On macOS ``sandbox-exec`` denies
  writes outside the run directory; reads are not restricted, and on other
  platforms there is no confinement. Commands the model writes run as you,
  so run live evaluations on a development machine. Tasks that need a cloud
  desktop (browser, the second half of video production) are not in this set.
- Title generation and reply suggestions are switched off (side calls, not
  agent behaviour); memory is off; trajectory recording is off; billing is
  metered in shadow mode (the synthetic workspace owns no credits). Every
  ``agent_tuning`` switch not named with ``--tuning`` is off.
- Inbox wakes are not scheduled, so work the personal assistant hands to
  another conversation is recorded but not executed (as in the placement
  evaluation).
- Change snapshots are skipped (``snapshot.track`` returns no snapshot).

Hard limits per run: 30 steps and 200,000 tokens (25.5); the run is stopped
when either is reached and recorded with ``limit_hit``.

Research tasks are judged by a person: each run in the result file carries
``"judge": "human"``, the rubric and the final answer. Set ``"completed"``
to true or false there before ``--compare``; runs left at null are reported
as pending and left out of the completion rule.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Trajectory recording writes to its own database and spool; the evaluation
# measures the agent, not the recorder.
os.environ["TRAJECTORY_RECORDING_ENABLED"] = "false"

MAX_STEPS = 30
MAX_TOKENS = 200_000
DEFAULT_REPEATS = 3
RUN_TIMEOUT_SECONDS = 900
QUESTION_TOOLS = frozenset({"question"})
PAID_GENERATION_TOOLS = frozenset({"image_gen", "video_generate", "video_compose", "footage_analyze",
                                   "light_remake", "light_wan_story", "light_gallery", "autopilot_run"})


# ---------------------------------------------------------------------------
# Local stand-in for the sandbox Action Server
# ---------------------------------------------------------------------------

class LocalSandbox:
    """The subset of ``sandbox.client.SandboxClient`` the build agent's tools use.

    ``/workspace`` (the sandbox root) is ``root`` on the host. Results speak
    sandbox paths, so the model never sees the host directory. Outputs follow
    the Action Server's formats (``container/action_server.py``).
    """

    desktop_id = ""

    def __init__(self, root: Path, *, confine_writes: bool = True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = self.root.parent / "home"
        self.tmp = self.root.parent / "tmp"
        for folder in (self.home, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []
        # On macOS, sandbox-exec keeps the model's shell from writing outside
        # this run's directory. Reads are not restricted, and other platforms
        # get no confinement at all.
        self.confined = bool(confine_writes and sys.platform == "darwin" and shutil.which("sandbox-exec"))

    # -- paths ---------------------------------------------------------------
    def host(self, path: str) -> Path:
        """Host path of a sandbox path; anything outside /workspace is refused."""
        text = str(path or "/workspace")
        pure = PurePosixPath(text if text.startswith("/") else f"/workspace/{text}")
        parts = pure.parts
        if parts[:2] != ("/", "workspace"):
            raise FileNotFoundError(f"Path not found: {path} (only /workspace exists in this sandbox)")
        candidate = self.root.joinpath(*parts[2:])
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PermissionError(f"Path escapes /workspace: {path}")
        return candidate

    def virtual(self, text: str) -> str:
        return (text.replace(str(self.root), "/workspace").replace(str(self.home), "/root")
                .replace(str(self.tmp), "/tmp"))

    def _command(self, command: str) -> str:
        """Point sandbox paths in a shell command at the host directory."""
        return re.sub(r"(?<![\w./-])/workspace(?=$|[/\s'\";:)|&<>])", lambda _match: str(self.root), command)

    def _env(self) -> dict[str, str]:
        # Only what a shell needs: the provider keys loaded from .env stay out.
        # System directories only, so neither secrets nor the operator's home
        # paths reach the model through `env`.
        env = {"PATH": ":".join(folder for folder in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                                      "/usr/sbin", "/sbin") if Path(folder).is_dir()),
               "LANG": "C.UTF-8", "TZ": "Asia/Shanghai"}
        env["HOME"] = str(self.home)
        env["TMPDIR"] = str(self.tmp)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _argv(self, command: str) -> list[str]:
        argv = ["bash", "-c", self._command(command)]
        if not self.confined:
            return argv
        run_dir = str(self.root.parent).replace('"', "")
        profile = ("(version 1)(allow default)(deny file-write*)"
                   f'(allow file-write* (subpath "{run_dir}") (literal "/dev/null") (literal "/dev/tty")'
                   ' (regex #"^/dev/fd/"))')
        return ["sandbox-exec", "-p", profile, *argv]

    def _cwd(self, workdir: str) -> Path:
        try:
            cwd = self.host(workdir or "/workspace")
        except (FileNotFoundError, PermissionError):
            cwd = self.root
        if not cwd.is_dir():
            cwd = self.root
        return cwd

    # -- execution -------------------------------------------------------------
    def _native(self, command: str):
        """Platform-fixed commands whose GNU syntax the host may not share."""
        from sandbox.client import ExecuteResult
        match = re.fullmatch(r"stat -c %Y -- (.+?) 2>/dev/null", command.strip())
        if match:
            try:
                target = self.host(shlex.split(match.group(1))[0])
                return ExecuteResult(0, f"{int(target.stat().st_mtime)}\n", "")
            except (OSError, ValueError):
                return ExecuteResult(1, "", "")
        if command.startswith("python3 -c ") and "canonical_path" in command:
            payload = json.loads(__import__("base64").b64decode(shlex.split(command)[-1]))
            rows = []
            for item in payload:
                host = self.host(item["path"]).resolve(strict=not item["allow_missing"])
                virtual = self.virtual(str(host))
                relative = virtual[len("/workspace/"):] if virtual.startswith("/workspace/") else (
                    "." if virtual == "/workspace" else None)
                rows.append({"canonical_path": virtual, "workspace_relative": relative})
            return ExecuteResult(0, json.dumps(rows), "")
        return None

    async def execute(self, command: str, timeout: int = 120, workdir: str = "/workspace"):
        from sandbox.client import ExecuteResult
        self.calls.append(f"execute:{command[:80]}")
        try:
            native = self._native(command)
        except (OSError, ValueError) as exc:
            return ExecuteResult(1, "", str(exc))
        if native is not None:
            return native
        process = await asyncio.create_subprocess_exec(
            *self._argv(command), cwd=str(self._cwd(workdir)), env=self._env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ExecuteResult(124, "", f"Command timed out after {timeout}s")
        return ExecuteResult(process.returncode, self.virtual(out.decode(errors="replace")),
                             self.virtual(err.decode(errors="replace")))

    async def execute_stream(self, command: str, timeout: int = 120, idle_timeout: int = 60,
                             workdir: str = "/workspace"):
        from sandbox.client import CommandExit, OutputChunk
        result = await self.execute(command, timeout=timeout, workdir=workdir)
        if result.stdout:
            yield OutputChunk(type="stdout", content=result.stdout)
        if result.stderr:
            yield OutputChunk(type="stderr", content=result.stderr)
        yield CommandExit(result.exit_code, output_complete=True)

    async def kill_command(self, pid: int) -> None:
        return None

    # -- files -------------------------------------------------------------------
    @staticmethod
    def _missing(path: str):
        import httpx
        request = httpx.Request("POST", "http://local-sandbox/read_file")
        response = httpx.Response(404, request=request, json={"detail": f"File not found: {path}"})
        return httpx.HTTPStatusError(f"File not found: {path}", request=request, response=response)

    async def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        self.calls.append(f"read_file:{path}")
        target = self.host(path)
        if not target.exists():
            raise self._missing(path)
        if not target.is_file():
            raise IsADirectoryError(f"Not a file: {path}")
        lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        start = max(0, offset)
        selected = lines[start:start + limit]
        return "\n".join(f"     {number}\t{line[:2000] + '... (truncated)' if len(line) > 2000 else line}"
                         for number, line in enumerate(selected, start=start + 1))

    async def read_file_if_exists(self, path: str, offset: int = 0, limit: int = 2000) -> str | None:
        try:
            return await self.read_file(path, offset, limit)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return None
            raise

    async def read_file_raw(self, path: str) -> str:
        target = self.host(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        self.calls.append(f"write_file:{path}")
        target = self.host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def glob(self, pattern: str, path: str = "/workspace", *, include_sensitive: bool = False) -> list[str]:
        self.calls.append(f"glob:{pattern}")
        base = self.host(path)
        if not base.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        found = sorted((p for p in base.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.virtual(str(p)) for p in found[:1000]]

    async def grep(self, pattern: str, path: str = "/workspace", file_type: str | None = None,
                   max_results: int = 100, *, include_sensitive: bool = False) -> str:
        self.calls.append(f"grep:{pattern}")
        command = ["grep", "-rn", "--color=never"]
        if file_type:
            command += ["--include", f"*.{file_type}"]
        command += ["-m", str(max_results), "--", pattern, str(self.host(path))]
        done = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=30)
        return self.virtual(done.stdout.decode(errors="replace"))

    async def list_files(self, path: str = "/workspace") -> list[dict]:
        target = self.host(path)
        return [{"name": item.name, "is_dir": item.is_dir(), "size": item.stat().st_size if item.is_file() else None,
                 "modified": None} for item in sorted(target.iterdir())]

    async def resolve_paths(self, targets) -> list:
        from sandbox.client import ResolvedPath
        rows = []
        for target in targets:
            host = self.host(target.path).resolve(strict=not target.allow_missing)
            virtual = self.virtual(str(host))
            relative = virtual[len("/workspace/"):] if virtual.startswith("/workspace/") else None
            rows.append(ResolvedPath(canonical_path=virtual, workspace_relative=relative))
        return rows

    # -- catalogue: no sandbox-installed Skills or MCP servers -------------------
    async def list_skills(self) -> list[dict]:
        return []

    async def list_mcp_tools(self) -> list[dict]:
        return []

    async def list_mcp_resources(self) -> list[dict]:
        return []

    async def get_skill(self, name: str) -> dict:
        raise self._missing(f"/skills/{name}")

    async def alive(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    @contextlib.asynccontextmanager
    async def request_context(self, *, session_id: str = "", tool_call_id: str = "", operation: str = ""):
        yield

    # Anything else the real client offers (desktop leases, MCP calls, the
    # media queue, skill installation) is deliberately absent: callers probe
    # with getattr(..., None), and a direct call fails with AttributeError,
    # which the tool reports like an unreachable Action Server.


# ---------------------------------------------------------------------------
# Provider wrapper: observation, hard limits, the fake model
# ---------------------------------------------------------------------------

@dataclass
class ProviderCall:
    """One provider request of the evaluated run, as the loop made it."""
    session_id: str
    message_id: str
    main: bool
    visible_tools: list[str]
    model_id: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    text: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""


class BudgetExceeded(RuntimeError):
    pass


class ObservedProvider:
    """Replaces ``agent.processor.stream_llm``; records each request and enforces the hard limits."""

    def __init__(self, *, main_session_id: str, upstream, max_steps: int = MAX_STEPS,
                 max_tokens: int = MAX_TOKENS):
        self.main_session_id = main_session_id
        self.upstream = upstream
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.calls: list[ProviderCall] = []
        self.limit_hit: str | None = None

    def main_steps(self) -> int:
        return len({call.message_id for call in self.calls if call.main})

    def total_tokens(self) -> int:
        return sum(usage_total(call.usage) for call in self.calls)

    async def __call__(self, **kwargs):
        ctx = kwargs["ctx"]
        main = ctx.session_id == self.main_session_id
        new_step = main and ctx.message_id not in {call.message_id for call in self.calls if call.main}
        if new_step and self.main_steps() >= self.max_steps:
            self.limit_hit = self.limit_hit or "steps"
        if self.total_tokens() >= self.max_tokens:
            self.limit_hit = self.limit_hit or "tokens"
        if self.limit_hit:
            from agent.driver import _current_lease
            lease = _current_lease.get()
            if lease is not None:
                lease.abort.set()
            yield {"type": "error", "error": BudgetExceeded(f"evaluation limit reached: {self.limit_hit}")}
            return
        tools = kwargs.get("tools") or {}
        call = ProviderCall(session_id=ctx.session_id, message_id=ctx.message_id, main=main,
                            visible_tools=sorted({getattr(info, "id", name) for name, info in tools.items()}),
                            model_id=str(kwargs.get("model_id") or ""))
        self.calls.append(call)
        async for event in self.upstream(**kwargs):
            kind = event.get("type")
            if kind == "tool_call":
                info = tools.get(event.get("tool"))
                call.tool_calls.append({
                    "wire": event.get("wire_tool") or event.get("tool"),
                    "tool": getattr(info, "id", None) or event.get("tool"),
                    "args": event.get("args"),
                    "arguments_raw": event.get("arguments_raw"),
                    "invalid": bool(event.get("invalid")),
                    "known": info is not None,
                })
            elif kind == "text_delta":
                call.text += event.get("text", "")
            elif kind == "finish":
                call.usage = dict(event.get("usage") or {})
            elif kind == "error":
                call.error = type(event.get("error")).__name__
            yield event


def usage_total(usage: dict) -> int:
    if not usage:
        return 0
    total = usage.get("total")
    if isinstance(total, (int, float)) and total:
        return int(total)
    return int(usage.get("input") or 0) + int(usage.get("output") or 0)


def call(tool: str, **args) -> dict:
    """A scripted tool call for the fake model."""
    return {"tool": tool, "args": args}


def say(text: str) -> dict:
    """A scripted final answer for the fake model."""
    return {"say": text}


class FakeModel:
    """A scripted provider. Each script entry is one step: a list of calls, or one ``say``.

    It behaves like a well-behaved model over the real tool exposure: a tool
    the step does not show is first revealed with ``capability_search`` (when
    that is offered), then called in the following step. String arguments may
    use ``{workdir}`` and the task's string ``extra`` values. An entry made
    with ``agent_behavior_tasks.dynamic`` is computed from the request (for
    ids only the conversation shows). When the script runs out it answers
    with text.
    """

    def __init__(self, script: list | None, variables: dict[str, str], env=None):
        self.script = list(script or [])
        self.variables = variables
        self.env = env
        self.position = 0
        self.searched: set[str] = set()
        self.counter = 0

    def _format(self, value):
        if isinstance(value, str):
            for key, replacement in self.variables.items():
                value = value.replace("{" + key + "}", replacement)
            return value
        if isinstance(value, list):
            return [self._format(item) for item in value]
        if isinstance(value, dict):
            return {key: self._format(item) for key, item in value.items()}
        return value

    def _call_event(self, wire: str, args: dict) -> dict:
        self.counter += 1
        raw = json.dumps(args, ensure_ascii=False)
        return {"type": "tool_call", "tool": wire, "wire_tool": wire, "args": args, "arguments_raw": raw,
                "call_id": f"fake-call-{self.counter}"}

    async def __call__(self, **kwargs):
        tools = kwargs.get("tools") or {}
        wire_of = {getattr(info, "id", name): name for name, info in tools.items()}
        prompt_chars = len(json.dumps(kwargs.get("messages") or [], ensure_ascii=False, default=str))
        prompt_chars += sum(len(part) for part in kwargs.get("system") or [])
        events = []
        entry = self.script[self.position] if self.position < len(self.script) else say("（脚本结束）")
        if isinstance(entry, dict) and "dynamic" in entry:
            entry = entry["dynamic"](kwargs, self.env)
        if isinstance(entry, dict) and "say" in entry:
            self.position += 1
            events.append({"type": "text_delta", "text": self._format(entry["say"])})
            reason = "stop"
        else:
            steps = entry if isinstance(entry, list) else [entry]
            hidden = [item["tool"] for item in steps if item["tool"] not in wire_of]
            unsearched = [tool for tool in hidden if tool not in self.searched]
            if unsearched and "capability_search" in wire_of:
                self.searched.update(unsearched)
                events.append(self._call_event(wire_of["capability_search"],
                                               {"query": " ".join(unsearched), "names": unsearched[:5]}))
            else:
                self.position += 1
                for item in steps:
                    events.append(self._call_event(wire_of.get(item["tool"], item["tool"]),
                                                   self._format(item["args"])))
            reason = "tool_calls"
        output_chars = sum(len(json.dumps(event, ensure_ascii=False)) for event in events)
        usage = {"input": prompt_chars // 3, "output": max(1, output_chars // 3)}
        usage["total"] = usage["input"] + usage["output"]
        for event in events:
            yield event
        yield {"type": "finish", "reason": reason, "usage": usage}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class RunMetrics:
    steps: int = 0
    tool_calls: int = 0
    tool_calls_per_step: list[int] = field(default_factory=list)
    question_calls: int = 0
    capability_search_calls: int = 0
    skill_loads: dict[str, int] = field(default_factory=dict)
    repeated_skill_loads: int = 0
    malformed_tool_calls: int = 0
    malformed_detail: list[dict] = field(default_factory=list)
    unmaterialized_tool_calls: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    subagent_requests: int = 0
    first_step_tools: list[str] = field(default_factory=list)


def _bad_json(raw) -> bool:
    if raw in (None, ""):
        return False
    if isinstance(raw, dict):
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return True
    return not isinstance(value, dict)


def extract_metrics(calls: list[ProviderCall], tool_parts: list[dict], registry_ids: set[str]) -> RunMetrics:
    """Per-run metrics from the provider log and the persisted tool parts.

    A step is one assistant message of the evaluated conversation (retries of
    the same request count once). Malformed calls are arguments that are not
    one JSON object, names that are no tool at all, and arguments the tool's
    schema rejected. A real tool called before it was revealed is counted
    separately as ``unmaterialized``: that is a visibility cost, not a format
    error.
    """
    metrics = RunMetrics()
    per_step: dict[str, list[dict]] = {}
    order: list[str] = []
    for request in calls:
        if not request.main:
            metrics.subagent_requests += 1
        else:
            if request.message_id not in per_step:
                per_step[request.message_id] = []
                order.append(request.message_id)
            if request.tool_calls:
                # A retried request replaces what the failed attempt emitted.
                per_step[request.message_id] = list(request.tool_calls)
        metrics.output_tokens += int((request.usage or {}).get("output") or 0)
        metrics.total_tokens += usage_total(request.usage)
    metrics.steps = len(order)
    for index, message_id in enumerate(order):
        tool_calls = per_step[message_id]
        metrics.tool_calls_per_step.append(len(tool_calls))
        if index == 0:
            metrics.first_step_tools = [entry["tool"] for entry in tool_calls]
        for entry in tool_calls:
            tool = entry["tool"]
            if _bad_json(entry.get("arguments_raw")):
                metrics.malformed_detail.append({"tool": tool, "kind": "arguments_not_json"})
            elif entry.get("invalid") or not entry.get("known", True):
                if tool in registry_ids:
                    metrics.unmaterialized_tool_calls += 1
                else:
                    metrics.malformed_detail.append({"tool": tool, "kind": "unknown_tool_name"})
            if tool in QUESTION_TOOLS:
                metrics.question_calls += 1
            elif tool == "capability_search":
                metrics.capability_search_calls += 1
    metrics.tool_calls = sum(metrics.tool_calls_per_step)
    for part in tool_parts:
        metadata = part.get("metadata") or {}
        if metadata.get("error_code") == "invalid_arguments":
            metrics.malformed_detail.append({"tool": part.get("tool"), "kind": "schema_rejected"})
        if part.get("tool") == "skill" and part.get("status") == "completed":
            name = str((part.get("input") or {}).get("skill") or "")
            if name and str(part.get("title") or "").startswith("Loaded skill"):
                metrics.skill_loads[name] = metrics.skill_loads.get(name, 0) + 1
    metrics.repeated_skill_loads = sum(count - 1 for count in metrics.skill_loads.values() if count > 1)
    metrics.malformed_tool_calls = len(metrics.malformed_detail)
    return metrics


# ---------------------------------------------------------------------------
# Runtime set-up
# ---------------------------------------------------------------------------

class Patches:
    """Attribute replacement with exact restoration (the harness runs outside pytest)."""

    def __init__(self):
        self._saved: list[tuple[Any, str, Any]] = []

    def set(self, target, name: str, value) -> None:
        if isinstance(target, str):
            import importlib
            target = importlib.import_module(target)
        self._saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def restore(self) -> None:
        while self._saved:
            target, name, value = self._saved.pop()
            setattr(target, name, value)


@dataclass
class EvalOptions:
    model: str | None = None
    exposure: str = "portable"
    tuning: tuple[str, ...] = ()
    fake_model: bool = False
    repeats: int = DEFAULT_REPEATS
    max_steps: int = MAX_STEPS
    max_tokens: int = MAX_TOKENS
    run_timeout: float = RUN_TIMEOUT_SECONDS
    label: str = ""
    keep_dirs: bool = False
    log_level: int = logging.CRITICAL


def evaluation_config(options: EvalOptions):
    """The deployment's config with the evaluated switches applied (a copy; nothing is written)."""
    from core.config import AgentTuningConfig, MemoryConfig, ProviderConfig, get_config
    config = get_config().model_copy(deep=True)
    if options.fake_model:
        config.provider = {**config.provider, "openai": ProviderConfig(
            api_key="fake-model-no-calls", base_url="https://fake-model.invalid/v1")}
        config.model = options.model or "openai/fake-eval-model"
        config.models = []
    elif options.model:
        config.model = options.model
    config.tool_exposure.mode = options.exposure
    tuning = {name: False for name, info in AgentTuningConfig.model_fields.items() if info.annotation is bool}
    for name in options.tuning:
        if name not in tuning:
            raise SystemExit(f"Unknown agent_tuning switch {name!r}; known: {', '.join(sorted(tuning))}")
        tuning[name] = True
    config.agent_tuning = AgentTuningConfig(**tuning, workspaces=[])
    memory = {name: False for name, info in MemoryConfig.model_fields.items() if info.annotation is bool}
    config.memory = config.memory.model_copy(update=memory)
    return config


def install_runtime(config, patches: Patches) -> None:
    """Side effects outside one run: config, titles, suggestions, wakes, snapshots."""
    import core.config
    from agent import loop

    async def nothing(*_args, **_kwargs):
        return None

    async def no_suggestions(*_args, **_kwargs):
        return []

    patches.set(core.config, "_config", config)
    patches.set(loop, "_ensure_title", nothing)
    patches.set("agent.suggestions", "generate_suggestions", no_suggestions)
    patches.set("agent.inbox", "schedule_inbox_wake", lambda *_args, **_kwargs: None)
    patches.set(loop.snapshot, "track", nothing)
    patches.set("sandbox.entitlement", "subscription_sandbox_enabled", lambda: False)
    patches.set("assistant.results", "on_execution_result_committed", nothing)


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

@dataclass
class TaskEnv:
    """What a task's setup and judge see of one run."""
    run_dir: Path
    sandbox: LocalSandbox
    user_id: str = ""
    workspace_id: str = ""
    project_id: str = ""
    project_slug: str = ""
    session_id: str = ""
    #: String values a fake script may use as ``{name}``.
    extra: dict = field(default_factory=dict)
    #: Anything else the setup leaves for the judge.
    state: dict = field(default_factory=dict)

    @property
    def workdir(self) -> str:
        """The session's working directory, as the model sees it."""
        return f"/workspace/{self.project_slug}"

    @property
    def project_dir(self) -> Path:
        """The same directory on the host."""
        return self.sandbox.root / self.project_slug

    def write(self, relative: str, content: str | bytes) -> Path:
        target = self.project_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target

    def read(self, relative: str) -> str:
        return (self.project_dir / relative).read_text(encoding="utf-8")


@dataclass
class RunView:
    """Everything a judge may inspect after a run."""
    env: TaskEnv
    metrics: RunMetrics
    tool_parts: list[dict]
    provider_calls: list[ProviderCall]
    final_text: str
    pending_questions: list[dict]
    finish: str | None

    def calls_of(self, tool: str) -> list[dict]:
        return [part for part in self.tool_parts if part.get("tool") == tool]

    def completed(self, tool: str) -> list[dict]:
        return [part for part in self.calls_of(tool) if part.get("status") == "completed"]


@dataclass
class Verdict:
    passed: bool | None
    detail: str = ""


async def _fresh_database(run_dir: Path):
    """A new SQLite store built the way desktop mode builds one (``db.base.ensure_engine``)."""
    import db.base as database
    import db.models  # noqa: F401  (registers every table)
    from agent.schema import _upgrade_sqlite_subagent_schema
    await database.close_engine()
    engine = database.init_engine(f"sqlite+aiosqlite:///{run_dir / 'eval.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
        for upgrade in (database._upgrade_desktop_billing_columns, database._upgrade_desktop_skill_store_columns,
                        database._upgrade_desktop_trajectory_columns, database._retire_desktop_trajectory_tables,
                        database._import_media_template_links, database._index_desktop_metadata_sync,
                        database._upgrade_desktop_message_center_columns, database._upgrade_desktop_memory_columns,
                        database._upgrade_desktop_assistant_columns, database._ensure_single_user_legacy_tables,
                        _upgrade_sqlite_subagent_schema, database._drop_assistant_evidence_triggers):
            await connection.run_sync(upgrade)


class _ErrorCapture(logging.Handler):
    """Keeps the platform's ERROR lines of one run (the loop logs and survives most failures)."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.name}: {record.getMessage()[:400]}")


def quiet_platform_logs(level: int, capture: logging.Handler | None = None) -> None:
    """OpenBox loggers are DEBUG and do not propagate (core/log.py); lower them and listen for errors."""
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if not name.startswith("openbox") or not isinstance(logger, logging.Logger):
            continue
        # The logger must still pass ERROR records to the capture; its own
        # stderr handler is what gets quieter.
        logger.setLevel(min(level, logging.ERROR))
        for handler in list(logger.handlers):
            if isinstance(handler, _ErrorCapture):
                if handler is not capture:
                    logger.removeHandler(handler)
            else:
                handler.setLevel(level)
        if capture is not None and capture not in logger.handlers:
            logger.addHandler(capture)


def _log_levels() -> dict[str, tuple[int, list[tuple[logging.Handler, int]]]]:
    return {name: (logger.level, [(handler, handler.level) for handler in logger.handlers])
            for name, logger in list(logging.root.manager.loggerDict.items())
            if name.startswith("openbox") and isinstance(logger, logging.Logger)}


def _restore_log_levels(saved: dict) -> None:
    """Put OpenBox loggers back as core/log.py left them (DEBUG, handler unfiltered)."""
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if not name.startswith("openbox") or not isinstance(logger, logging.Logger):
            continue
        level, handlers = saved.get(name, (logging.DEBUG, []))
        logger.setLevel(level)
        known = dict(handlers)
        for handler in list(logger.handlers):
            if isinstance(handler, _ErrorCapture):
                logger.removeHandler(handler)
            else:
                handler.setLevel(known.get(handler, logging.NOTSET))


async def _seed_normal(env: TaskEnv, task, model: str) -> None:
    from db.repository.user_repo import PgUserRepo
    from project.workspace import create_project
    from session.session import create_session
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(id=f"eval-{suffix}", username=f"eval-{suffix}", password_hash="unused")
    env.user_id, env.workspace_id = user["id"], user["default_workspace_id"]
    project = await create_project(env.user_id, env.workspace_id, task.project_name, slug=task.project_slug,
                                   description=task.project_description or None)
    env.project_id, env.project_slug = project.id, project.slug
    session = await create_session(model=model, user_id=env.user_id, workspace_id=env.workspace_id,
                                   project_id=env.project_id, title=task.id, visibility="private",
                                   light_entry=task.light_entry)
    env.session_id = session.id
    env.project_dir.mkdir(parents=True, exist_ok=True)


async def _tool_parts(session_id: str) -> list[dict]:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    async with get_db_session() as db:
        rows = (await db.execute(select(Part.data, Part.canonical_tool_id).where(
            Part.session_id == session_id, Part.type == "tool").order_by(Part.created_at, Part.id))).all()
    parts = []
    for data, canonical in rows:
        entry = dict(data or {})
        if canonical:
            entry["tool"] = canonical
        parts.append(entry)
    return parts


async def _pending_questions(session_id: str) -> list[dict]:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.question import QuestionCheckpoint
    async with get_db_session() as db:
        rows = (await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == session_id).order_by(QuestionCheckpoint.created_at))).all()
    return [{"id": row.id, "status": row.status, "questions": row.questions, "answers": row.answers,
             "continuation": row.continuation} for row in rows]


async def _final_text(session_id: str, user_id: str) -> tuple[str, str | None]:
    from session.session import get_messages
    messages = await get_messages(session_id, user_id=user_id)
    for message in reversed(messages):
        if message.role == "assistant":
            text = "".join(getattr(part, "text", "") or "" for part in message.parts
                           if getattr(part, "type", "") == "text")
            return text, message.finish
    return "", None


def _registry_ids() -> set[str]:
    from tool.registry import list_tools
    return {tool.id for tool in list_tools()}


def ensure_builtin_tools() -> None:
    """Register the platform's built-in tools as ``main.py`` does (custom project tools are not loaded)."""
    from tool.registry import get_tool, register_builtin_tools
    if get_tool("bash") is None:
        register_builtin_tools(load_custom=False)


async def run_task_once(task, options: EvalOptions, repeat: int, *, upstream=None) -> dict:
    """One run of ``task``: fresh database and sandbox, the real loop, the task's judge."""
    from agent import processor
    from agent.driver import reserve_run
    from core.config import get_config
    from sandbox import sandbox_manager
    from session.session import create_user_message

    record: dict[str, Any] = {"task": task.id, "category": task.category, "repeat": repeat,
                              "judge": "human" if task.judge == "human" else "auto"}
    if task.skip_reason:
        return {**record, "skipped": task.skip_reason}
    run_dir = Path(tempfile.mkdtemp(prefix=f"openbox-agent-eval-{task.id}-"))
    sandbox = LocalSandbox(run_dir / "workspace")
    env = TaskEnv(run_dir=run_dir, sandbox=sandbox)
    patches = Patches()
    lease = None
    capture = _ErrorCapture()
    try:
        await _fresh_database(run_dir)
        quiet_platform_logs(options.log_level, capture)
        model = get_config().model
        if task.session_kind == "assistant":
            from tests.manual.agent_behavior_tasks import seed_assistant
            await seed_assistant(env, task, model)
        else:
            await _seed_normal(env, task, model)
        if task.setup is not None:
            result = task.setup(env)
            if asyncio.iscoroutine(result):
                await result

        async def local_client(session_id, *_args, **_kwargs):
            return sandbox if session_id == env.session_id else None
        patches.set(sandbox_manager, "get_client", local_client)
        if upstream is None:
            if options.fake_model:
                variables = {"workdir": env.workdir, **{key: value for key, value in env.extra.items()
                                                         if isinstance(value, str)}}
                upstream_stream = FakeModel(task.fake_script, variables, env)
            else:
                from agent.llm import stream_llm as upstream_stream
        else:
            upstream_stream = upstream
        observed = ObservedProvider(main_session_id=env.session_id, upstream=upstream_stream,
                                    max_steps=options.max_steps, max_tokens=options.max_tokens)
        patches.set(processor, "stream_llm", observed)

        from agent.loop import run_loop
        quiet_platform_logs(options.log_level, capture)
        started = time.monotonic()
        error = None
        if task.session_kind == "assistant":
            from agent.inbox import accept_inbox_item
            await accept_inbox_item(session_id=env.session_id, user_id=env.user_id, delivery="followup",
                                    prompt=task.prompt, agent="assistant", origin="human",
                                    origin_ref={"actor_user_id": env.user_id})
            runner = run_loop(env.session_id, user_id=env.user_id)
        else:
            lease = await reserve_run(env.session_id, env.user_id)
            fence = (env.session_id, lease.run_id, lease.generation)
            await create_user_message(env.session_id, task.prompt, model=model, user_id=env.user_id,
                                      run_fence=fence, bind_trigger=True, origin="human",
                                      origin_ref={"actor_user_id": env.user_id, "entrypoint": "session_prompt"})
            runner = run_loop(env.session_id, user_id=env.user_id, lease=lease)
        try:
            await asyncio.wait_for(runner, options.run_timeout)
        except asyncio.TimeoutError:
            error = f"run exceeded {options.run_timeout:.0f}s"
        except Exception as exc:  # the record says what broke; the suite goes on
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
        wall = time.monotonic() - started

        parts = await _tool_parts(env.session_id)
        metrics = extract_metrics(observed.calls, parts, _registry_ids())
        final_text, finish = await _final_text(env.session_id, env.user_id)
        view = RunView(env=env, metrics=metrics, tool_parts=parts, provider_calls=observed.calls,
                       final_text=final_text, pending_questions=await _pending_questions(env.session_id),
                       finish=finish)
        if task.judge == "human":
            verdict = Verdict(None, "pending human judgement")
        else:
            try:
                verdict = task.judge(view)
                if asyncio.iscoroutine(verdict):
                    verdict = await verdict
            except Exception as exc:
                verdict = Verdict(False, f"judge raised {type(exc).__name__}: {exc}")
        if observed.limit_hit and verdict.passed is not False:
            # Over budget is not done, whatever the artefacts say (25.5 预算).
            verdict = Verdict(False, f"{observed.limit_hit} limit reached (judge alone: {verdict.passed}, "
                                     f"{verdict.detail})")
        record.update({
            "completed": verdict.passed,
            "judge_detail": verdict.detail,
            **({"rubric": task.rubric} if task.rubric else {}),
            "metrics": asdict(metrics),
            "wall_seconds": round(wall, 3),
            "limit_hit": observed.limit_hit,
            "error": error,
            "finish": finish,
            "final_text": final_text[:4000],
            "tools": [entry["tool"] for request in observed.calls if request.main for entry in request.tool_calls],
            "models_used": sorted({request.model_id for request in observed.calls if request.model_id}),
            "tool_trace": [{"tool": part.get("tool"), "status": part.get("status"),
                            "input": json.dumps(part.get("input"), ensure_ascii=False, default=str)[:300],
                            "result": str(part.get("output") or part.get("error") or "")[:300]}
                           for part in parts[:60]],
            "platform_errors": capture.lines[:20],
        })
        if options.keep_dirs:
            record["run_dir"] = str(run_dir)
        return record
    finally:
        quiet_platform_logs(options.log_level)
        patches.restore()
        if lease is not None:
            try:
                await lease.release(session_status="idle")
            except Exception:
                pass
        from db.base import close_engine
        await close_engine()
        if not options.keep_dirs:
            shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suite, summary, comparison
# ---------------------------------------------------------------------------

def select_tasks(tasks, selectors: list[str] | None):
    if not selectors:
        return list(tasks)
    wanted = set(selectors)
    chosen = [task for task in tasks if task.id in wanted or task.category in wanted]
    unknown = wanted - {task.id for task in chosen} - {task.category for task in chosen}
    if unknown:
        raise SystemExit(f"Unknown task or category: {', '.join(sorted(unknown))}")
    return chosen


async def run_suite(tasks, options: EvalOptions, output: Path | None = None, *, upstream=None) -> dict:
    import core.config
    from core.config import get_config
    os.environ["TRAJECTORY_RECORDING_ENABLED"] = "false"
    # Synthetic workspaces own no credits: meter the calls (shadow), never refuse them.
    previous_billing = os.environ.get("BILLING_MODE")
    os.environ["BILLING_MODE"] = "shadow"
    previous_cwd = Path.cwd()
    patches = Patches()
    import tool.registry  # noqa: F401  (its logger exists before the registration lines)
    saved_logs = _log_levels()
    result = {
        "label": options.label,
        "fake_model": options.fake_model,
        "exposure": options.exposure,
        "tuning": sorted(options.tuning),
        "repeats": options.repeats,
        "limits": {"steps": options.max_steps, "tokens": options.max_tokens},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": [],
    }
    try:
        os.chdir(BACKEND)  # project Skills are found relative to the backend checkout
        original = core.config._config
        config = evaluation_config(options)  # loads the deployment config if nothing had yet
        core.config._config = original  # so the patch below restores exactly what was there
        install_runtime(config, patches)
        quiet_platform_logs(options.log_level)
        ensure_builtin_tools()
        quiet_platform_logs(options.log_level)
        result["model"] = get_config().model
        for task in tasks:
            for repeat in range(1, options.repeats + 1):
                try:
                    record = await run_task_once(task, options, repeat, upstream=upstream)
                except Exception as exc:  # a harness or setup failure; the round goes on
                    record = {"task": task.id, "category": task.category, "repeat": repeat,
                              "harness_error": f"{type(exc).__name__}: {str(exc)[:500]}"}
                result["runs"].append(record)
                print(json.dumps({key: record.get(key) for key in (
                    "task", "repeat", "completed", "skipped", "limit_hit", "error", "harness_error")}
                    | {"steps": (record.get("metrics") or {}).get("steps"),
                       "tokens": (record.get("metrics") or {}).get("total_tokens")},
                    ensure_ascii=False), flush=True)
                if output is not None:
                    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                if task.skip_reason:
                    break
    finally:
        patches.restore()
        _restore_log_levels(saved_logs)
        os.chdir(previous_cwd)
        if previous_billing is None:
            os.environ.pop("BILLING_MODE", None)
        else:
            os.environ["BILLING_MODE"] = previous_billing
    result["aggregate"] = aggregate(result["runs"])
    if output is not None:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def aggregate(runs: list[dict]) -> dict:
    """Round totals the decision rules use; skipped runs are left out."""
    measured = [run for run in runs if "metrics" in run]
    judged = [run for run in measured if run.get("completed") is not None]
    steps = sum(run["metrics"]["steps"] for run in measured)
    calls = sum(run["metrics"]["tool_calls"] for run in measured)
    questions = sum(run["metrics"]["question_calls"] for run in measured)
    tokens = sum(run["metrics"]["total_tokens"] for run in measured)
    count = len(measured) or 1
    return {
        "runs": len(measured),
        "judged": len(judged),
        "pending_human": sum(1 for run in measured if run.get("completed") is None),
        "completed": sum(1 for run in judged if run["completed"]),
        "completion_rate": (sum(1 for run in judged if run["completed"]) / len(judged)) if judged else None,
        "steps": steps,
        "questions": questions,
        "steps_plus_questions_per_run": (steps + questions) / count,
        "tool_calls": calls,
        "mean_tool_calls_per_step": calls / steps if steps else 0.0,
        "capability_search_calls": sum(run["metrics"]["capability_search_calls"] for run in measured),
        "repeated_skill_loads": sum(run["metrics"]["repeated_skill_loads"] for run in measured),
        "malformed_tool_calls": sum(run["metrics"]["malformed_tool_calls"] for run in measured),
        "unmaterialized_tool_calls": sum(run["metrics"]["unmaterialized_tool_calls"] for run in measured),
        "output_tokens": sum(run["metrics"]["output_tokens"] for run in measured),
        "total_tokens": tokens,
        "total_tokens_per_run": tokens / count,
        "wall_seconds": round(sum(run.get("wall_seconds", 0.0) for run in measured), 3),
        "limit_hits": sum(1 for run in measured if run.get("limit_hit")),
        "errors": sum(1 for run in measured if run.get("error")),
        "runs_with_platform_errors": sum(1 for run in measured if run.get("platform_errors")),
        "harness_errors": sum(1 for run in runs if run.get("harness_error")),
        "models_used": sorted({model for run in measured for model in run.get("models_used", [])}),
    }


def summary_table(result: dict) -> str:
    """One line per task, then the round totals."""
    by_task: dict[str, list[dict]] = {}
    for run in result["runs"]:
        by_task.setdefault(run["task"], []).append(run)
    header = f"{'task':34} {'done':>6} {'steps':>6} {'calls/st':>8} {'ask':>4} {'csrch':>5} " \
             f"{'skill+':>6} {'bad':>4} {'out_tok':>8} {'tot_tok':>9} {'secs':>7}"
    lines = [f"model={result['model']} exposure={result['exposure']} tuning={','.join(result['tuning']) or '-'}"
             f" fake={result['fake_model']} label={result.get('label') or '-'}", header, "-" * len(header)]
    for task_id, runs in by_task.items():
        if all("skipped" in run for run in runs):
            lines.append(f"{task_id:34} skipped: {runs[0]['skipped']}")
            continue
        measured = [run for run in runs if "metrics" in run]
        if not measured:
            lines.append(f"{task_id:34} harness error: {runs[0].get('harness_error')}")
            continue
        judged = [run for run in measured if run.get("completed") is not None]
        done = f"{sum(1 for run in judged if run['completed'])}/{len(judged)}" if judged else "human"
        mean = lambda key: statistics.mean(run["metrics"][key] for run in measured)  # noqa: E731
        steps = sum(run["metrics"]["steps"] for run in measured)
        calls = sum(run["metrics"]["tool_calls"] for run in measured)
        lines.append(
            f"{task_id:34} {done:>6} {mean('steps'):6.1f} {(calls / steps if steps else 0):8.2f} "
            f"{mean('question_calls'):4.1f} {mean('capability_search_calls'):5.1f} "
            f"{mean('repeated_skill_loads'):6.1f} {sum(run['metrics']['malformed_tool_calls'] for run in measured):4d} "
            f"{mean('output_tokens'):8.0f} {mean('total_tokens'):9.0f} "
            f"{statistics.mean(run.get('wall_seconds', 0) for run in measured):7.1f}")
    total = result.get("aggregate") or aggregate(result["runs"])
    rate = "n/a" if total["completion_rate"] is None else f"{total['completion_rate']:.0%}"
    lines.append("-" * len(header))
    lines.append(f"runs={total['runs']} completed={total['completed']}/{total['judged']} ({rate}) "
                 f"pending_human={total['pending_human']} steps+questions/run={total['steps_plus_questions_per_run']:.2f} "
                 f"calls/step={total['mean_tool_calls_per_step']:.2f} malformed={total['malformed_tool_calls']} "
                 f"tokens/run={total['total_tokens_per_run']:.0f} limit_hits={total['limit_hits']} errors={total['errors']}"
                 f" platform_errors={total['runs_with_platform_errors']}")
    others = [model for model in total["models_used"] if model != result["model"]]
    if others:
        lines.append(f"WARNING: some requests used {others}, not {result['model']} (agent or session override)")
    return "\n".join(lines)


def _comparable(result: dict, task_ids: set[str]) -> dict:
    return aggregate([run for run in result["runs"] if run["task"] in task_ids])


def compare(baselines: list[dict], candidate: dict, *, a1: bool = False) -> list[dict]:
    """The decision rules of 25.5 (initial values), on the tasks every file measured.

    With one baseline round, "lower" and "higher" are that round.
    """
    if not 1 <= len(baselines) <= 2:
        raise ValueError("compare takes one or two baseline rounds")
    measured = lambda result: {run["task"] for run in result["runs"] if "metrics" in run}  # noqa: E731
    task_ids = measured(candidate)
    for baseline in baselines:
        task_ids &= measured(baseline)
    base = [_comparable(baseline, task_ids) for baseline in baselines]
    cand = _comparable(candidate, task_ids)
    rules = []

    rates = [round_["completion_rate"] for round_ in base]
    if cand["completion_rate"] is None or any(rate is None for rate in rates):
        rules.append({"rule": "completion", "passed": None,
                      "detail": "no judged runs (fill in the human verdicts first)"})
    else:
        lower = min(rates)
        rules.append({"rule": "completion", "passed": cand["completion_rate"] >= lower,
                      "detail": f"candidate {cand['completed']}/{cand['judged']} ({cand['completion_rate']:.1%})"
                                f" >= lower baseline {lower:.1%}"})
    higher = max(round_["steps_plus_questions_per_run"] for round_ in base)
    rules.append({"rule": "steps_plus_questions", "passed": cand["steps_plus_questions_per_run"] <= higher,
                  "detail": f"candidate {cand['steps_plus_questions_per_run']:.2f}/run <= higher baseline {higher:.2f}/run"})
    if a1:
        higher_calls = max(round_["mean_tool_calls_per_step"] for round_ in base)
        rules.append({"rule": "a1_tool_calls_per_step", "passed": cand["mean_tool_calls_per_step"] > higher_calls,
                      "detail": f"candidate {cand['mean_tool_calls_per_step']:.3f} > higher baseline {higher_calls:.3f}"})
    rules.append({"rule": "malformed_tool_calls", "passed": cand["malformed_tool_calls"] == 0,
                  "detail": f"candidate {cand['malformed_tool_calls']} malformed calls (must be 0)"})
    higher_tokens = max(round_["total_tokens_per_run"] for round_ in base)
    # Integer percentages: 1.15 * x drifts below 115% of x in floating point.
    rules.append({"rule": "total_tokens", "passed": cand["total_tokens_per_run"] * 100 <= higher_tokens * 115,
                  "detail": f"candidate {cand['total_tokens_per_run']:.0f}/run <= 115% of {higher_tokens:.0f}/run"
                            f" ({1.15 * higher_tokens:.0f})"})
    if base and cand["total_tokens"] > 1.5 * max(round_["total_tokens"] for round_ in base):
        rules.append({"rule": "budget_watch", "passed": None,
                      "detail": "round used more than 1.5x a baseline round's tokens: stop and look at why (25.5)"})
    if cand["pending_human"]:
        rules.append({"rule": "pending_human", "passed": None,
                      "detail": f"{cand['pending_human']} human-judged runs have no verdict and were left out"})
    return rules


def format_rules(rules: list[dict]) -> str:
    mark = {True: "PASS", False: "FAIL", None: "NOTE"}
    return "\n".join(f"{mark[rule['passed']]:4}  {rule['rule']:24} {rule['detail']}" for rule in rules)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="Model id to evaluate (default: the configured chat model)")
    parser.add_argument("--exposure", default="portable", choices=["portable", "legacy_eager"],
                        help="tool_exposure.mode for the run")
    parser.add_argument("--tuning", action="append", default=[], metavar="SWITCH",
                        help="Turn one agent_tuning switch on (repeatable), e.g. relaxed_prompt")
    parser.add_argument("--tasks", nargs="*", help="Task ids or categories (default: all)")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--run-timeout", type=float, default=RUN_TIMEOUT_SECONDS)
    parser.add_argument("--label", default="")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / "openbox-agent-eval.json")
    parser.add_argument("--fake-model", action="store_true", help="Scripted fake model; no provider call")
    parser.add_argument("--keep-dirs", action="store_true", help="Keep each run's temporary directory")
    parser.add_argument("--verbose", action="store_true", help="Show the platform's warnings")
    parser.add_argument("--list", action="store_true", help="List the tasks and exit")
    parser.add_argument("--compare", nargs="+", type=Path, metavar="RESULT",
                        help="BASELINE [BASELINE2] CANDIDATE: apply the 25.5 decision rules")
    parser.add_argument("--a1", action="store_true", help="With --compare: also require more tool calls per step")
    args = parser.parse_args(argv)

    if args.compare:
        if len(args.compare) not in (2, 3):
            parser.error("--compare takes BASELINE [BASELINE2] CANDIDATE")
        loaded = [json.loads(path.read_text()) for path in args.compare]
        rules = compare(loaded[:-1], loaded[-1], a1=args.a1)
        print(format_rules(rules))
        return 0 if all(rule["passed"] is not False for rule in rules) else 1

    from tests.manual.agent_behavior_tasks import TASKS
    tasks = select_tasks(TASKS, args.tasks)
    if args.list:
        for task in tasks:
            print(f"{task.id:34} {task.category:14} {'human' if task.judge == 'human' else 'auto':5} "
                  f"{('skip: ' + task.skip_reason) if task.skip_reason else ''}")
        return 0
    if not args.fake_model:
        from dotenv import load_dotenv
        load_dotenv(BACKEND / ".env")
    logging.getLogger().setLevel(logging.ERROR)
    options = EvalOptions(model=args.model, exposure=args.exposure, tuning=tuple(args.tuning),
                          fake_model=args.fake_model, repeats=args.repeats, max_steps=args.max_steps,
                          max_tokens=args.max_tokens, run_timeout=args.run_timeout, label=args.label,
                          keep_dirs=args.keep_dirs, log_level=logging.WARNING if args.verbose else logging.CRITICAL)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_suite(tasks, options, args.output))
    print(summary_table(result))
    print(f"results: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
