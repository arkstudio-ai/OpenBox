"""Capability-gated provider-native deferred tool discovery.

The portable discovery path remains the authority for every provider unless a
complete provider binding is explicitly allowlisted.  This module contains the
provider-specific wire and stream contracts; planner and executor code should
only consume its typed decisions/events.

OpenAI Responses is the sole native adapter implemented here.  Anthropic stays
portable until a direct-wire (or proven LiteLLM) request, stream, and replay
contract exists.  An OpenAI-compatible URL is not proof of the Responses Tool
Search protocol.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from agent.tool_exposure import EligibleCatalog, ExposurePlan
from agent.tool_payload import build_tool_definitions, proxy_token_count
from session.internal_parts import ProviderCapabilityBinding


OPENAI_TOOL_SEARCH_DESCRIPTION = (
    "Search the eligible deferred tool catalogue by capability. Matching "
    "results reveal exact typed tools; do not guess tool names."
)
OPENAI_TOOL_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Short capability or task to search for.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}
NATIVE_CAPABILITY_STATE_KEY = "native_capabilities"
NATIVE_CAPABILITY_STATE_VERSION = 1
DEFAULT_NATIVE_CAPABILITY_TTL_SECONDS = 30 * 60


class NativeProtocolError(RuntimeError):
    """A native stream violated the request-bound protocol contract."""


class NativeFeatureUnsupported(RuntimeError):
    """A provider rejected Tool Search before emitting any response event."""


class NativeHTTPError(RuntimeError):
    """A non-feature HTTP error that must not trigger a portable replay."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = str(body)[:2_000]
        super().__init__(f"Responses API {status_code}: {self.body[:200]}")


@dataclass(frozen=True)
class NativeCapabilityKey:
    """Complete, secret-free key for one native capability entitlement."""

    adapter: str
    binding_digest: str
    config_generation: str

    def digest(self) -> str:
        payload = json.dumps(
            {
                "adapter": self.adapter,
                "binding": self.binding_digest,
                "config_generation": self.config_generation,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


NativeCapabilityStatus = Literal["supported", "unsupported"]


@dataclass(frozen=True)
class NativeCapabilityRecord:
    status: NativeCapabilityStatus
    expires_at: float
    reason: str = ""

    def active(self, now: float | None = None) -> bool:
        return self.expires_at > (time.time() if now is None else now)


class NativeCapabilityCache:
    """Session-scoped sticky capability cache with bounded, serializable state."""

    def __init__(self, *, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._records: dict[tuple[str, str], NativeCapabilityRecord] = {}

    def get(
        self,
        session_id: str,
        key: NativeCapabilityKey,
        *,
        now: float | None = None,
    ) -> NativeCapabilityRecord | None:
        cache_key = (session_id, key.digest())
        record = self._records.get(cache_key)
        if record is not None and not record.active(now):
            self._records.pop(cache_key, None)
            return None
        return record

    def record(
        self,
        session_id: str,
        key: NativeCapabilityKey,
        status: NativeCapabilityStatus,
        *,
        ttl_seconds: int = DEFAULT_NATIVE_CAPABILITY_TTL_SECONDS,
        reason: str = "",
        now: float | None = None,
    ) -> NativeCapabilityRecord:
        if not 1 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        current = time.time() if now is None else now
        record = NativeCapabilityRecord(
            status=status,
            expires_at=current + ttl_seconds,
            reason=str(reason)[:256],
        )
        self._records[(session_id, key.digest())] = record
        if len(self._records) > self.max_entries:
            oldest = sorted(
                self._records,
                key=lambda cache_key: (
                    self._records[cache_key].expires_at,
                    cache_key,
                ),
            )
            for cache_key in oldest[: len(self._records) - self.max_entries]:
                self._records.pop(cache_key, None)
        return record

    def load_session_state(
        self,
        session_id: str,
        state: Mapping[str, Any] | None,
        *,
        now: float | None = None,
    ) -> None:
        """Load the private projection stored under session tool exposure state."""

        current = time.time() if now is None else now
        root = dict(state or {}).get(NATIVE_CAPABILITY_STATE_KEY)
        if not isinstance(root, dict) or root.get("version") != NATIVE_CAPABILITY_STATE_VERSION:
            return
        entries = root.get("entries")
        if not isinstance(entries, dict):
            return
        for key_digest, raw in entries.items():
            if (
                not isinstance(key_digest, str)
                or len(key_digest) != 64
                or not isinstance(raw, dict)
                or raw.get("status") not in {"supported", "unsupported"}
            ):
                continue
            try:
                expires_at = float(raw.get("expires_at"))
            except (TypeError, ValueError):
                continue
            record = NativeCapabilityRecord(
                status=raw["status"],
                expires_at=expires_at,
                reason=str(raw.get("reason") or "")[:256],
            )
            if record.active(current):
                self._records[(session_id, key_digest)] = record

    def project_session_state(
        self,
        session_id: str,
        state: Mapping[str, Any] | None,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return a copy suitable for the existing private session JSON column."""

        current = time.time() if now is None else now
        projected = json.loads(json.dumps(dict(state or {})))
        entries: dict[str, dict[str, Any]] = {}
        for (record_session, key_digest), record in sorted(self._records.items()):
            if record_session != session_id or not record.active(current):
                continue
            entries[key_digest] = {
                "status": record.status,
                "expires_at": record.expires_at,
                "reason": record.reason,
            }
        projected[NATIVE_CAPABILITY_STATE_KEY] = {
            "version": NATIVE_CAPABILITY_STATE_VERSION,
            "entries": entries,
        }
        return projected


# Process-local hot cache. Durable session state remains authoritative and is
# loaded into this cache by the loop before deciding a native request.
NATIVE_CAPABILITY_CACHE = NativeCapabilityCache()


def native_config_generation(
    config: Mapping[str, Any],
    *,
    catalogue_generation: str,
) -> str:
    """Bind capability probes to rollout config and exact eligible catalogue."""

    payload = {
        "config": dict(config),
        "catalogue_generation": catalogue_generation,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class NativeGateDecision:
    enabled: bool
    probe: bool
    reason: str
    key: NativeCapabilityKey | None = None


def _normalize_endpoint(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return ""
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return ""
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}{path}"


def _allowlisted(value: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns if pattern)


def decide_native_adapter(
    *,
    requested_mode: str,
    model_id: str,
    configured_endpoint: str,
    binding: ProviderCapabilityBinding,
    endpoint_allowlist: Sequence[str],
    model_allowlist: Sequence[str],
    config_generation: str,
    session_id: str,
    cache: NativeCapabilityCache,
    has_deferred_tools: bool,
    catalogue_wire_chars: int,
    catalogue_wire_hard_chars: int = 128_000,
) -> NativeGateDecision:
    """Gate native execution on every request/binding eligibility dimension."""

    if requested_mode != "native_auto":
        return NativeGateDecision(False, False, "mode_not_native_auto")
    if binding.provider != "openai" or binding.dialect != "responses":
        # This is the explicit capability gate for Anthropic, Gemini and
        # OpenAI-compatible Chat Completions routes.
        return NativeGateDecision(False, False, "adapter_not_verified")
    if not has_deferred_tools:
        return NativeGateDecision(False, False, "nothing_deferred")
    if catalogue_wire_chars > min(128_000, catalogue_wire_hard_chars):
        return NativeGateDecision(False, False, "catalogue_wire_hard_limit")
    normalized_endpoint = _normalize_endpoint(configured_endpoint)
    normalized_allowlist = tuple(
        normalized
        for raw in endpoint_allowlist
        if (normalized := _normalize_endpoint(raw))
    )
    if not normalized_endpoint or not _allowlisted(normalized_endpoint, normalized_allowlist):
        return NativeGateDecision(False, False, "endpoint_not_allowlisted")
    bare_model = model_id.split("/", 1)[-1]
    if not (
        _allowlisted(model_id, model_allowlist)
        or _allowlisted(bare_model, model_allowlist)
    ):
        return NativeGateDecision(False, False, "model_not_allowlisted")
    if not config_generation:
        return NativeGateDecision(False, False, "incomplete_config_binding")

    key = NativeCapabilityKey(
        adapter="openai_responses_tool_search_v1",
        binding_digest=binding.digest(),
        config_generation=config_generation,
    )
    cached = cache.get(session_id, key)
    if cached is not None and cached.status == "unsupported":
        return NativeGateDecision(False, False, "sticky_unsupported", key)
    return NativeGateDecision(
        True,
        cached is None,
        "probe" if cached is None else "cached_supported",
        key,
    )


@dataclass(frozen=True)
class OpenAINativePlan:
    """Exact Responses wire projection and validation bindings for one request."""

    tools: tuple[dict[str, Any], ...]
    direct_wire_names: frozenset[str]
    deferred_wire_names: frozenset[str]
    wire_to_canonical: Mapping[str, str]
    schema_digest_by_wire: Mapping[str, str]
    provider_definition_digest_by_wire: Mapping[str, str]
    same_response_safe_wire_names: frozenset[str]
    catalogue_generation: str
    catalogue_wire_chars: int
    initial_visible_chars: int
    catalogue_wire_proxy_tokens: int
    initial_visible_proxy_tokens: int


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_openai_responses_native_plan(
    catalog: EligibleCatalog,
    exposure_plan: ExposurePlan,
    *,
    synthetic_tools: Mapping[str, Any] | None = None,
) -> OpenAINativePlan:
    """Map the logical discovery slot to Responses Tool Search exactly once."""

    eligible = set(catalog.entries)
    if (set(exposure_plan.direct_ids) | set(exposure_plan.deferred_ids)) != eligible:
        raise ValueError("native exposure plan must partition the eligible catalogue")
    if set(exposure_plan.direct_ids) & set(exposure_plan.deferred_ids):
        raise ValueError("native direct and deferred sets must be disjoint")

    direct_ids = tuple(
        tool_id
        for tool_id in exposure_plan.direct_ids
        if tool_id != "capability_search"
    )
    deferred_ids = tuple(
        tool_id
        for tool_id in exposure_plan.deferred_ids
        if tool_id != "capability_search"
    )
    if not deferred_ids:
        raise ValueError("native Tool Search requires a deferred frontier")

    wire_to_canonical = {
        entry.provider_name: tool_id
        for tool_id, entry in catalog.entries.items()
        if tool_id != "capability_search"
    }
    direct_tools = {
        catalog.entries[tool_id].provider_name: catalog.tools[tool_id]
        for tool_id in direct_ids
    }
    deferred_tools = {
        catalog.entries[tool_id].provider_name: catalog.tools[tool_id]
        for tool_id in deferred_ids
    }
    synthetic = dict(synthetic_tools or {})
    if set(synthetic) & set(wire_to_canonical):
        raise ValueError("synthetic native tool collides with eligible catalogue")
    direct_definitions = build_tool_definitions(
        {**direct_tools, **synthetic},
        "responses",
    )
    deferred_definitions = build_tool_definitions(deferred_tools, "responses")
    for definition in deferred_definitions:
        definition["defer_loading"] = True
    search_definition = {
        "type": "tool_search",
        "execution": "server",
        "description": OPENAI_TOOL_SEARCH_DESCRIPTION,
        "parameters": json.loads(json.dumps(OPENAI_TOOL_SEARCH_PARAMETERS)),
    }
    definitions = (*direct_definitions, search_definition, *deferred_definitions)
    wire_text = _compact(definitions)
    visible_text = _compact((*direct_definitions, search_definition))
    wire_to_canonical.update(
        {
            provider_name: str(
                getattr(tool, "canonical_id", None)
                or getattr(tool, "id", None)
                or provider_name
            )
            for provider_name, tool in synthetic.items()
        }
    )
    schema_digest_by_wire = {
        catalog.entries[tool_id].provider_name: catalog.entries[tool_id].schema_digest
        for tool_id in (*direct_ids, *deferred_ids)
    }
    provider_definition_digest_by_wire = {
        str(definition["name"]): hashlib.sha256(_compact(definition).encode()).hexdigest()
        for definition in deferred_definitions
    }
    safe = frozenset(
        catalog.entries[tool_id].provider_name
        for tool_id in deferred_ids
        if catalog.entries[tool_id].same_response_safe
    )
    return OpenAINativePlan(
        tools=tuple(definitions),
        direct_wire_names=frozenset((*direct_tools, *synthetic)),
        deferred_wire_names=frozenset(deferred_tools),
        wire_to_canonical=wire_to_canonical,
        schema_digest_by_wire=schema_digest_by_wire,
        provider_definition_digest_by_wire=provider_definition_digest_by_wire,
        same_response_safe_wire_names=safe,
        catalogue_generation=catalog.generation,
        catalogue_wire_chars=len(wire_text),
        initial_visible_chars=len(visible_text),
        catalogue_wire_proxy_tokens=proxy_token_count(wire_text),
        initial_visible_proxy_tokens=proxy_token_count(visible_text),
    )


def is_explicit_native_unsupported(status_code: int, body: str) -> bool:
    """Recognize only feature-unsupported pre-stream errors, never generic 400s."""

    if status_code not in {400, 404, 422}:
        return False
    text = str(body or "").lower()[:8_000]
    feature = r"(?:tool_search|defer_loading)"
    # Plain-text gateways are accepted only when the unsupported phrase is
    # locally about the native feature.  Merely echoing ``tool_search`` in a
    # request dump must not turn an unrelated schema/parameter 400 into a
    # replayable capability miss.
    if re.search(
        rf"\b{feature}\b.{{0,64}}\b(?:unsupported|not supported|not available|unrecognized)\b",
        text,
    ):
        return True
    if re.search(
        rf"\b(?:unsupported|not supported|not available|unknown tool type|unrecognized tool type)\b.{{0,64}}\b{feature}\b",
        text,
    ):
        return True

    # Structured provider errors may identify the rejected parameter
    # separately from their message/code.  Require that selector itself to be
    # native; never infer it from another occurrence in the body.
    try:
        payload = json.loads(str(body or ""))
    except (TypeError, ValueError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    selector = " ".join(
        str(error.get(key) or "")
        for key in ("param", "parameter")
    ).lower()
    classification = " ".join(
        str(error.get(key) or "")
        for key in ("message", "code", "type")
    ).lower()
    return bool(
        re.search(r"\b(?:tool_search|defer_loading)\b", selector)
        and any(
            marker in classification
            for marker in (
                "unsupported",
                "not supported",
                "not available",
                "unknown",
                "unrecognized",
            )
        )
    )


@dataclass(frozen=True)
class NativeNormalizedEvent:
    type: Literal["search_started", "search_result", "tool_revealed", "tool_call"]
    stream_seq: int
    raw_item: Mapping[str, Any]
    canonical_tool_id: str | None = None
    wire_tool_name: str | None = None
    call_id: str | None = None
    arguments: str | None = None
    same_response_executable: bool | None = None
    error_code: str | None = None


def _definition_digest(definition: Mapping[str, Any]) -> str:
    """Digest the complete provider-visible definition, including extensions."""

    return hashlib.sha256(_compact(definition).encode()).hexdigest()


class OpenAIResponsesNativeNormalizer:
    """Normalize raw Responses output items without buffering or reordering."""

    _RAW_TYPES = frozenset({"tool_search_call", "tool_search_output", "tool_reference"})

    def __init__(
        self,
        plan: OpenAINativePlan,
        *,
        budget_state: Any,
    ) -> None:
        self.plan = plan
        required = (
            "_capability_search_calls",
            "_capability_revealed_ids",
            "_capability_result_chars",
            "_capability_max_search_calls",
            "_capability_max_reveals",
            "_capability_max_result_chars",
        )
        if any(not hasattr(budget_state, name) for name in required):
            raise ValueError("native discovery requires a complete step budget state")
        if not isinstance(budget_state._capability_revealed_ids, set):
            raise ValueError("native discovery reveal budget state must be mutable")
        self.budget_state = budget_state
        self.max_search_calls = max(1, int(budget_state._capability_max_search_calls))
        self.max_reveals = max(1, int(budget_state._capability_max_reveals))
        self.max_result_chars = max(100, int(budget_state._capability_max_result_chars))
        self.revealed_wire_names: set[str] = set()
        self._search_stage: Literal["idle", "called", "result"] = "idle"
        self._active_search_call_id: str | None = None
        self._stream_seq = 0
        self._seen_item_by_id: dict[str, str] = {}
        # response.completed repeats output items that may already have arrived
        # as output_item.done. Server Tool Search is allowed to omit item and
        # call ids, so only that terminal summary uses counted fingerprints;
        # ordinary id-less items remain distinct and consume their budgets.
        self._streamed_fingerprints: Counter[str] = Counter()
        self._saw_response_completed = False
        self._finalized = False

    @property
    def search_calls(self) -> int:
        return int(self.budget_state._capability_search_calls)

    @property
    def result_chars(self) -> int:
        return int(self.budget_state._capability_result_chars)

    def _next(
        self,
        event_type: Literal["search_started", "search_result", "tool_revealed", "tool_call"],
        raw_item: Mapping[str, Any],
        **kwargs: Any,
    ) -> NativeNormalizedEvent:
        event = NativeNormalizedEvent(
            type=event_type,
            stream_seq=self._stream_seq,
            raw_item=json.loads(json.dumps(dict(raw_item))),
            **kwargs,
        )
        self._stream_seq += 1
        return event

    def feed_sse(self, event: Mapping[str, Any]) -> tuple[NativeNormalizedEvent, ...]:
        """Consume one SSE JSON object and preserve its output-item order."""

        event_type = str(event.get("type") or "")
        if self._finalized or self._saw_response_completed:
            raise NativeProtocolError("Responses emitted an event after response.completed")
        if event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict):
                raise NativeProtocolError("Responses output item event has no object item")
            # Added items are often incomplete (arguments/status are finalized
            # by output_item.done). Persist/replay only the authoritative done
            # item; its position still precedes the result/reference items.
            return ()
        if event_type == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                raise NativeProtocolError("Responses output item event has no object item")
            normalized = self.feed_item(item)
            if not item.get("id"):
                self._streamed_fingerprints[_compact(item)] += 1
            return normalized
        if event_type == "response.completed":
            response = event.get("response")
            output = response.get("output") if isinstance(response, dict) else None
            if not isinstance(output, list):
                return ()
            normalized: list[NativeNormalizedEvent] = []
            for item in output:
                if isinstance(item, dict):
                    if not item.get("id"):
                        fingerprint = _compact(item)
                        if self._streamed_fingerprints[fingerprint] > 0:
                            self._streamed_fingerprints[fingerprint] -= 1
                            continue
                    normalized.extend(self.feed_item(item))
            self._saw_response_completed = True
            return tuple(normalized)
        return ()

    def finalize(self) -> None:
        """Require a terminal completed response and no unfinished search."""

        if self._finalized:
            return
        if not self._saw_response_completed:
            raise NativeProtocolError("native Responses stream ended before response.completed")
        if self._search_stage == "called":
            raise NativeProtocolError("native Responses stream ended with unfinished Tool Search")
        self._finalized = True

    def feed_item(self, item: Mapping[str, Any]) -> tuple[NativeNormalizedEvent, ...]:
        raw_type = str(item.get("type") or "")
        if raw_type not in self._RAW_TYPES | {"function_call"}:
            return ()
        item_id = str(item.get("id") or "")
        if item_id:
            fingerprint = _compact(item)
            previous = self._seen_item_by_id.get(item_id)
            if previous is not None:
                if previous != fingerprint:
                    raise NativeProtocolError(
                        "Responses output item id was reused with different content"
                    )
                return ()
            self._seen_item_by_id[item_id] = fingerprint

        if raw_type == "tool_search_call":
            if item.get("execution") != "server":
                raise NativeProtocolError("native Tool Search call was not server-executed")
            if self._search_stage == "called":
                raise NativeProtocolError("native Tool Search calls overlap")
            if self.search_calls >= self.max_search_calls:
                raise NativeProtocolError("native Tool Search call budget exceeded")
            self.budget_state._capability_search_calls += 1
            self._search_stage = "called"
            raw_call_id = item.get("call_id")
            self._active_search_call_id = (
                str(raw_call_id) if raw_call_id not in (None, "") else None
            )
            return (self._next("search_started", item),)

        if raw_type == "tool_search_output":
            if self._search_stage != "called":
                raise NativeProtocolError("native Tool Search output preceded its call")
            if item.get("execution") != "server" or item.get("status") != "completed":
                raise NativeProtocolError("native Tool Search output is not completed server output")
            raw_output_call_id = item.get("call_id")
            output_call_id = (
                str(raw_output_call_id)
                if raw_output_call_id not in (None, "")
                else None
            )
            if output_call_id != self._active_search_call_id:
                raise NativeProtocolError("native Tool Search call/output id mismatch")
            tools = item.get("tools")
            if not isinstance(tools, list):
                raise NativeProtocolError("native Tool Search output tools must be an array")
            encoded = _compact(item)
            if self.result_chars + len(encoded) > self.max_result_chars:
                raise NativeProtocolError("native Tool Search result character budget exceeded")
            self.budget_state._capability_result_chars += len(encoded)
            self._search_stage = "result"
            self._active_search_call_id = None
            events: list[NativeNormalizedEvent] = [self._next("search_result", item)]
            for definition in tools:
                if not isinstance(definition, dict):
                    raise NativeProtocolError("native Tool Search returned a non-object definition")
                events.extend(self._reveal_definition(definition, item))
            return tuple(events)

        if raw_type == "tool_reference":
            if self._search_stage != "result":
                raise NativeProtocolError("native tool reference preceded its search result")
            definition = item.get("tool")
            if not isinstance(definition, dict):
                raise NativeProtocolError("native tool reference has no exact tool definition")
            encoded = _compact(item)
            if self.result_chars + len(encoded) > self.max_result_chars:
                raise NativeProtocolError("native Tool Search result character budget exceeded")
            self.budget_state._capability_result_chars += len(encoded)
            return tuple(self._reveal_definition(definition, item))

        wire_name = str(item.get("name") or "")
        canonical_id = self.plan.wire_to_canonical.get(wire_name)
        if canonical_id is None:
            raise NativeProtocolError("native function call referenced an unknown tool")
        was_deferred = wire_name in self.plan.deferred_wire_names
        if was_deferred and wire_name not in self.revealed_wire_names:
            raise NativeProtocolError("native function call preceded its tool reference")
        same_response = not was_deferred or wire_name in self.plan.same_response_safe_wire_names
        return (
            self._next(
                "tool_call",
                item,
                canonical_tool_id=canonical_id,
                wire_tool_name=wire_name,
                call_id=str(item.get("call_id") or item.get("id") or ""),
                arguments=str(item.get("arguments") or "{}"),
                same_response_executable=same_response,
                error_code=None if same_response else "deferred_until_next_step",
            ),
        )

    def _reveal_definition(
        self,
        definition: Mapping[str, Any],
        raw_item: Mapping[str, Any],
    ) -> list[NativeNormalizedEvent]:
        if definition.get("type") != "function" or definition.get("defer_loading") is not True:
            raise NativeProtocolError("native Tool Search returned a non-deferred function")
        wire_name = str(definition.get("name") or "")
        if wire_name not in self.plan.deferred_wire_names:
            raise NativeProtocolError("native Tool Search revealed an ineligible tool")
        expected_digest = self.plan.provider_definition_digest_by_wire.get(wire_name)
        if not expected_digest or _definition_digest(definition) != expected_digest:
            raise NativeProtocolError("native Tool Search schema digest mismatch")
        if wire_name in self.revealed_wire_names:
            return []
        canonical_id = self.plan.wire_to_canonical[wire_name]
        shared_reveals = self.budget_state._capability_revealed_ids
        if canonical_id not in shared_reveals and len(shared_reveals) >= self.max_reveals:
            raise NativeProtocolError("native unique reveal budget exceeded")
        self.revealed_wire_names.add(wire_name)
        shared_reveals.add(canonical_id)
        reveal_raw = {
            "type": "tool_revealed",
            "tool": canonical_id,
            "wire_tool": wire_name,
            "source": "native",
            "provider_item": json.loads(json.dumps(dict(raw_item))),
        }
        return [
            self._next(
                "tool_revealed",
                reveal_raw,
                canonical_tool_id=canonical_id,
                wire_tool_name=wire_name,
                same_response_executable=(
                    wire_name in self.plan.same_response_safe_wire_names
                ),
            )
        ]


def build_openai_native_replay_sequence(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate opaque history while retaining its cross-store stream order."""

    ordered = sorted(records, key=lambda record: int(record.get("stream_seq", -1)))
    output: list[dict[str, Any]] = []
    last_seq = -1
    search_stage: Literal["idle", "called", "result"] = "idle"
    active_call_id: str | None = None
    for record in ordered:
        stream_seq = int(record.get("stream_seq", -1))
        if stream_seq <= last_seq:
            raise NativeProtocolError("provider replay stream sequence is not strictly increasing")
        last_seq = stream_seq
        raw = record.get("data")
        if not isinstance(raw, dict):
            raise NativeProtocolError("provider replay block is not an object")
        item = raw.get("item") if raw.get("type", "").startswith("response.") else raw
        if not isinstance(item, dict):
            raise NativeProtocolError("provider replay block has no output item")
        item_type = item.get("type")
        if item_type == "tool_search_call":
            if search_stage == "called" or item.get("execution") != "server":
                raise NativeProtocolError("provider replay has overlapping search calls")
            search_stage = "called"
            raw_call_id = item.get("call_id")
            active_call_id = str(raw_call_id) if raw_call_id not in (None, "") else None
        elif item_type == "tool_search_output":
            raw_call_id = item.get("call_id")
            output_call_id = str(raw_call_id) if raw_call_id not in (None, "") else None
            if (
                search_stage != "called"
                or item.get("execution") != "server"
                or item.get("status") != "completed"
                or not isinstance(item.get("tools"), list)
                or output_call_id != active_call_id
            ):
                raise NativeProtocolError("provider replay has orphan search output")
            search_stage = "result"
            active_call_id = None
        elif item_type == "tool_reference":
            if search_stage != "result":
                raise NativeProtocolError("provider replay has orphan tool reference")
        else:
            raise NativeProtocolError("provider replay contains a non-search opaque block")
        output.append(
            {
                "stream_seq": stream_seq,
                "item": json.loads(json.dumps(item)),
            }
        )
    if search_stage == "called":
        raise NativeProtocolError("provider replay has an unfinished search call")
    return output


def build_openai_native_replay_input(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild only validated, ordered opaque Tool Search transcript items."""

    return [
        entry["item"]
        for entry in build_openai_native_replay_sequence(records)
    ]


async def stream_openai_responses_json(
    client: Any,
    *,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw Responses SSE JSON while classifying only pre-stream 4xx.

    ``client`` intentionally follows the small ``httpx.AsyncClient`` surface
    instead of being constructed here.  The production adapter can reuse its
    configured timeout/client, and tests capture the actual POST wire rather
    than asserting kwargs passed to a higher-level SDK.
    """

    async with client.stream(
        "POST",
        url,
        headers=dict(headers),
        json=json.loads(json.dumps(dict(payload))),
    ) as response:
        if response.status_code != 200:
            raw_body = await response.aread()
            body = raw_body.decode("utf-8", errors="replace")
            if is_explicit_native_unsupported(response.status_code, body):
                raise NativeFeatureUnsupported(
                    f"Responses native Tool Search unsupported ({response.status_code})"
                )
            raise NativeHTTPError(response.status_code, body)

        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_text = line[6:]
            if data_text == "[DONE]":
                return
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError as exc:
                raise NativeProtocolError("invalid Responses SSE JSON") from exc
            if not isinstance(data, dict):
                raise NativeProtocolError("Responses SSE data must be an object")
            yield data


async def stream_with_native_fallback(
    *,
    session_id: str,
    key: NativeCapabilityKey,
    cache: NativeCapabilityCache,
    native_factory: Callable[[], AsyncIterator[dict[str, Any]]],
    portable_factory: Callable[[], AsyncIterator[dict[str, Any]]],
    ttl_seconds: int = DEFAULT_NATIVE_CAPABILITY_TTL_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Fallback once only when unsupported is proven before the first event."""

    emitted = False
    try:
        async for event in native_factory():
            emitted = True
            yield event
    except NativeFeatureUnsupported as exc:
        if emitted:
            raise
        cache.record(
            session_id,
            key,
            "unsupported",
            ttl_seconds=ttl_seconds,
            reason=str(exc),
        )
        async for event in portable_factory():
            yield event
        return
    cache.record(session_id, key, "supported", ttl_seconds=ttl_seconds)
