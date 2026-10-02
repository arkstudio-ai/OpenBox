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
_ONLY_CURRENT = re.compile(r"只(?:用|看|根据|依据).*(?:本轮|这轮|当前材料|这条消息)|不要(?:查|用|读取|检索).*(?:历史|记忆)|仅(?:用|根据).*(?:本轮|当前)|only.*(?:this message|current input)|do not.*(?:memory|history)", re.I)


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
    explicit = {"memory": bool(_MEMORY.search(direct)), "task": bool(_TASK.search(direct))}
    if any(explicit.values()):
        for route, needed in explicit.items():
            result[route] = {"needed": needed, "choice": ("retrieve" if route == "memory" else "read") if needed else "skip",
                             "reason_code": "explicit_rule", "confidence": None, "probabilities": None}
        result["reason_code"] = "explicit_rule"
        result["rule"] = "explicit_read_request"
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
                result[route] = {**answer, "threshold": threshold, "needed": confident and answer["choice"] == read_choice, "reason_code": code}
            result["reason_code"] = "fallback" if any(result[r]["reason_code"] in {"unknown_choice", "low_confidence"} for r in ("memory", "task")) else "jev_retrieve" if any(result[r]["needed"] for r in ("memory", "task")) else "jev_skip"
        except MemoryProviderError as exc:
            result["reason_code"] = exc.code
            result["fallback"] = "assistant_supplement_available"
            for route in ("memory", "task"):
                result[route]["reason_code"] = "fallback"
    else:
        result["reason_code"] = "disabled"
        result["fallback"] = "assistant_supplement_available"
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
