"""Semantic read restrictions and open-ended information needs, in one request.

The gate sees only the current input. Prior text is used to contextualize search
AFTER permission is resolved, never sent speculatively to this gate. There is no
list of user expressions, industries or intents that can skip/authorize recall.
"""
import time
import uuid
from collections import OrderedDict
from copy import deepcopy

from memory.providers.common import MemoryProviderError
from memory.providers.jev import evaluate_read_needs
from memory.redaction import json_hash, redact_credentials

SCHEMA = "read-needs-v2"
_cache = OrderedDict()


async def route(utterance, scope, config, *, recent_context=(), visible_conversation_available=False, evaluator=None):
    from db.base import get_db_session
    from memory.versions import version
    from memory.dialogue_read import DialogueReadEvaluator
    dialogue = isinstance(evaluator, DialogueReadEvaluator)
    started = time.monotonic()
    result = {"attempt_id": str(uuid.uuid4()), "schema_version": SCHEMA, "policy_version": config.policy_version,
        "model_requested": config.read_router_model or config.jev_model, "model": None, "called": False, "usage": {}, "duration_ms": 0,
        "memory": {"needed": False, "choice": "unknown", "reason_code": "restriction_unresolved"},
        "task": {"needed": False, "choice": "unknown", "reason_code": "restriction_unresolved"},
        "memory_forbidden": True, "read_policy": "uncertain", "reason_code": "restriction_unresolved"}
    if dialogue:
        result.update(model_requested=evaluator.model, planner="dialogue", provider_attempts=0)
    # Truncating a request could remove its restriction. Do not classify a
    # prefix as if it were the user's full request.
    if len(utterance) > config.route_input_max_chars:
        result["reason_code"] = "restriction_input_budget"
        return result
    if not config.enabled("route_jev", scope.actor_user_id):
        result["reason_code"] = "semantic_router_disabled"
        return result
    async with get_db_session() as db:
        current_version = await version(db, scope.actor_user_id)
    from memory.providers.configured_read import cache_identity
    # The planner receives only the presence of prior context, never its
    # contents. Its decision can be reused across changed preceding replies;
    # contextual_query still binds the current reference text on every read.
    key = json_hash({"input": utterance, "context_available": bool(recent_context), "user": scope.actor_user_id,
        "visible_conversation_available": bool(visible_conversation_available),
        "workspace": scope.workspace_id, "project": scope.project_id, "projects": list(scope.project_ids),
        "acl": scope.acl_epoch, "content": current_version, "model": result["model_requested"],
        "schema": SCHEMA, "policy": config.policy_version, "memory_threshold": config.jev_memory_confidence,
        "task_threshold": config.jev_task_confidence,
        "provider_identity": "dialogue-read-v1" if dialogue else cache_identity(config) if config.read_router_model else config.jev_url})
    result["input_hash"] = key
    cached = _cache.get(key) if evaluator is None else None
    if cached and time.monotonic() - cached[0] < 60:
        return {**deepcopy(cached[1]), "attempt_id": result["attempt_id"], "called": False,
                "cache": "hit", "usage": {}, "duration_ms": round((time.monotonic() - started) * 1000)}
    state = {"current_request": redact_credentials(utterance), "schema": SCHEMA,
        "context_available": bool(recent_context), "project_in_view": bool(scope.project_id),
        "visible_conversation_available": bool(visible_conversation_available),
        "available_reads": ["personal_facts", "history", "knowledge", "assets", "task_state", "validated_experience"]}
    try:
        result["called"] = not dialogue
        from memory.providers.configured_read import evaluate as configured_read
        response = await (evaluator or (configured_read if config.read_router_model else evaluate_read_needs))(state, config)
        answers = response["answers"]
        policy = answers["read_policy"]
        typed_planner = response.get("classifier") in {"configured", "dialogue"}
        allowed_score = (policy["confidence"] if typed_planner
                         else policy.get("probabilities", {}).get("allowed", 0))
        allowed = (policy["choice"] == "allowed" and policy["confidence"] >= 0.8
                   and allowed_score >= 0.9)
        documents_only = (typed_planner
            and policy["choice"] == "documents_only" and policy["confidence"] >= .9
            and bool(policy.get("restriction_quote")))
        read_policy = ("allowed" if allowed else "uncertain") if policy["choice"] == "allowed" else policy["choice"]
        if policy["choice"] == "documents_only" and not documents_only:
            read_policy = "uncertain"
        result.update(model=response["model"], provider_model=response.get("provider_model"), classifier=response.get("classifier", "jev"),
            provider_attempts=response.get("provider_attempts", 1),
            protocol_errors=response.get("protocol_errors", []),
            usage=response["usage"], memory_forbidden=not (allowed or documents_only),
            read_policy=read_policy, read_policy_answer=policy, needs={key: answers[key]["noul"] for key in
                ("history_useful", "knowledge_useful", "assets_useful", "experience_useful")})
        if response.get("classifier") == "dialogue":
            # Planning is part of the already-accounted dialogue response. Do
            # not invent an additional Qwen/provider call or double bill usage.
            result.update(called=False, model_requested=response["model"], provider_attempts=0,
                          planner="dialogue", usage={})
        if "recent_context_useful" in answers:
            result["recent_context_useful"] = answers["recent_context_useful"]["noul"]
        if "time_intent" in answers:
            result["time_intent"] = answers["time_intent"]
        for name, threshold, read in (("memory", config.jev_memory_confidence, "retrieve"),
                                      ("task", config.jev_task_confidence, "read")):
            answer = answers[name + "_needed"]
            uncertain = answer["confidence"] < threshold or answer["choice"] == "unknown"
            needed = allowed and (answer["choice"] == read or uncertain and name == "memory")
            if name == "task" and read_policy == "no_history":
                needed = not uncertain and answer["choice"] == "read"
            result[name] = {**answer, "needed": needed, "reason_code":
                "restriction_unresolved" if read_policy == "uncertain" else "read_restricted" if not allowed and not needed
                else "bounded_uncertain_read" if uncertain else "semantic_read" if needed else "semantic_skip"}
        if allowed and max(result["needs"].get(key, 0) for key in ("history_useful", "knowledge_useful")) >= 0.4:
            result["memory"].update(needed=True, reason_code="semantic_read")
        if allowed and result["memory"]["needed"] and answers.get("search_query"):
            result["search_query"] = answers["search_query"]
        result["reason_code"] = "read_restricted" if not allowed else "semantic_read" if result["memory"]["needed"] else "semantic_skip"
        if evaluator is None:
            _cache[key] = (time.monotonic(), deepcopy(result))
            _cache.move_to_end(key)
            while len(_cache) > 256:
                _cache.popitem(last=False)
    except MemoryProviderError as exc:
        result.update(reason_code=exc.code, fallback="current_input_only_until_read_scope_resolved")
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
