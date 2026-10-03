"""One bounded, two-question routing call; no model can widen SQL access."""
import re
import time
import uuid

from memory.providers.common import MemoryProviderError
from memory.providers.jev import evaluate_context_needs
from memory.redaction import json_hash, redact_text

ROUTING_SCHEMA = "memory-task-choice-v1"
_MEMORY = re.compile(r"记得|记忆|偏好|以前|之前|上次|历史|过去|曾经|原话|旧决定|prior|previous|remember|preference|history", re.I)
_TASK = re.compile(r"任务.*(?:状态|进度|完成|运行)|(?:状态|进度).*任务|会话.*(?:状态|运行)|项目.*进度|当前.*(?:任务|运行)|task.*status|running.*session|project.*progress", re.I)
# A global limit to this turn's own material switches every lookup off.
_ONLY_CURRENT = re.compile(r"只(?:用|看|根据|依据).*(?:本轮|这轮|当前材料|这条消息)|仅(?:用|根据).*(?:本轮|当前)|only.*(?:this message|current input)", re.I)
# A prohibition covers its own clause only: "不要查历史记忆，请看当前任务状态" forbids
# memory and still asks for task state; "我之前说过不要吃辣" forbids nothing.
_CLAUSE = r"[^，。；;,.!?！？\n]*"
_NO_MEMORY = re.compile(r"(?:不要|别|不用|无需|不需要)再?(?:查|用|看|读取|检索|调用|参考|翻)" + _CLAUSE + r"(?:历史|记忆|以前|之前|过去)"
                        r"|(?:do not|don't|dont)\s+(?:use|check|search|read|look at)" + _CLAUSE + r"(?:memory|memories|history)", re.I)
_NO_TASK = re.compile(r"(?:不要|别|不用|无需|不需要)再?(?:查|用|看|读取|检索|调用)" + _CLAUSE + r"(?:任务|进度|运行状态)"
                      r"|(?:do not|don't|dont)\s+(?:use|check|search|read|look at)" + _CLAUSE + r"(?:task|progress)", re.I)


def _explicit_needs(direct: str) -> dict | None:
    """Explicit asks and prohibitions, clause by clause; None when there are none."""
    clauses = [clause for clause in re.split(r"[，。；;,.!?！？\n]", direct) if clause.strip()]
    no_memory = any(_NO_MEMORY.search(clause) for clause in clauses)
    no_task = any(_NO_TASK.search(clause) for clause in clauses)
    asks_memory = any(_MEMORY.search(clause) and not _NO_MEMORY.search(clause) for clause in clauses)
    asks_task = any(_TASK.search(clause) and not _NO_TASK.search(clause) for clause in clauses)
    if not (no_memory or no_task or asks_memory or asks_task):
        return None
    return {"memory": asks_memory and not no_memory, "task": asks_task and not no_task}


def _direct_input(utterance: str) -> str:
    # Rules cannot be triggered by fenced or quoted reference material.
    return "\n".join(line for line in re.sub(r"```[\s\S]*?```", "", utterance).splitlines()
                     if not line.lstrip().startswith(">"))


async def route_context_needs(utterance: str, scope, config, *, recent_context=(), evaluator=None) -> dict:
    started = time.monotonic()
    direct = _direct_input(utterance)
    limited = redact_text(utterance, config.route_input_max_chars)
    result = {"attempt_id": str(uuid.uuid4()), "schema_version": ROUTING_SCHEMA,
              "policy_version": config.policy_version, "model_requested": config.jev_model,
              "model": None, "called": False, "memory": {"needed": False, "choice": "skip"},
              "task": {"needed": False, "choice": "skip"}, "usage": {}, "reason_code": "jev_skip",
              "input_hash": json_hash({"utterance": limited}), "duration_ms": 0}
    if _ONLY_CURRENT.search(direct):
        result["reason_code"] = "explicit_rule"
        result["rule"] = "current_input_only"
        result["memory"]["reason_code"] = result["task"]["reason_code"] = "explicit_rule"
        return result
    explicit = _explicit_needs(direct)
    if explicit is not None:
        for route, needed in explicit.items():
            result[route] = {"needed": needed, "choice": ("retrieve" if route == "memory" else "read") if needed else "skip",
                             "reason_code": "explicit_rule", "confidence": None, "probabilities": None}
        result["reason_code"] = "explicit_rule"
        result["rule"] = "explicit_read_request" if any(explicit.values()) else "explicit_prohibition"
    elif config.enabled("route_jev", scope.actor_user_id):
        state = {"utterance": limited,
                 "recent_context": [{"role": entry.get("role", "user"), "text": redact_text(str(entry.get("text", "")), 400)}
                                    for entry in list(recent_context)[-2:]],
                 "allowed_routes": ["memory", "task_state"],
                 "allowed_spaces": [{"ref": "space_1", "kind": "current_project" if scope.project_id else "personal_workspace"}],
                 "policy_version": config.policy_version}
        try:
            result["called"] = True
            response = await (evaluator or evaluate_context_needs)(state, config)
            result["model"] = response["model"]
            tokens = response["usage"]["input_tokens"]
            result["usage"] = {**response["usage"], "model": response["model"],
                               "estimated_cost": None if config.jev_price_per_million is None else tokens * config.jev_price_per_million / 1_000_000,
                               "currency": config.jev_currency, "price_version": config.price_version}
            for route, key, threshold, read_choice in (("memory", "memory_needed", config.jev_memory_confidence, "retrieve"),
                    ("task", "task_needed", config.jev_task_confidence, "read")):
                answer = response["answers"][key]
                confident = answer["confidence"] >= threshold and answer["choice"] != "unknown"
                code = "unknown_choice" if answer["choice"] == "unknown" else "low_confidence" if not confident else "jev_skip" if answer["choice"] == "skip" else "jev_retrieve"
                # Unsure whether memory helps: a bounded, reranked retrieval costs far
                # less than the assistant searching on its own in another model turn.
                unsure = route == "memory" and not confident and answer["choice"] in {read_choice, "unknown"}
                needed = (confident and answer["choice"] == read_choice) or unsure
                result[route] = {**answer, "threshold": threshold, "needed": needed, "reason_code": code}
            result["reason_code"] = "fallback" if any(result[r]["reason_code"] in {"unknown_choice", "low_confidence"} for r in ("memory", "task")) else "jev_retrieve" if any(result[r]["needed"] for r in ("memory", "task")) else "jev_skip"
        except MemoryProviderError as exc:
            result["reason_code"] = exc.code
            result["fallback"] = "assistant_supplement_available"
            for route in ("memory", "task"):
                result[route]["reason_code"] = "fallback"
            # Without the router's answer, retrieve: cheap, bounded and reranked.
            result["memory"].update(needed=True, choice="retrieve")
    else:
        result["reason_code"] = "disabled"
        result["fallback"] = "assistant_supplement_available"
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
