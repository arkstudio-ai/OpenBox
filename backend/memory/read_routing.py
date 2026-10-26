"""Semantic read restrictions and open-ended information needs, in one request.

The gate sees only the current input. Prior text is used to contextualize search
AFTER permission is resolved, never sent speculatively to this gate. There is no
list of user expressions, industries or intents that can skip/authorize recall.
"""
import asyncio
import time
import uuid
from collections import OrderedDict
from copy import deepcopy

from memory.providers.common import MemoryProviderError
from memory.providers.jev import evaluate_read_needs
from memory.redaction import json_hash, redact_credentials

SCHEMA = "read-needs-v4"
_cache = OrderedDict()


def fallback_config(config):
    if config.read_router_model or not config.jev_fallback_model:
        return None
    return config.model_copy(update={"read_router_model": config.jev_fallback_model,
        "read_router_url": "", "read_router_timeout_seconds": config.jev_fallback_timeout_seconds})


def deadline(config):
    if config.read_router_model:
        return config.read_router_timeout_seconds
    return config.jev_timeout_seconds + (config.jev_fallback_timeout_seconds if config.jev_fallback_model else 0)


def unresolved_reason(result, config):
    if result["read_policy"] == "uncertain":
        return result["reason_code"]
    if result["read_policy"] in {"no_history", "current_only"}:
        policy = result.get("read_policy_answer", {})
        if (policy.get("confidence", 0) < .9
                or policy.get("probabilities", {}).get(result["read_policy"], 1) < .9):
            return "uncertain_read_scope"
    if result["read_policy"] == "allowed":
        if result.get("time_intent", {}).get("choice") == "uncertain":
            return "uncertain_time_scope"
        for name, threshold in (("memory", config.jev_memory_confidence), ("task", config.jev_task_confidence)):
            choice = result[name]
            if choice.get("choice") == "unknown" or choice.get("confidence", 0) < threshold:
                return "uncertain_" + name
    return None


async def route(utterance, scope, config, *, recent_context=(), visible_conversation_available=False, evaluator=None, entry="chat"):
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
    secondary = fallback_config(config) if evaluator is None else None
    # The planner receives only the presence of prior context, never its
    # contents. Its decision can be reused across changed preceding replies;
    # contextual_query still binds the current reference text on every read.
    key = json_hash({"input": utterance, "entry": entry, "context_available": bool(recent_context), "user": scope.actor_user_id,
        "visible_conversation_available": bool(visible_conversation_available),
        "workspace": scope.workspace_id, "project": scope.project_id, "projects": list(scope.project_ids),
        "acl": scope.acl_epoch, "content": current_version, "model": result["model_requested"],
        "schema": SCHEMA, "policy": config.policy_version, "memory_threshold": config.jev_memory_confidence,
        "task_threshold": config.jev_task_confidence,
        "fallback": [secondary.read_router_model, secondary.read_router_timeout_seconds, cache_identity(secondary)] if secondary else None,
        "provider_identity": "dialogue-read-v1" if dialogue else cache_identity(config) if config.read_router_model else config.jev_url})
    result["input_hash"] = key
    cached = _cache.get(key) if evaluator is None else None
    if cached and time.monotonic() - cached[0] < 60:
        return {**deepcopy(cached[1]), "attempt_id": result["attempt_id"], "called": False,
                "cache": "hit", "usage": {}, "duration_ms": round((time.monotonic() - started) * 1000)}
    state = {"current_request": redact_credentials(utterance), "schema": SCHEMA,
        "entry": entry,
        "context_available": bool(recent_context), "project_in_view": bool(scope.project_id),
        "visible_conversation_available": bool(visible_conversation_available),
        "available_reads": ["personal_facts", "history", "knowledge", "assets", "task_state", "validated_experience"]}
    try:
        result["called"] = not dialogue
        from memory.providers.configured_read import evaluate as configured_read
        timeout = config.read_router_timeout_seconds if config.read_router_model else config.jev_timeout_seconds
        async with asyncio.timeout(timeout):
            response = await (evaluator or (configured_read if config.read_router_model else evaluate_read_needs))(state, config)
        answers = response["answers"]
        policy = answers["read_policy"]
        typed_planner = response.get("classifier") in {"configured", "dialogue"}
        allowed_score = (policy["confidence"] if typed_planner
                         else policy.get("probabilities", {}).get("allowed", 0))
        allowed = (policy["choice"] == "allowed" and policy["confidence"] >= 0.8
                   and allowed_score >= 0.9)
        native_v3 = response.get("protocol") == "native-read-v3"
        native_scope_valid = True
        if native_v3:
            role, temporal = answers["restriction_role"], answers["time_scope"]
            expected = {"access": {"documents_only", "no_history", "current_only", "uncertain"},
                "action": {"allowed", "uncertain"}, "mentioned": {"allowed", "uncertain"},
                "withdrawn": {"allowed", "uncertain"}, "none": {"allowed", "uncertain"},
                "uncertain": {"uncertain"}}
            native_scope_valid = (policy["choice"] in expected[role["choice"]]
                and role["confidence"] >= .8 and role["probabilities"][role["choice"]] >= .9)
            if (policy["choice"] == "allowed" and role["choice"] in {"none", "action", "mentioned", "withdrawn"}
                    and role["confidence"] >= .9 and role["probabilities"][role["choice"]] >= .9):
                # The independently classified role establishes that no read
                # limit is active. The conditional scope answer must agree;
                # a restrictive/contradictory answer is never overridden.
                allowed = True
                result["policy_basis"] = "non_access_role_with_allowed_policy"
            # Native typed decisions cannot supply the exact time quotation
            # needed to bind a record-date filter. Resolve that scope through
            # the grounded planner before reading, never silently ignore it.
            if policy["choice"] in {"allowed", "documents_only"}:
                native_scope_valid &= (temporal["choice"] == "unrestricted"
                    and temporal["confidence"] >= .9 and temporal["probabilities"]["unrestricted"] >= .9)
            allowed &= native_scope_valid
        documents_only = (typed_planner
            and policy["choice"] == "documents_only" and policy["confidence"] >= .9
            and bool(policy.get("restriction_quote")))
        if native_v3 and policy["choice"] == "documents_only":
            documents_only = (native_scope_valid and policy["confidence"] >= .8
                              and policy["probabilities"]["documents_only"] >= .9)
        read_policy = ("allowed" if allowed else "uncertain") if policy["choice"] == "allowed" else policy["choice"]
        if policy["choice"] == "documents_only" and not documents_only:
            read_policy = "uncertain"
        if native_v3 and not native_scope_valid:
            read_policy = "uncertain"
        result.update(model=response["model"], provider_model=response.get("provider_model"), classifier=response.get("classifier", "jev"),
            provider_attempts=response.get("provider_attempts", 1),
            protocol_errors=response.get("protocol_errors", []),
            usage=response["usage"], memory_forbidden=not (allowed or documents_only),
            read_policy=read_policy, read_policy_answer=policy, needs={key: answers[key]["noul"] for key in
                ("history_useful", "knowledge_useful", "assets_useful", "experience_useful", "personal_useful") if key in answers})
        if native_v3:
            result.update(protocol=response["protocol"], restriction_role=answers["restriction_role"],
                          time_scope=answers["time_scope"])
            if native_scope_valid and answers["time_scope"]["choice"] == "unrestricted":
                result["time_intent"] = {"choice": "none", "expression": "", "confidence": temporal["confidence"]}
        if response.get("classifier") == "dialogue":
            # Planning is part of the already-accounted dialogue response. Do
            # not invent an additional Qwen/provider call or double bill usage.
            result.update(called=False, model_requested=response["model"], provider_attempts=0,
                          planner="dialogue", usage={})
        if "recent_context_useful" in answers:
            result["recent_context_useful"] = answers["recent_context_useful"]["noul"]
        if "time_intent" in answers:
            result["time_intent"] = answers["time_intent"]
        if entry == "voice" and response.get("classifier") == "configured" and answers.get("spoken_language"):
            result["spoken_language"] = answers["spoken_language"]
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
    except (MemoryProviderError, TimeoutError) as exc:
        result.update(reason_code=exc.code if isinstance(exc, MemoryProviderError) else "timeout",
                      fallback="current_input_only_until_read_scope_resolved")
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    reason = unresolved_reason(result, config) if secondary is not None else None
    if reason:
        primary = deepcopy(result)
        async def single_fallback(state, settings):
            # Exactly one independent Qwen request, no protocol retry and no
            # third-stage dialogue planner. It sees the original input only.
            return await configured_read(state, settings, max_attempts=1)
        result = await route(utterance, scope, secondary, recent_context=recent_context,
            visible_conversation_available=visible_conversation_available, evaluator=single_fallback, entry=entry)
        if unresolved_reason(result, config):
            # A second uncertain answer ends the decision chain. Preserve its
            # diagnostics, but never turn uncertainty into speculative recall.
            result.update(read_policy="uncertain", memory_forbidden=True)
            for name in ("memory", "task"):
                result[name].update(needed=False, reason_code="restriction_unresolved")
            if result["reason_code"] not in {"timeout", "network_error", "invalid_response"}:
                result["reason_code"] = "fallback_unresolved"
        result.update(attempt_id=primary["attempt_id"], input_hash=key, primary_decision=primary,
            fallback="independent_read_judge", fallback_reason="jev_" + reason,
            primary_model_requested=primary["model_requested"],
            provider_attempts=primary.get("provider_attempts", 1) + result.get("provider_attempts", 1),
            usage={name: primary.get("usage", {}).get(name, 0) + result.get("usage", {}).get(name, 0)
                   for name in ("input_tokens", "output_tokens")},
            duration_ms=round((time.monotonic() - started) * 1000))
    if evaluator is None and result.get("model") and result["read_policy"] != "uncertain":
        _cache[key] = (time.monotonic(), deepcopy(result))
        _cache.move_to_end(key)
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return result
