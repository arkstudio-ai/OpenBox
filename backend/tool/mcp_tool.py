"""MCP tool wrapper: dynamically create ToolInfo for MCP tools from container."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import itertools
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from core.log import create_logger
from tool.tool import ToolInfo, ToolResult, ToolContext

log = create_logger("tool.mcp")

MAX_TOOL_NAME_LEN = 64
MCP_CANONICAL_PREFIX = "mcp:v2:"
MCP_CANONICAL_DIGEST_LEN = 52
MCP_EVIDENCE_TTL_SECONDS = 300.0
MCP_EVIDENCE_MAX_ENTRIES = 4096
MCP_DIRECT_SINGLE_HARD_CHARS = 5_000
MCP_DIRECT_TOTAL_HARD_CHARS = 32_000
MCP_META_INDEX_NAME_CHARS = 120
MCP_META_INDEX_HINT_CHARS = 200
MCP_META_INDEX_PARAMETER_LIMIT = 24
MCP_META_INDEX_PARAMETER_NAME_CHARS = 80
MCP_META_INDEX_PARAMETER_DESCRIPTION_CHARS = 80
MCP_META_INDEX_SERVER_LIMIT = 20
MCP_META_INDEX_SERVER_CHARS = 80
MCP_RESOURCE_CANONICAL_PREFIX = "mcp-resource:v1:"
MCP_RESOURCE_TITLE_CHARS = 160
MCP_FAILURE_IDENTITY_CHARS = 128
MCP_FAILURE_DETAIL_CHARS = 500
MCP_FAILURE_MAX_BYTES = 2_000
MCP_NORMALIZATION_CACHE_TTL_SECONDS = 60.0
MCP_NORMALIZATION_CACHE_MAX_ENTRIES = 64
MCP_NORMALIZATION_DIALECT = "provider-neutral-v1"

_MCP_META_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MCP_META_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class _McpBinding:
    """One unambiguous catalogue entry.

    ``canonical_id`` is the security identity. ``provider_name`` is only the
    bounded name placed on the model wire. Raw MCP identities never become
    permission subjects, but remain losslessly available inside this closure
    so the executor can address the sandbox server.
    """

    canonical_id: str
    provider_name: str
    legacy_name: str
    server: str
    name: str
    description: str
    input_schema: dict
    schema_digest: str


@dataclass(frozen=True)
class _McpMetaIndexEntry:
    """Bounded search-only projection retaining the complete execution binding."""

    binding: _McpBinding
    searchable: str
    display_server: str
    display_name: str
    description_hint: str
    category: str
    parameter_details: tuple[tuple[str, str, str, bool], ...]


@dataclass(frozen=True)
class _McpNormalizationArtifacts:
    """Permission-independent, immutable work for one catalogue generation."""

    bindings: tuple[_McpBinding, ...]
    search_index: tuple[_McpMetaIndexEntry, ...]
    schema_models: tuple[tuple[str, type[BaseModel]], ...]


@dataclass(frozen=True)
class _McpNormalizationCacheEntry:
    artifacts: _McpNormalizationArtifacts
    expires_at: float


_MCP_NORMALIZATION_CACHE: OrderedDict[str, _McpNormalizationCacheEntry] = (
    OrderedDict()
)
_MCP_NORMALIZATION_INFLIGHT: dict[tuple[int, str], asyncio.Task] = {}
_MCP_NORMALIZATION_LOCK = threading.Lock()


def _mcp_normalization_now() -> float:
    return time.monotonic()


def _bounded_meta_text(value: object, limit: int) -> str:
    """Normalize only a bounded prefix of an untrusted catalogue string."""

    if limit <= 0:
        return ""
    # Slice before regex work so a multi-megabyte remote description cannot
    # make every repeated search linear in its complete size.
    prefix = str(value or "")[: limit * 2]
    return _MCP_META_SPACE.sub(
        " ", _MCP_META_CONTROL.sub("", prefix)
    ).strip()[:limit]


def _meta_parameter_details(input_schema: dict) -> tuple[tuple[str, str, str, bool], ...]:
    """Read only a fixed prefix of parameter metadata for search display."""

    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return ()
    raw_required = input_schema.get("required", [])
    required_prefix = {
        str(value)
        for value in itertools.islice(
            raw_required if isinstance(raw_required, list) else (),
            MCP_META_INDEX_PARAMETER_LIMIT,
        )
    }
    details = []
    for raw_name, raw_definition in itertools.islice(
        properties.items(), MCP_META_INDEX_PARAMETER_LIMIT
    ):
        name = _bounded_meta_text(
            raw_name, MCP_META_INDEX_PARAMETER_NAME_CHARS
        )
        if not name:
            continue
        definition = raw_definition if isinstance(raw_definition, dict) else {}
        raw_type = definition.get("type")
        parameter_type = (
            _bounded_meta_text(raw_type, 24)
            if isinstance(raw_type, str)
            else "?"
        )
        description = _bounded_meta_text(
            definition.get("description", ""),
            MCP_META_INDEX_PARAMETER_DESCRIPTION_CHARS,
        )
        details.append(
            (name, parameter_type or "?", description, str(raw_name) in required_prefix)
        )
    return tuple(details)


def _build_meta_search_index(
    bindings: list[_McpBinding],
) -> tuple[_McpMetaIndexEntry, ...]:
    """Build a bounded immutable index once; searches never revisit raw schemas."""

    entries = []
    for binding in bindings:
        display_server = _bounded_meta_text(
            binding.server, MCP_META_INDEX_SERVER_CHARS
        )
        display_name = _bounded_meta_text(
            binding.name, MCP_META_INDEX_NAME_CHARS
        )
        description_hint = _bounded_meta_text(
            binding.description, MCP_META_INDEX_HINT_CHARS
        )
        searchable = " ".join(
            value for value in (display_server, display_name, description_hint) if value
        ).casefold()
        category = (display_name.split("_", 1)[0] if display_name else "other") or "other"
        entries.append(_McpMetaIndexEntry(
            binding=binding,
            searchable=searchable,
            display_server=display_server,
            display_name=display_name,
            description_hint=description_hint,
            category=category,
            parameter_details=_meta_parameter_details(binding.input_schema),
        ))
    return tuple(sorted(entries, key=lambda entry: entry.binding.canonical_id))


# Discovery evidence is deliberately server-side state. The model receives a
# canonical ID, never a bearer token it could replay in a different scope.
# Values are monotonic expirations; every key also carries all relevant scope
# and catalogue fields (see _evidence_key).
_DISCOVERY_EVIDENCE: dict[tuple[str, ...], float] = {}


def _length_prefixed(parts: tuple[str, ...]) -> bytes:
    encoded = bytearray()
    for part in parts:
        value = part.encode("utf-8")
        encoded.extend(len(value).to_bytes(8, "big"))
        encoded.extend(value)
    return bytes(encoded)


def _canonical_tool_id(server: str, name: str) -> str:
    """Return the bounded v2 security identity for an MCP raw tuple."""
    digest = hashlib.sha256(_length_prefixed((server, name))).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    assert len(token) == MCP_CANONICAL_DIGEST_LEN
    return f"{MCP_CANONICAL_PREFIX}{token}"


def _canonical_resource_id(server: object, uri: object) -> str:
    """Return a fixed-size permission subject for one exact MCP resource."""

    digest = hashlib.sha256(
        _length_prefixed((str(server or ""), str(uri or "")))
    ).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    assert len(token) == MCP_CANONICAL_DIGEST_LEN
    return f"{MCP_RESOURCE_CANONICAL_PREFIX}{token}"


def _bounded_resource_contents(contents: object) -> tuple[str, bool]:
    """Render only MCP resource body fields, within the shared output cap.

    The full body is deliberately not copied to host storage. Resource data
    may contain credentials, so truncation leaves only a bounded in-context
    preview and a notice rather than a path to a second ungoverned copy.
    """

    from tool.truncation import MAX_BYTES, MAX_LINES

    notice = "\n\n... MCP resource output truncated at the safe context limit ..."
    notice_bytes = len(notice.encode("utf-8"))
    payload_byte_limit = max(0, MAX_BYTES - notice_bytes)
    payload_line_limit = max(1, MAX_LINES - notice.count("\n"))
    if not isinstance(contents, list):
        return "(empty)", False

    chunks: list[str] = []
    used_bytes = 0
    used_lines = 0
    truncated = False
    for item in contents:
        if not isinstance(item, dict):
            continue
        if "text" in item:
            raw = str(item.get("text") or "")
        elif "blob" in item:
            mime = _bounded_meta_text(item.get("mimeType", "unknown"), 120)
            raw = f"[Binary data: {mime or 'unknown'}]"
        else:
            # Unknown remote fields (including a generic `content`) are not a
            # resource body contract and must not be reflected accidentally.
            continue

        separator = "\n" if chunks else ""
        remaining_bytes = payload_byte_limit - used_bytes
        remaining_lines = payload_line_limit - used_lines
        separator_bytes = len(separator.encode("utf-8"))
        if remaining_bytes <= separator_bytes or remaining_lines <= 0:
            truncated = True
            break
        content_byte_limit = remaining_bytes - separator_bytes

        # Slice before UTF-8 encoding or line splitting. At most MAX_BYTES
        # characters from any untrusted field are copied for this preview.
        char_prefix = raw[:content_byte_limit]
        encoded = char_prefix.encode("utf-8")
        if len(encoded) > content_byte_limit:
            char_prefix = encoded[:content_byte_limit].decode("utf-8", errors="ignore")
        line_prefix = "\n".join(char_prefix.split("\n")[:remaining_lines])
        candidate = separator + line_prefix
        candidate_bytes = len(candidate.encode("utf-8"))
        chunks.append(candidate)
        used_bytes += candidate_bytes
        used_lines += line_prefix.count("\n") + 1

        if len(line_prefix) < len(raw):
            truncated = True
            break

    rendered = "".join(chunks) or "(empty)"
    if truncated:
        rendered += notice
    # Keep this assertion beside the renderer: future notice edits must not
    # silently turn the documented hard limit into a soft target.
    assert len(rendered.encode("utf-8")) <= MAX_BYTES
    assert rendered.count("\n") + 1 <= MAX_LINES
    return rendered, truncated


def _schema_digest(description: str, input_schema: dict) -> str:
    payload = json.dumps(
        {"description": description, "input_schema": input_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _catalogue_generation(bindings: list[_McpBinding]) -> str:
    payload = _length_prefixed(tuple(
        f"{binding.canonical_id}:{binding.schema_digest}"
        for binding in sorted(bindings, key=lambda item: item.canonical_id)
    ))
    return hashlib.sha256(payload).hexdigest()


def _sandbox_identity(sandbox: Any) -> str:
    """Hash the real sandbox instance behind a catalogue snapshot wrapper.

    Tool definitions are built from ``_CatalogueSnapshotSandbox`` while their
    executors receive the underlying ``SandboxClient`` in ``ToolContext``.  The
    wrapper must therefore be transparent to discovery-evidence binding.  The
    live object identity also keeps two clients isolated even when a test or a
    deployment gives them the same externally visible locator.
    """
    source = getattr(sandbox, "_sandbox", sandbox)
    if source is None:
        source = sandbox
    instance = (
        f"{type(source).__module__}.{type(source).__qualname__}:{id(source)}"
    )
    for attr in ("sandbox_id", "base_url"):
        value = getattr(source, attr, None)
        if value not in (None, ""):
            raw = f"{instance}:{attr}:{value}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Test doubles and older clients have no stable locator. Object identity is
    # still fail-closed: evidence cannot cross to a different sandbox object.
    return hashlib.sha256(instance.encode("utf-8")).hexdigest()


def _build_bindings(mcp_tools: list[dict]) -> list[_McpBinding]:
    """Normalize a raw catalogue and resolve every identity before exposure."""
    candidates: dict[str, _McpBinding] = {}
    ambiguous: set[str] = set()

    for raw_tool in mcp_tools:
        if not isinstance(raw_tool, dict):
            continue
        server = str(raw_tool.get("server", "unknown"))
        name = str(raw_tool.get("name", "unknown"))
        description = str(raw_tool.get("description", "") or "")
        input_schema = raw_tool.get("input_schema", {})
        if not isinstance(input_schema, dict):
            input_schema = {}

        canonical_id = _canonical_tool_id(server, name)
        candidate = _McpBinding(
            canonical_id=canonical_id,
            provider_name="",
            legacy_name=_sanitize_tool_name(server, name),
            server=server,
            name=name,
            description=description,
            input_schema=input_schema,
            schema_digest=_schema_digest(description, input_schema),
        )
        previous = candidates.get(canonical_id)
        if previous is not None:
            if (
                previous.server != candidate.server
                or previous.name != candidate.name
                or previous.schema_digest != candidate.schema_digest
            ):
                ambiguous.add(canonical_id)
                log.error("Rejected ambiguous MCP canonical identity %s", canonical_id)
            # Byte-for-byte duplicate catalogue rows collapse to one binding.
            continue
        candidates[canonical_id] = candidate

    for canonical_id in ambiguous:
        candidates.pop(canonical_id, None)

    alias_groups: dict[str, list[_McpBinding]] = {}
    for candidate in candidates.values():
        alias_groups.setdefault(candidate.legacy_name, []).append(candidate)

    resolved: list[_McpBinding] = []
    used_provider_names: dict[str, str] = {}
    for legacy_name in sorted(alias_groups):
        group = sorted(alias_groups[legacy_name], key=lambda item: item.canonical_id)
        for candidate in group:
            if len(group) == 1:
                provider_name = legacy_name
            else:
                # Keep the old prefix for diagnostics and use the complete
                # canonical digest as suffix. Complete digest uniqueness was
                # already checked above, so this cannot silently collide.
                digest = candidate.canonical_id.removeprefix(MCP_CANONICAL_PREFIX)
                prefix_len = MAX_TOOL_NAME_LEN - len(digest) - 1
                provider_name = f"{legacy_name[:prefix_len]}_{digest}"

            previous_id = used_provider_names.get(provider_name)
            if previous_id is not None and previous_id != candidate.canonical_id:
                log.error("Rejected MCP provider-name collision")
                continue
            used_provider_names[provider_name] = candidate.canonical_id
            resolved.append(_McpBinding(
                canonical_id=candidate.canonical_id,
                provider_name=provider_name,
                legacy_name=candidate.legacy_name,
                server=candidate.server,
                name=candidate.name,
                description=candidate.description,
                input_schema=candidate.input_schema,
                schema_digest=candidate.schema_digest,
            ))

    return sorted(resolved, key=lambda item: item.canonical_id)


def _binding_rule_action(
    binding: _McpBinding,
    ruleset: list | None,
    legacy_name_count: int,
) -> str:
    """Compile canonical and legacy whole-tool rules with last-match-wins.

    A unique legacy alias keeps its old meaning. For a colliding exact alias,
    deny applies to every candidate but allow/ask cannot pick an arbitrary
    winner. Wildcard rules retain their intentionally broad meaning.
    """
    from core.wildcard import match as wildcard_match

    action = "ask"
    for rule in ruleset or []:
        if getattr(rule, "pattern", None) != "*":
            continue
        permission = str(getattr(rule, "permission", ""))
        rule_action = str(getattr(rule, "action", "ask"))
        if wildcard_match(binding.canonical_id, permission):
            action = rule_action
            continue
        if not wildcard_match(binding.legacy_name, permission):
            continue
        is_broad = "*" in permission
        if legacy_name_count > 1 and not is_broad and rule_action != "deny":
            continue
        action = rule_action
    return action


def _filter_permitted_bindings(
    bindings: list[_McpBinding], ruleset: list | None
) -> list[_McpBinding]:
    alias_counts: dict[str, int] = {}
    for binding in bindings:
        alias_counts[binding.legacy_name] = alias_counts.get(binding.legacy_name, 0) + 1
    return [
        binding
        for binding in bindings
        if _binding_rule_action(binding, ruleset, alias_counts[binding.legacy_name]) != "deny"
    ]


def _evidence_key(
    ctx: ToolContext,
    *,
    agent_id: str,
    sandbox_id: str,
    generation: str,
    binding: _McpBinding,
) -> tuple[str, ...] | None:
    actual_sandbox_id = _sandbox_identity(ctx.sandbox) if ctx.sandbox is not None else ""
    context_agent_id = str(getattr(ctx, "agent_id", "") or agent_id)
    # These are populated by the production loop. Missing scope is not a
    # wildcard: fail closed rather than creating replayable global evidence.
    if not all((ctx.user_id, ctx.session_id, ctx.run_id, context_agent_id, actual_sandbox_id)):
        return None
    if actual_sandbox_id != sandbox_id or context_agent_id != agent_id:
        return None
    return (
        ctx.user_id,
        ctx.project_id or "",
        ctx.session_id,
        ctx.run_id,
        context_agent_id,
        sandbox_id,
        generation,
        binding.canonical_id,
        binding.schema_digest,
    )


def _prune_evidence(now: float) -> None:
    expired = [key for key, deadline in _DISCOVERY_EVIDENCE.items() if deadline <= now]
    for key in expired:
        _DISCOVERY_EVIDENCE.pop(key, None)
    if len(_DISCOVERY_EVIDENCE) <= MCP_EVIDENCE_MAX_ENTRIES:
        return
    oldest = sorted(_DISCOVERY_EVIDENCE.items(), key=lambda item: item[1])
    for key, _deadline in oldest[:len(_DISCOVERY_EVIDENCE) - MCP_EVIDENCE_MAX_ENTRIES]:
        _DISCOVERY_EVIDENCE.pop(key, None)


def _record_evidence(
    ctx: ToolContext,
    *,
    agent_id: str,
    sandbox_id: str,
    generation: str,
    binding: _McpBinding,
) -> None:
    now = time.monotonic()
    _prune_evidence(now)
    key = _evidence_key(
        ctx,
        agent_id=agent_id,
        sandbox_id=sandbox_id,
        generation=generation,
        binding=binding,
    )
    if key is not None:
        _DISCOVERY_EVIDENCE[key] = now + MCP_EVIDENCE_TTL_SECONDS
        _prune_evidence(now)


def _has_evidence(
    ctx: ToolContext,
    *,
    agent_id: str,
    sandbox_id: str,
    generation: str,
    binding: _McpBinding,
) -> bool:
    now = time.monotonic()
    _prune_evidence(now)
    key = _evidence_key(
        ctx,
        agent_id=agent_id,
        sandbox_id=sandbox_id,
        generation=generation,
        binding=binding,
    )
    return key is not None and _DISCOVERY_EVIDENCE.get(key, 0.0) > now


async def _authorize_underlying(
    ctx: ToolContext, canonical_id: str, arguments: dict
) -> ToolResult | None:
    arguments_key = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # Direct MCP definitions have already passed ToolHooks under the exact
    # canonical identity. Keep the explicit inner check as a security
    # boundary, but do not prompt twice for the same normalized call. Meta
    # dispatch uses a different outer identity and therefore still performs
    # the required underlying authorization here.
    if (
        ctx._authorized_tool_id == canonical_id
        and ctx._authorized_tool_args_key == arguments_key
    ):
        return None
    authorize = ctx._authorize_tool
    if authorize is None:
        return ToolResult(
            title="Permission unavailable",
            output="MCP execution was blocked because its underlying permission could not be checked.",
            metadata={"blocked": True},
        )
    return await authorize(canonical_id, arguments)


def _bounded_utf8_text(value: object, max_bytes: int) -> tuple[str, bool]:
    """Return a UTF-8-safe prefix without encoding an unbounded input."""

    raw = str(value or "")
    prefix = raw[:max_bytes]
    encoded = prefix.encode("utf-8")
    if len(encoded) > max_bytes:
        prefix = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return prefix, len(prefix) < len(raw)


def _describe_failure(
    e: Exception,
    server_or_canonical: str,
    tool: str | None = None,
) -> str:
    """Explain a failed MCP call to the model in terms it can act on.

    Several of the exceptions this path sees stringify to nothing at all —
    httpx.ReadTimeout is the common one — so interpolating str(e) produced
    "Failed to call MCP tool:" and stopped. That tells the model no more than
    silence would, and it is exactly the case where the model most needs to
    know whether to retry, use a different tool, or give up.
    """
    import httpx

    if tool is None:
        subject = _bounded_meta_text(
            server_or_canonical,
            MCP_FAILURE_IDENTITY_CHARS,
        ) or "unknown MCP capability"
    else:
        server_hint = _bounded_meta_text(
            server_or_canonical,
            MCP_FAILURE_IDENTITY_CHARS // 2,
        )
        tool_hint = _bounded_meta_text(
            tool,
            MCP_FAILURE_IDENTITY_CHARS // 2,
        )
        subject = f"{server_hint or '?'}/{tool_hint or '?'}"

    if isinstance(e, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
                      asyncio.TimeoutError)):
        message = (
            f"MCP tool {subject} timed out. The server did not answer in time.\n"
            f"This tool may be too slow for a single call — try a narrower request, "
            f"or a different tool that returns less."
        )
    elif isinstance(e, httpx.ConnectError):
        message = (
            f"Could not reach the MCP server for '{subject}'. It may be disconnected — "
            f"check the skill centre, or reconnect it and retry."
        )
    else:
        detail = _bounded_meta_text(str(e).strip(), MCP_FAILURE_DETAIL_CHARS)
        # Fall back to the type when the message is empty, so the reader always
        # has a name to search for.
        message = f"MCP tool {subject} failed: {detail or type(e).__name__}"
    return _bounded_utf8_text(message, MCP_FAILURE_MAX_BYTES)[0]


def _mcp_failure_result(
    e: Exception,
    *,
    canonical_id: str,
    raw_server: str,
    raw_tool: str,
    title_prefix: str,
) -> ToolResult:
    """Build one bounded error result without reflecting raw MCP identities."""

    detail = str(e).strip()
    identity_was_oversized = (
        len(raw_server) > MCP_FAILURE_IDENTITY_CHARS
        or len(raw_tool) > MCP_FAILURE_IDENTITY_CHARS
    )
    detail_was_oversized = len(detail) > MCP_FAILURE_DETAIL_CHARS
    title, title_truncated = _bounded_utf8_text(
        f"{title_prefix}: {canonical_id}",
        MCP_FAILURE_IDENTITY_CHARS + 40,
    )
    output, output_truncated = _bounded_utf8_text(
        _describe_failure(e, canonical_id),
        MCP_FAILURE_MAX_BYTES,
    )
    return ToolResult(
        title=title,
        output=output,
        metadata={
            "truncated": bool(
                identity_was_oversized
                or detail_was_oversized
                or title_truncated
                or output_truncated
            ),
            "identity_redacted": True,
        },
    )


def _mcp_result_title(canonical_id: str, *, is_error: bool) -> str:
    """Return a bounded display title without reflecting sandbox identity."""

    return f"{'Error: ' if is_error else ''}{canonical_id}"


def _sanitize_tool_name(server: str, name: str) -> str:
    """Build a sanitized tool ID: only [a-zA-Z0-9_-], max 64 chars.

    Matches opencode's sanitization: replace non-alphanumeric with '_'.
    """
    safe_server = re.sub(r"[^a-zA-Z0-9_-]", "_", server)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    tool_id = f"mcp_{safe_server}_{safe_name}"
    if len(tool_id) > MAX_TOOL_NAME_LEN:
        tool_id = tool_id[:MAX_TOOL_NAME_LEN]
    return tool_id


def _sanitize_schema(schema: Any) -> Any:
    """Recursively fix JSON Schema issues that OpenAI/Gemini reject.

    Fixes applied (matching opencode's sanitizeGemini):
    - array type missing 'items' → add { "type": "string" }
    - items is empty object without type → set type to "string"
    - required array references non-existent properties → filter
    """
    if schema is None or not isinstance(schema, dict):
        return schema

    result = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            result[key] = _sanitize_schema(value)
        elif isinstance(value, list):
            result[key] = [_sanitize_schema(v) if isinstance(v, dict) else v for v in value]
        else:
            result[key] = value

    # Fix: array without items
    if result.get("type") == "array":
        if "items" not in result or result["items"] is None:
            result["items"] = {"type": "string"}
        elif isinstance(result["items"], dict) and not result["items"].get("type"):
            result["items"]["type"] = "string"

    # Fix: required references non-existent properties
    if result.get("type") == "object" and "properties" in result and isinstance(result.get("required"), list):
        result["required"] = [f for f in result["required"] if f in result["properties"]]

    return result


def _make_raw_schema_model(input_schema: dict) -> type[BaseModel]:
    """Create a Pydantic BaseModel subclass that returns the raw MCP input_schema
    from model_json_schema().

    This lets the LLM see the full JSON Schema (including const, enum, anyOf,
    default values, etc.) instead of a lossy Pydantic-generated schema.
    The actual validation is done by the MCP server, not locally.
    """
    # Ensure it's a proper object schema and sanitize
    schema = dict(input_schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema = _sanitize_schema(schema)

    class McpRawSchemaModel(BaseModel):
        _raw_schema: ClassVar[dict] = schema

        @classmethod
        def model_json_schema(cls, **_kwargs: Any) -> dict:
            """Return an isolated copy of the cached normalized schema."""
            return copy.deepcopy(cls._raw_schema)

    return McpRawSchemaModel


def _mcp_normalization_cache_key(sandbox: Any) -> str | None:
    """Key immutable raw catalogue bytes without retaining scope secrets.

    The action-server ``mcp_generation`` is a digest of the complete connected
    server/tool/schema projection.  A legacy client has no such authority and
    therefore stays uncached rather than risking a stale schema binding.
    """

    snapshot = getattr(sandbox, "_snapshot", None)
    if not isinstance(snapshot, dict):
        return None
    generation = snapshot.get("mcp_generation")
    if not isinstance(generation, str) or not generation:
        return None

    source = getattr(sandbox, "_sandbox", sandbox)

    def digest(value: object) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    # Object identity is deliberate: manager-owned clients are tenant scoped,
    # and two live clients must never share cached raw closures merely because
    # they point at the same URL. Explicit dimensions strengthen test doubles
    # and future clients that expose richer account/project metadata.
    scope = (
        f"{type(source).__module__}.{type(source).__qualname__}",
        str(id(source)),
        digest(getattr(source, "user_id", "")),
        digest(getattr(source, "account_id", "")),
        digest(getattr(source, "project_id", "")),
        digest(getattr(source, "sandbox_id", "")),
        digest(getattr(source, "base_url", "")),
        digest(getattr(source, "region", "")),
        digest(getattr(source, "api_key", "")),
        digest(snapshot.get("boot_id", "")),
        digest(snapshot.get("generation", "")),
        digest(generation),
        MCP_NORMALIZATION_DIALECT,
    )
    return hashlib.sha256(_length_prefixed(scope)).hexdigest()


def _clear_mcp_normalization_cache() -> None:
    """Test/support hook; production eviction is TTL + bounded LRU."""

    with _MCP_NORMALIZATION_LOCK:
        _MCP_NORMALIZATION_CACHE.clear()
        _MCP_NORMALIZATION_INFLIGHT.clear()


def _build_normalization_artifacts(
    mcp_tools: list[dict],
) -> _McpNormalizationArtifacts:
    """Normalize one detached raw generation exactly once."""

    # The projection is already a copy-on-read value, but detach once more at
    # the cache ownership boundary so a legacy/fake caller cannot mutate a
    # cached binding after construction.
    bindings = tuple(_build_bindings(copy.deepcopy(mcp_tools)))
    search_index = _build_meta_search_index(list(bindings))
    return _McpNormalizationArtifacts(
        bindings=bindings,
        search_index=search_index,
        # Schema models are populated only after current-call permission
        # filtering decides a binding may be materialized directly.
        schema_models=(),
    )


async def _build_and_cache_normalization(
    cache_key: str,
    inflight_key: tuple[int, str],
    mcp_tools: list[dict],
) -> _McpNormalizationArtifacts:
    """Singleflight worker; exceptions leave neither cache nor negative entry."""

    try:
        artifacts = _build_normalization_artifacts(mcp_tools)
        entry = _McpNormalizationCacheEntry(
            artifacts=artifacts,
            expires_at=_mcp_normalization_now() + MCP_NORMALIZATION_CACHE_TTL_SECONDS,
        )
        with _MCP_NORMALIZATION_LOCK:
            _MCP_NORMALIZATION_CACHE[cache_key] = entry
            _MCP_NORMALIZATION_CACHE.move_to_end(cache_key)
            while len(_MCP_NORMALIZATION_CACHE) > MCP_NORMALIZATION_CACHE_MAX_ENTRIES:
                _MCP_NORMALIZATION_CACHE.popitem(last=False)
        return artifacts
    finally:
        with _MCP_NORMALIZATION_LOCK:
            current = _MCP_NORMALIZATION_INFLIGHT.get(inflight_key)
            if current is asyncio.current_task():
                _MCP_NORMALIZATION_INFLIGHT.pop(inflight_key, None)


async def _get_normalization_artifacts(
    sandbox: Any,
    mcp_tools: list[dict],
) -> _McpNormalizationArtifacts | None:
    """Read a generation-scoped cache, or return None for legacy catalogues."""

    cache_key = _mcp_normalization_cache_key(sandbox)
    if cache_key is None:
        return None

    now = _mcp_normalization_now()
    loop = asyncio.get_running_loop()
    inflight_key = (id(loop), cache_key)
    with _MCP_NORMALIZATION_LOCK:
        expired = [
            key
            for key, value in _MCP_NORMALIZATION_CACHE.items()
            if value.expires_at <= now
        ]
        for key in expired:
            _MCP_NORMALIZATION_CACHE.pop(key, None)
        cached = _MCP_NORMALIZATION_CACHE.get(cache_key)
        if cached is not None:
            _MCP_NORMALIZATION_CACHE.move_to_end(cache_key)
            return cached.artifacts
        task = _MCP_NORMALIZATION_INFLIGHT.get(inflight_key)
        if task is None:
            task = loop.create_task(
                _build_and_cache_normalization(
                    cache_key,
                    inflight_key,
                    mcp_tools,
                )
            )
            _MCP_NORMALIZATION_INFLIGHT[inflight_key] = task
    return await asyncio.shield(task)


def _get_cached_schema_models(
    cache_key: str,
    bindings: list[_McpBinding],
) -> dict[str, type[BaseModel]]:
    """Normalize only permitted direct schemas and cache successful models."""

    with _MCP_NORMALIZATION_LOCK:
        entry = _MCP_NORMALIZATION_CACHE.get(cache_key)
        existing = dict(entry.artifacts.schema_models) if entry is not None else {}

    built: dict[str, type[BaseModel]] = {}
    for binding in bindings:
        if binding.canonical_id in existing:
            continue
        try:
            built[binding.canonical_id] = _make_raw_schema_model(binding.input_schema)
        except Exception as exc:
            # Never negative-cache malformed or transient normalization
            # failures; a later call may recover, while this call fails the
            # direct definition closed or falls back to meta.
            log.warning(
                "Failed to normalize MCP schema %s error_type=%s",
                binding.canonical_id,
                type(exc).__name__,
            )

    if built:
        with _MCP_NORMALIZATION_LOCK:
            current = _MCP_NORMALIZATION_CACHE.get(cache_key)
            if current is not None:
                merged = dict(current.artifacts.schema_models)
                merged.update(built)
                updated = _McpNormalizationArtifacts(
                    bindings=current.artifacts.bindings,
                    search_index=current.artifacts.search_index,
                    schema_models=tuple(sorted(merged.items())),
                )
                _MCP_NORMALIZATION_CACHE[cache_key] = _McpNormalizationCacheEntry(
                    artifacts=updated,
                    expires_at=current.expires_at,
                )
                _MCP_NORMALIZATION_CACHE.move_to_end(cache_key)
                existing = merged
            else:
                existing.update(built)
    return {
        binding.canonical_id: existing[binding.canonical_id]
        for binding in bindings
        if binding.canonical_id in existing
    }


def _make_mcp_executor(server_name: str, tool_name: str, canonical_id: str):
    """Create an executor function for a specific MCP tool.

    Arguments from the LLM are forwarded directly to the MCP server
    without local Pydantic validation — the MCP server handles validation.
    """

    async def executor(args, ctx: ToolContext) -> ToolResult:
        if not ctx.sandbox:
            return ToolResult(
                title=f"MCP tool error: {canonical_id}",
                output="No sandbox available for MCP tool execution.",
                metadata={"identity_redacted": True},
            )
        try:
            # args is a raw dict from the agent loop
            if hasattr(args, "model_dump"):
                arguments = args.model_dump()
            elif isinstance(args, dict):
                arguments = args
            else:
                arguments = dict(args)

            # Remove None values but keep False, 0, empty string, etc.
            arguments = {k: v for k, v in arguments.items() if v is not None}

            blocked = await _authorize_underlying(ctx, canonical_id, arguments)
            if blocked is not None:
                return blocked

            log.info(
                "Calling MCP tool %s argument_count=%s",
                canonical_id,
                len(arguments),
            )
            result = await ctx.sandbox.call_mcp_tool(
                server_name, tool_name, arguments,
            )
            log.info(
                "MCP tool %s returned is_error=%s",
                canonical_id,
                bool(result.get("isError")),
            )

            # Pass raw MCP result to LLM, with truncation.
            # If truncated, save full output ONLY to container (not host).
            import json as _json
            raw_output = _json.dumps(result, ensure_ascii=False, default=str)
            from tool.truncation import MAX_BYTES, MAX_LINES
            raw_bytes = len(raw_output.encode("utf-8"))
            raw_lines = raw_output.count("\n") + 1

            if raw_bytes > MAX_BYTES or raw_lines > MAX_LINES:
                safe_id = canonical_id.removeprefix(MCP_CANONICAL_PREFIX)
                saved_path = f"{ctx.workdir}/.mcp_output_{safe_id}_{int(time.time())}.json"
                try:
                    await ctx.sandbox.write_file(saved_path, raw_output)
                except Exception:
                    saved_path = None
                # Truncate: keep first portion as preview
                lines = raw_output.split("\n")
                preview_lines = []
                byte_count = 0
                for line in lines:
                    size = len(line.encode("utf-8")) + 1
                    if byte_count + size > MAX_BYTES or len(preview_lines) >= MAX_LINES:
                        break
                    preview_lines.append(line)
                    byte_count += size
                preview = "\n".join(preview_lines)
                hint = f"\n\nFull output saved to: {saved_path}\nUse the read tool with offset/limit to view specific sections." if saved_path else ""
                return ToolResult(
                    title=_mcp_result_title(
                        canonical_id,
                        is_error=bool(result.get("isError")),
                    ),
                    output=preview + hint,
                    metadata={"truncated": True},
                )

            return ToolResult(
                title=_mcp_result_title(
                    canonical_id,
                    is_error=bool(result.get("isError")),
                ),
                output=raw_output,
            )
        except Exception as e:
            # Exception messages from remote clients can embed full URLs or
            # rejected argument values. The model receives a bounded actionable
            # error below; shared logs keep only the exception class.
            log.error(
                "MCP tool %s failed: %s",
                canonical_id,
                type(e).__name__,
            )
            return _mcp_failure_result(
                e,
                canonical_id=canonical_id,
                raw_server=server_name,
                raw_tool=tool_name,
                title_prefix="MCP tool error",
            )

    # Internal-only identity metadata used by catalogue/wire contract tests and
    # collision-safe merge code; it is never serialized to the provider.
    executor._mcp_canonical_id = canonical_id
    executor._mcp_raw_identity = (server_name, tool_name)
    return executor


# When MCP tools exceed this threshold, switch to the two meta-tool pattern
# (mcp_find_tool + mcp_call_tool) instead of registering each tool individually.
# This reduces token usage by ~95% (2 tools vs hundreds).
# Reference: https://docs.litellm.ai/docs/mcp_semantic_filter
#            https://dev.to/stacklok/cut-token-waste-from-your-ai-workflow-with-the-toolhive-mcp-optimizer-3oo6
MCP_META_TOOL_THRESHOLD = 40


def _create_direct_tools(
    bindings: list[_McpBinding],
    schema_models: dict[str, type[BaseModel]] | None = None,
) -> dict[str, ToolInfo]:
    """Materialize permission-filtered bindings without editing their schemas."""
    tools: dict[str, ToolInfo] = {}
    for binding in bindings:
        try:
            param_model = (schema_models or {}).get(binding.canonical_id)
            if param_model is None:
                param_model = _make_raw_schema_model(binding.input_schema)
        except Exception as exc:
            log.warning(
                "Failed to create MCP schema %s error_type=%s",
                binding.canonical_id,
                type(exc).__name__,
            )
            continue

        executor = _make_mcp_executor(
            binding.server,
            binding.name,
            binding.canonical_id,
        )
        tools[binding.provider_name] = ToolInfo(
            id=binding.provider_name,
            description=f"[MCP:{binding.server}] {binding.description}",
            parameters=param_model,
            execute=executor,
            sandbox_required=True,
            source="mcp",
            plane="sandbox",
            canonical_id=binding.canonical_id,
            provider_name=binding.provider_name,
            pack=None,
            same_response_safe=False,
        )
    return tools


def _direct_payload_requires_meta(tools: dict[str, ToolInfo]) -> tuple[bool, str]:
    """Apply the same serialized definition budgets used by provider payloads.

    A recursive or otherwise unsupported schema also takes the meta path: the
    MCP server remains the schema validator, while OpenBox avoids crashing the
    provider serializer or silently cutting the schema.
    """
    try:
        from agent.tool_payload import measure_tool_definitions

        metrics = measure_tool_definitions(tools, "responses")
    except Exception as exc:
        log.warning(
            "MCP direct schema measurement failed error_type=%s; using meta tools",
            type(exc).__name__,
        )
        return True, "schema_not_directly_serializable"

    if any(
        item.definition_chars > MCP_DIRECT_SINGLE_HARD_CHARS
        for item in metrics.items
    ):
        return True, "single_definition_hard_limit"
    if metrics.catalogue_wire_definition_chars > MCP_DIRECT_TOTAL_HARD_CHARS:
        return True, "catalogue_definition_hard_limit"
    return False, ""


def _create_meta_tools(
    bindings: list[_McpBinding],
    sandbox_ref,
    *,
    agent_id: str,
    generation: str,
    search_index: tuple[_McpMetaIndexEntry, ...] | None = None,
) -> dict[str, ToolInfo]:
    """Create two meta-tools (find + call) that replace hundreds of individual tools.

    Pattern from ToolHive MCP Optimizer / Speakeasy Dynamic Toolsets:
    - mcp_find_tool: Search tools by keyword, returns matching tool names + schemas
    - mcp_call_tool: Call an evidenced exact canonical ID with arguments
    """
    # The index is permission-filtered before construction and keyed solely by
    # the security identity. Raw provider names are never accepted as lookup
    # keys by the call tool.
    tool_index = {binding.canonical_id: binding for binding in bindings}
    if search_index is None:
        search_index = _build_meta_search_index(bindings)
    sandbox_id = _sandbox_identity(sandbox_ref)
    server_hints = sorted({
        entry.display_server for entry in search_index if entry.display_server
    })[:MCP_META_INDEX_SERVER_LIMIT]
    server_summary = ", ".join(
        json.dumps(server, ensure_ascii=False) for server in server_hints
    )

    # --- mcp_find_tool ---
    class FindToolParams(BaseModel):
        query: str = Field(
            max_length=500,
            description="Search keyword to find relevant MCP tools",
        )
        server: str = Field(
            default="",
            max_length=500,
            description=(
                "Optional exact server filter. Bounded server hints: "
                f"{server_summary or '(none)'}"
            ),
        )

    async def find_executor(args, ctx: ToolContext) -> ToolResult:
        params = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        query = params.get("query", "").casefold().strip()
        server_filter = params.get("server", "")

        max_calls = max(1, int(ctx._capability_max_search_calls))
        max_reveals = max(1, int(ctx._capability_max_reveals))
        max_result_chars = max(100, int(ctx._capability_max_result_chars))
        if ctx._capability_search_calls >= max_calls:
            return ToolResult(
                title="MCP search limit reached",
                output="This step already used the bounded capability-search limit.",
                metadata={"blocked": True},
            )
        ctx._capability_search_calls += 1
        remaining_ids = max_reveals - len(ctx._capability_revealed_ids)
        remaining_chars = max_result_chars - ctx._capability_result_chars
        if remaining_ids <= 0 or remaining_chars <= 0:
            return ToolResult(
                title="MCP reveal limit reached",
                output="The bounded capability-result budget for this step is exhausted.",
                metadata={"blocked": True},
            )

        # Split query into keywords for OR matching
        # "tiktok video search" → matches tools containing "tiktok" OR "video" OR "search"
        keywords = [k for k in query.split() if len(k) >= 2]

        # Special: empty/generic queries like "list tools", "all", "help" → show categories
        if not keywords or query in ("list tools", "list", "all", "help", "tools"):
            # Return category summary
            categories: dict[str, int] = {}
            for entry in search_index:
                if server_filter and entry.binding.server != server_filter:
                    continue
                categories[entry.category] = categories.get(entry.category, 0) + 1
            sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
            lines = [
                f"  {json.dumps(cat, ensure_ascii=False)}: {count} tools"
                for cat, count in sorted_cats[:30]
            ]
            output = (
                f"{len(tool_index)} tools available across {len(categories)} categories.\n"
                f"Search with specific keywords like 'tiktok', 'youtube', 'download', 'search', etc.\n\n"
                f"Top categories:\n" + "\n".join(lines)
            )
            output = output[:remaining_chars]
            ctx._capability_result_chars += len(output)
            return ToolResult(title=f"{len(tool_index)} MCP tools available", output=output)

        matches = []
        for entry in search_index:
            if server_filter and entry.binding.server != server_filter:
                continue
            # Score: how many keywords match
            score = sum(1 for k in keywords if k in entry.searchable)
            if score > 0:
                matches.append((score, entry))

        # Stable ordering prevents a reconnect or provider order change from
        # changing which bounded subset receives discovery evidence.
        matches.sort(key=lambda item: (-item[0], item[1].binding.canonical_id))

        if not matches:
            return ToolResult(
                title="No tools found",
                output="No permitted MCP tools matched. Try narrower capability keywords.",
            )

        # Only entries actually returned receive evidence. Keeping this local
        # and bounded also removes the old secondary-LLM path, whose free-form
        # output could not be bound unambiguously to server/tool identities.
        selected: list[_McpBinding] = []
        results: list[str] = []
        header = f"Found {len(matches)} tools. Use mcp_call_tool with an EXACT canonical_id returned below:\n"
        used_chars = len(header)
        for _score, entry in matches:
            binding = entry.binding
            block = (
                f"  canonical_id: \"{binding.canonical_id}\"\n"
                f"  server: {json.dumps(entry.display_server, ensure_ascii=False)}\n"
                f"  tool: {json.dumps(entry.display_name, ensure_ascii=False)}\n"
                f"  description: {json.dumps(entry.description_hint, ensure_ascii=False)}"
            )
            separator_chars = 5 if results else 0
            available_chars = remaining_chars - used_chars - separator_chars
            if len(block) > available_chars:
                continue

            if not entry.parameter_details:
                no_parameters = "\n  (no parameters)"
                if len(block) + len(no_parameters) <= available_chars:
                    block += no_parameters
            else:
                parameter_header = "\n  parameters:"
                if len(block) + len(parameter_header) <= available_chars:
                    block += parameter_header
                    rendered_parameters = 0
                    for pname, ptype, pdesc, required in entry.parameter_details:
                        req = " (required)" if required else ""
                        detail = (
                            f"\n    {json.dumps(pname, ensure_ascii=False)}: {ptype}{req}"
                            + (
                                f" - {json.dumps(pdesc, ensure_ascii=False)}"
                                if pdesc else ""
                            )
                        )
                        if len(block) + len(detail) > available_chars:
                            break
                        block += detail
                        rendered_parameters += 1
                    if rendered_parameters < len(entry.parameter_details):
                        omitted = "\n    ... bounded parameter hint truncated"
                        if len(block) + len(omitted) <= available_chars:
                            block += omitted

            selected.append(binding)
            results.append(block)
            used_chars += separator_chars + len(block)
            if len(selected) >= remaining_ids:
                break

        if not selected:
            output = "Matching MCP tools exceeded the remaining bounded result budget. Use a narrower query."
            output = output[:remaining_chars]
            ctx._capability_result_chars += len(output)
            return ToolResult(title="MCP result budget exhausted", output=output)

        for binding in selected:
            _record_evidence(
                ctx,
                agent_id=agent_id,
                sandbox_id=sandbox_id,
                generation=generation,
                binding=binding,
            )
        output = header + "\n---\n".join(results)
        ctx._capability_revealed_ids.update(
            binding.canonical_id for binding in selected
        )
        ctx._capability_result_chars += len(output)

        return ToolResult(title=f"Found {len(matches)} MCP tools", output=output)

    find_tool = ToolInfo(
        id="mcp_find_tool",
        description=(
            f"Search {len(bindings)} available MCP tools by keyword. Returns canonical IDs, descriptions, and parameter schemas. "
            f"Bounded server hints: {server_summary or '(none)'}. Use this FIRST to find the right tool, then use mcp_call_tool to execute it."
        ),
        parameters=FindToolParams,
        execute=find_executor,
        sandbox_required=True,
        never_prune=True,
        source="mcp",
        plane="sandbox",
        canonical_id="mcp_find_tool",
        provider_name="mcp_find_tool",
        pack=None,
        same_response_safe=False,
    )

    # --- mcp_call_tool ---
    class CallToolParams(BaseModel):
        canonical_id: str = Field(description="Exact canonical_id returned by mcp_find_tool")
        arguments: dict = Field(default_factory=dict, description="Tool arguments as JSON object")

    async def call_executor(args, ctx: ToolContext) -> ToolResult:
        if not ctx.sandbox:
            return ToolResult(title="Error", output="No sandbox available")
        params = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        canonical_id = params.get("canonical_id", "")
        arguments = params.get("arguments", {})

        # Remove None values
        if isinstance(arguments, dict):
            arguments = {k: v for k, v in arguments.items() if v is not None}
        else:
            return ToolResult(
                title="Invalid MCP arguments",
                output="MCP arguments must be a JSON object.",
                metadata={"blocked": True},
            )

        binding = tool_index.get(canonical_id)
        if binding is None:
            return ToolResult(
                title="MCP tool unavailable",
                output="That exact MCP canonical_id is not available. Use mcp_find_tool again.",
                metadata={"blocked": True},
            )
        if not _has_evidence(
            ctx,
            agent_id=agent_id,
            sandbox_id=sandbox_id,
            generation=generation,
            binding=binding,
        ):
            return ToolResult(
                title="MCP discovery required",
                output="This MCP tool is not authorized by current discovery evidence. Use mcp_find_tool first.",
                metadata={"blocked": True},
            )

        blocked = await _authorize_underlying(ctx, binding.canonical_id, arguments)
        if blocked is not None:
            return blocked

        try:
            log.info(
                "Calling MCP tool %s argument_count=%s",
                binding.canonical_id,
                len(arguments),
            )
            result = await ctx.sandbox.call_mcp_tool(
                binding.server, binding.name, arguments
            )
            import json as _json
            raw_output = _json.dumps(result, ensure_ascii=False, default=str)

            # Truncate large MCP results to prevent context explosion.
            # Save full output ONLY to container (not host) so LLM can read it.
            from tool.truncation import MAX_BYTES, MAX_LINES
            raw_bytes = len(raw_output.encode("utf-8"))
            raw_lines = raw_output.count("\n") + 1

            if raw_bytes > MAX_BYTES or raw_lines > MAX_LINES:
                # Save full output to container only
                safe_id = binding.canonical_id.removeprefix(MCP_CANONICAL_PREFIX)
                saved_path = f"{ctx.workdir}/.mcp_output_{safe_id}_{int(time.time())}.json"
                try:
                    await ctx.sandbox.write_file(saved_path, raw_output)
                except Exception:
                    saved_path = None

                # Truncate: keep first portion as preview
                lines = raw_output.split("\n")
                preview_lines = []
                byte_count = 0
                for line in lines:
                    size = len(line.encode("utf-8")) + 1
                    if byte_count + size > MAX_BYTES or len(preview_lines) >= MAX_LINES:
                        break
                    preview_lines.append(line)
                    byte_count += size
                preview = "\n".join(preview_lines)

                hint = f"\n\nFull output saved to: {saved_path}\nUse the read tool with offset/limit to view specific sections." if saved_path else ""
                return ToolResult(
                    title=_mcp_result_title(
                        binding.canonical_id,
                        is_error=bool(result.get("isError")),
                    ),
                    output=preview + hint,
                    metadata={"truncated": True},
                )

            return ToolResult(
                title=_mcp_result_title(
                    binding.canonical_id,
                    is_error=bool(result.get("isError")),
                ),
                output=raw_output,
            )
        except Exception as e:
            log.error(
                "MCP tool %s failed: %s",
                binding.canonical_id,
                type(e).__name__,
            )
            return _mcp_failure_result(
                e,
                canonical_id=binding.canonical_id,
                raw_server=binding.server,
                raw_tool=binding.name,
                title_prefix="MCP call failed",
            )

    call_tool = ToolInfo(
        id="mcp_call_tool",
        description=(
            "Call an MCP tool by the exact canonical_id returned by mcp_find_tool. "
            "Pass arguments as a JSON object matching the discovered parameter schema."
        ),
        parameters=CallToolParams,
        execute=call_executor,
        sandbox_required=True,
        source="mcp",
        plane="sandbox",
        canonical_id="mcp_call_tool",
        provider_name="mcp_call_tool",
        pack=None,
        same_response_safe=False,
    )

    return {"mcp_find_tool": find_tool, "mcp_call_tool": call_tool}


async def create_mcp_tools(
    sandbox,
    ruleset: list | None = None,
    *,
    agent_id: str = "",
) -> dict[str, ToolInfo]:
    """Fetch MCP tools from the container and create ToolInfo wrappers.

    Two modes based on tool count:
    - <= MCP_META_TOOL_THRESHOLD: Register each tool individually (full schema)
    - > MCP_META_TOOL_THRESHOLD: Register 2 meta-tools (find + call) to avoid
      token explosion. This reduces token usage by ~95%.
    """
    tools: dict[str, ToolInfo] = {}
    try:
        mcp_tools = await sandbox.list_mcp_tools()
    except Exception as e:
        log.debug(
            "Failed to list MCP tools from container error_type=%s",
            type(e).__name__,
        )
        return tools

    if not isinstance(mcp_tools, list):
        log.warning("Rejected malformed MCP tool catalogue")
        return tools

    cache_key = _mcp_normalization_cache_key(sandbox)
    try:
        artifacts = await _get_normalization_artifacts(sandbox, mcp_tools)
    except Exception as exc:
        # A failed normalization is not a durable empty catalogue. Fail this
        # step closed and let the next step retry the same generation.
        log.warning(
            "Failed to normalize MCP catalogue error_type=%s",
            type(exc).__name__,
        )
        return tools

    if artifacts is None:
        # Legacy projections have no authoritative raw-schema generation and
        # therefore retain the safe uncached path.
        all_bindings = _build_bindings(mcp_tools)
        cached_search_index = None
        cached_schema_models: dict[str, type[BaseModel]] = {}
    else:
        all_bindings = list(artifacts.bindings)
        cached_search_index = artifacts.search_index
        cached_schema_models = dict(artifacts.schema_models)

    # Permission conclusions are deliberately absent from the cache. Resolve
    # policy for every call, then select only permitted normalized artifacts.
    bindings = _filter_permitted_bindings(all_bindings, ruleset)
    permitted_ids = {binding.canonical_id for binding in bindings}
    search_index = (
        tuple(
            entry
            for entry in cached_search_index
            if entry.binding.canonical_id in permitted_ids
        )
        if cached_search_index is not None
        else None
    )
    total = len(bindings)
    generation = _catalogue_generation(bindings)

    # Large tool sets: use meta-tool pattern (2 tools instead of hundreds).
    # Count is not sufficient: one huge schema or a smaller catalogue whose
    # serialized definitions exceed the hard budget must remain discoverable
    # through the same meta path rather than be truncated or disappear.
    if total > MCP_META_TOOL_THRESHOLD:
        log.info(
            "MCP has %s permitted tools (>%s), using meta-tool pattern",
            total,
            MCP_META_TOOL_THRESHOLD,
        )
        return _create_meta_tools(
            bindings,
            sandbox,
            agent_id=agent_id,
            generation=generation,
            search_index=search_index,
        )

    # Small tool sets are direct only while their exact provider serialization
    # is within the same immutable schema budgets as built-ins.
    if cache_key is not None:
        cached_schema_models = _get_cached_schema_models(cache_key, bindings)
    tools = _create_direct_tools(bindings, cached_schema_models)
    requires_meta, reason = _direct_payload_requires_meta(tools)
    if requires_meta:
        log.info(
            "MCP direct catalogue uses meta-tool pattern reason=%s permitted_count=%s",
            reason,
            total,
        )
        return _create_meta_tools(
            bindings,
            sandbox,
            agent_id=agent_id,
            generation=generation,
            search_index=search_index,
        )

    if tools:
        log.info(f"Loaded {len(tools)} MCP tools (direct mode)")

    return tools


def create_mcp_resource_tool() -> ToolInfo:
    """Create a generic tool for reading MCP resources."""

    class ReadResourceParams(BaseModel):
        server: str = Field(description="MCP server name that owns the resource")
        uri: str = Field(description="Resource URI to read")

    async def executor(args, ctx: ToolContext) -> ToolResult:
        if not ctx.sandbox:
            return ToolResult(title="Error", output="No sandbox available")
        try:
            params = ReadResourceParams.model_validate(
                args.model_dump() if hasattr(args, "model_dump") else args
            ).model_dump()
            result = await ctx.sandbox.read_mcp_resource(params["server"], params["uri"])
            contents = result.get("contents", []) if isinstance(result, dict) else []
            output, truncated = _bounded_resource_contents(contents)
            return ToolResult(
                title=f"Resource: {_bounded_meta_text(params['uri'], MCP_RESOURCE_TITLE_CHARS)}",
                output=output,
                metadata={"truncated": truncated},
            )
        except Exception as e:
            log.error("MCP resource read failed error_type=%s", type(e).__name__)
            return ToolResult(
                title="Resource read error",
                output="The MCP resource could not be read safely.",
                metadata={"error": True},
            )

    return ToolInfo(
        id="mcp_read_resource",
        description="Read a resource from a connected MCP server by URI. Use this to fetch context from MCP resources.",
        parameters=ReadResourceParams,
        execute=executor,
        sandbox_required=True,
        source="mcp",
        plane="sandbox",
        canonical_id="mcp_read_resource",
        provider_name="mcp_read_resource",
        pack=None,
        same_response_safe=False,
    )
