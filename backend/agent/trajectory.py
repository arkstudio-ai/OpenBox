"""Explicit trajectory capture at model and executor boundaries.

Provider request bodies and response chunks are recorded verbatim, so a prompt
can be debugged from its trace; only the transport settings and credentials among
a call's keyword arguments are left out, by name. Capture is fail-open: it opens
no database transaction, retains no media bytes and never makes a provider call
or a delivered chunk wait for the recorder.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from core.identifier import ascending
from core.log import create_logger

log = create_logger("agent.trajectory")

#: Transport settings and credentials among a provider call's keyword arguments
#: (and a service body's top-level fields): left out of the recorded request,
#: which names them in ``omitted_fields``.
TRANSPORT_FIELDS = frozenset({
    "api_key", "api_base", "base_url", "organization", "headers", "extra_headers", "default_headers",
    "extra_query", "timeout", "max_retries", "client", "http_client",
    "authorization", "cookie", "cookies", "access_token", "refresh_token", "security_token",
    "credential", "credentials",
})
#: Names ``public_value`` leaves out of tool arguments, metadata and usage; provider bodies keep them.
PRIVATE_FIELDS = frozenset({
    "api_key", "authorization", "headers", "extra_headers", "cookie", "cookies",
    "access_token", "refresh_token", "security_token", "password", "secret",
    "signature", "encrypted_content", "provider_metadata", "provider_specific_fields",
    "provider_replay", "provider_binding_digest", "provider_account_id",
    "canonical_tool_id", "provider_dialect", "credential", "credentials",
})


SCHEMA_KEYS = frozenset({"schema", "parameters", "inputschema", "outputschema", "jsonschema"})


def _schema_node(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        (isinstance(value.get("type"), str) and value["type"] in {"object", "array", "string", "number", "integer", "boolean", "null"})
        or isinstance(value.get("type"), list)
        or any(key in value for key in ("properties", "$ref", "oneOf", "anyOf", "allOf")))


def public_value(value: Any) -> Any:
    """Serialize known public fields, excluding nested SDK/private state."""
    return _public(value, schema=False)


def _public(value: Any, *, schema: bool) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        # A JSON Schema is a public input contract: a property called password
        # is kept with its definition, not excluded as a credential.
        return {
            str(key): _public(item, schema=schema or (
                str(key).replace("_", "").lower() in SCHEMA_KEYS and _schema_node(item)))
            for key, item in value.items()
            if schema or (not str(key).startswith("_") and str(key).lower() not in PRIVATE_FIELDS)
        }
    if isinstance(value, (list, tuple)):
        return [_public(item, schema=schema) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Unknown SDK objects can carry headers and credentials in repr/__dict__.
    return {"availability": "not_recorded", "reason": "unsupported_public_value"}


def _transport(key: Any) -> bool:
    return str(key).lower() in TRANSPORT_FIELDS


def provider_body(value: Any) -> Any:
    """A provider request body as JSON values, verbatim at any depth: an SDK model becomes its JSON dump.

    A value JSON cannot hold becomes a ``not_recorded`` marker when the event is encoded (SPEC §5.4).
    """
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            return {"availability": "not_recorded", "reason": "unsupported_value"}
    if isinstance(value, Mapping):
        return {key: provider_body(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [provider_body(item) for item in value]
    return value


def request_snapshot(kwargs: Mapping[str, Any]) -> dict:
    """The complete body a provider call sends, without its transport settings and credentials."""
    snapshot = {key: provider_body(value) for key, value in kwargs.items() if not _transport(key)}
    omitted = sorted(str(key) for key in kwargs if _transport(key))
    if omitted:
        snapshot["omitted_fields"] = omitted
    return snapshot


def _compact_inline_media(value, media: Mapping[str, dict]):
    """Capture owned image bytes by reference before serializing the spool event.

    Only exact inputs resolved from an asset are replaced. Unknown media and
    every other provider field stay verbatim; the provider's body is untouched.
    The worker converts this producer marker to the usual protected $media ref.
    """
    if isinstance(value, str):
        ref = media.get(value)
        return {"$asset_media": dict(ref)} if ref is not None else value
    if isinstance(value, Mapping):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            ref = media.get(f"data:{value.get('media_type')};base64,{value['data']}")
            if ref is not None:
                return {"$asset_media": dict(ref)}
        return {key: _compact_inline_media(item, media) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_inline_media(item, media) for item in value]
    return value


def provider_output_snapshot(value: Any) -> Any:
    """A provider chunk or response exactly as it arrived: an SDK model's JSON dump, anything else as it is.

    Nothing is filtered or copied. Events are encoded when they are recorded
    (SPEC §5.3), so an adapter changing the chunk afterwards cannot change the
    recorded one; a value JSON cannot hold becomes a ``not_recorded`` marker (§5.4).
    """
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return {"availability": "not_recorded", "reason": "unsupported_value"}
    return value


_warned: set[str] = set()


def _not_recorded(reason: str, exc: BaseException | None = None) -> None:
    """Log once per reason: a capture failure never reaches the business path."""
    if reason not in _warned:
        _warned.add(reason)
        log.warning("Trajectory capture skipped: %s%s", reason,
                    f" error_type={type(exc).__name__}" if exc is not None else "")


@dataclass(frozen=True)
class _ServiceScope:
    ctx: Any
    job_id: str | None
    media: dict[str, dict]


_service_scope: ContextVar[_ServiceScope | None] = ContextVar("trajectory_service_scope", default=None)
_service_request: ContextVar[Any] = ContextVar("trajectory_service_request", default=None)

def _owned_key(url: str, user_id: str, prefixes: tuple[str, ...]) -> str | None:
    """The object key of a URL in the configured bucket under one of the owner's prefixes."""
    from urllib.parse import unquote, urlsplit
    try:
        from core.oss import get_oss
        host = get_oss().host
        parsed = urlsplit(url)
    except Exception:
        return None
    key = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != host:
        return None
    if not key.startswith(tuple(f"{prefix}{user_id}/" for prefix in prefixes)):
        return None
    return key


def _media_id(item: Mapping) -> str:
    return str(item.get("media_id") or item.get("asset_id"))


async def register_owned_media_inputs(ctx, urls: list[str]) -> dict[str, str]:
    """Map owned OSS inputs to the ready assets stored under their keys.

    Nothing is downloaded and no hidden asset row is created. A key without an
    asset (sampled media under ``analysis/``) is bound to its source asset by
    ``retain_derived_media_inputs``; an input that is not owned is simply not
    mapped, because recording never refuses a dispatch.
    """
    from trajectory import enabled
    if not enabled(ctx.user_id) or not urls or await context_for_tool(ctx) is None:
        return {}
    keys = {}
    for url in dict.fromkeys(urls):
        key = _owned_key(url, ctx.user_id, ("assets/", "analysis/"))
        if key is not None:
            keys[url] = key
    if not keys:
        return {}
    try:
        from sqlalchemy import select
        from db.base import get_db_session
        from db.models.file_asset import FileAsset
        async with get_db_session() as db:
            rows = (await db.scalars(select(FileAsset).where(
                FileAsset.oss_key.in_(set(keys.values())), FileAsset.user_id == ctx.user_id,
            ).order_by(FileAsset.created_at.desc()))).all()
    except Exception as exc:
        _not_recorded("owned media inputs could not be resolved", exc)
        return {}
    newest = {}
    for row in rows:
        newest.setdefault(row.oss_key, row)
    result = {}
    for url, key in keys.items():
        asset = newest.get(key)
        if asset is not None and not asset.is_deleted and not asset.deleted_at and asset.status == "ready":
            result[url] = asset.id
    return result


async def retain_derived_media_inputs(ctx, urls: list[str], source_asset_id: str) -> dict[str, dict]:
    """Sampled frames and audio as OSS-key references bound to their source asset.

    Deleting the source asset revokes them with it. No bytes are read.
    """
    import mimetypes
    from trajectory import enabled
    if not enabled(ctx.user_id) or not urls:
        return {}
    trace = await context_for_tool(ctx)
    if trace is None:
        return {}
    try:
        from db.base import get_db_session
        from db.models.file_asset import FileAsset
        async with get_db_session() as db:
            source = await db.get(FileAsset, source_asset_id)
    except Exception as exc:
        _not_recorded("sampled media source could not be resolved", exc)
        return {}
    if source is None or source.is_deleted or source.deleted_at or source.user_id != trace.user_id:
        return {}
    result = {}
    for url in dict.fromkeys(urls):
        key = _owned_key(url, trace.user_id, ("analysis/",))
        if key is None:
            continue
        result[url] = {"media_id": f"{source_asset_id}:{hashlib.sha256(key.encode()).hexdigest()[:16]}",
                       "asset_id": source_asset_id, "source_asset_id": source_asset_id, "oss_key": key,
                       "media_type": mimetypes.guess_type(key)[0] or "application/octet-stream",
                       "reference": "derived_asset", "availability": "available"}
    return result


async def _asset_media(trace, asset_urls: Mapping[str, str]) -> dict[str, dict]:
    """Owned assets as dispatch media references; each use is recorded as an artifact."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from trajectory.artifacts import capture_asset
    identifiers = list(dict.fromkeys(asset_urls.values()))
    try:
        async with get_db_session() as db:
            rows = {row.id: row for row in (await db.scalars(
                select(FileAsset).where(FileAsset.id.in_(identifiers)))).all()}
    except Exception as exc:
        _not_recorded("dispatch media assets could not be resolved", exc)
        rows = {}
    availability = {}
    for asset_id in identifiers:
        asset = rows.get(asset_id)
        reference = await capture_asset(trace, asset, role="input") if asset is not None else None
        availability[asset_id] = (reference or {}).get("availability", "not_recorded")
    media = {}
    for url, asset_id in asset_urls.items():
        asset = rows.get(asset_id)
        media[url] = {"media_id": asset_id, "asset_id": asset_id, "source_asset_id": asset_id,
                      "oss_key": getattr(asset, "oss_key", None), "media_type": getattr(asset, "mime", None),
                      "reference": "asset", "availability": availability[asset_id]}
    return media


async def _service_inputs(ctx, asset_urls, retained_media):
    import copy
    from trajectory import enabled
    local = copy.copy(ctx)
    media = dict(retained_media or {})
    if enabled(ctx.user_id):
        trace = await context_for_tool(local)
        local.trace_context = trace
        if trace is not None and asset_urls:
            media.update(await _asset_media(trace, asset_urls))
    local._trajectory_media_urls = media
    return local, media


@asynccontextmanager
async def service_scope(ctx, *, job=None, asset_urls: Mapping[str, str] | None = None,
                        retained_media: Mapping[str, dict] | None = None):
    """One task-local dispatch scope; it never mutates a caller's ToolContext."""
    local, media = await _service_inputs(ctx, asset_urls, retained_media)
    token = _service_scope.set(_ServiceScope(local, getattr(job, "id", None), media))
    prior_ids = (getattr(job, "request_data", None) or {}).get("_trajectory_request_ids") or []
    trace = getattr(local, "trace_context", None)
    request_token = _service_request.set(trace.derive(request_id=prior_ids[-1]) if trace and prior_ids else None)
    try:
        yield local
    finally:
        _service_request.reset(request_token)
        _service_scope.reset(token)


def _service_body(value, media):
    """A service body verbatim, each retained media URL replaced by its asset reference."""
    if hasattr(value, "model_dump"):
        value = provider_body(value)
    if isinstance(value, Mapping):
        return {str(key): _service_body(item, media) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_service_body(item, media) for item in value]
    if isinstance(value, str):
        if value in media:
            return "trajectory-media:" + _media_id(media[value])
        # IMS puts compiled business objects in JSON-valued query parameters.
        if value.lstrip().startswith(("{", "[")):
            import json
            try:
                return json.dumps(_service_body(json.loads(value), media), ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
    return value


async def _link_service_job(scope, capture):
    """Append a dispatched request to its job: a business link kept only while recording."""
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from trajectory import record
    try:
        async with get_db_session() as db:
            job = await db.get(VideoJob, scope.job_id, with_for_update=True)
            if (job is None or job.user_id != capture.context.user_id
                    or job.session_id != capture.context.source_session_id):
                _not_recorded("provider dispatch job does not match the execution scope")
                return
            metadata = dict(job.request_data or {})
            identifiers = list(metadata.get("_trajectory_request_ids") or [])
            identifiers.append(capture.context.request_id)
            job.request_data = {**metadata, "_trajectory_request_ids": identifiers}
            await record("job.progress", {"job_id": job.id, "status": job.status,
                "operation": "provider_dispatch", "provider_request_ids": identifiers,
                "request_id": capture.context.request_id}, context=capture.context, db=db)
    except Exception as exc:
        _not_recorded("provider dispatch was not linked to its job", exc)


@asynccontextmanager
async def capture_service_dispatch(*, purpose: str, provider: str, model: str, operation: str,
                                   body: Mapping, profile: str, capture_level="provider_wire",
                                   accepted=True):
    """Capture one observed submit, with later job polls kept out of request counts.

    The adapter's ``body`` is recorded verbatim except for top-level transport
    settings and credentials; ``profile`` names the adapter.
    """
    import copy
    from question.runtime import assert_current
    from trajectory import current
    # A revoked run submits no paid provider work.
    await assert_current("service")
    scope = _service_scope.get()
    if scope is None:
        from agent.hooks import current_tool_context
        from tool.tool import ToolContext
        ctx = current_tool_context()
        trace = current()
        if ctx is None and trace is not None:
            ctx = ToolContext(user_id=trace.user_id, session_id=trace.source_session_id,
                              workspace_id=trace.workspace_id or "", trace_context=trace)
        scope = _ServiceScope(copy.copy(ctx), None, {}) if ctx is not None else None
    visible = _service_body({key: item for key, item in body.items() if not _transport(key)},
                            scope.media if scope else {})
    manifest = {_media_id(item): item for item in scope.media.values()} if scope else {}
    dispatch_ctx = copy.copy(scope.ctx) if scope else None
    if dispatch_ctx is not None:
        # Media is settled by the job ledger, not the parent chat UsageMeter.
        dispatch_ctx._trajectory_billing_event_id = None
    capture = await RequestCapture.start(dispatch_ctx,
        purpose=purpose, model_id=f"{provider}/{model}", capture_level=capture_level,
        payload={"model": model, "input": {"operation": operation, "business_body": visible,
            "omitted_fields": sorted(str(key) for key in body if _transport(key)), "media_inputs": manifest,
            "media_url_representation": "retained_asset_reference" if manifest else "no_retained_media",
            "job_id": scope.job_id if scope else None}})
    try:
        if capture.context is not None:
            _service_request.set(capture.context)
            if scope is not None and scope.job_id:
                await _link_service_job(scope, capture)
        yield capture
    except BaseException as exc:
        await capture.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc)
        raise
    else:
        await capture.finish("completed", reason="accepted" if accepted else "stop")


async def capture_http_response(capture, response):
    """Observe an actual response before status/JSON validation can discard it."""
    status = getattr(response, "status_code", None)
    try:
        body = response.json()
    except ValueError:
        await capture.chunk({"response": {"status": status,
            "output": {"unparsed_body": getattr(response, "text", ""), "format": "non_json"}}})
        raise
    await capture.chunk({"response": {"output": body, "status": status}})
    return body


async def observe_service_response(body, *, operation: str):
    """An observed poll/download belongs to the original job, not a new model call."""
    from trajectory import record
    scope = _service_scope.get()
    if scope is None:
        return
    trace = _service_request.get() or getattr(scope.ctx, "trace_context", None)
    if trace is None:
        return
    await record("job.progress", {"job_id": scope.job_id, "operation": operation,
        "request_id": trace.request_id, "provider_response": provider_output_snapshot(body)}, context=trace)


def tool_schema(tool_info, name: str) -> dict:
    parameters = getattr(tool_info, "parameters", None)
    schema = getattr(tool_info, "raw_schema", None)
    schema_builder = getattr(parameters, "model_json_schema", None)
    if schema is None and callable(schema_builder):
        schema = schema_builder()
    return {"name": name, "description": getattr(tool_info, "description", ""), "parameters": schema}


def request_tool_schemas(definitions: Any) -> dict[str, dict]:
    """Index the finalized public wire definitions, including native namespaces."""
    result = {}
    for definition in definitions or []:
        if not isinstance(definition, Mapping):
            continue
        function = definition.get("function") or definition
        name = function.get("name")
        if name:
            result[name] = public_value(definition)
        if definition.get("type") == "namespace":
            for nested_name, nested in request_tool_schemas(definition.get("tools")).items():
                result[nested_name] = nested
                if name:
                    result[f"{name}.{nested_name}"] = nested
    return result


def requested_tool_schema(ctx, name: str, tool_info=None) -> tuple[dict, str]:
    schema = getattr(ctx, "_trajectory_request_schemas", {}).get(name)
    return (schema, "provider_request") if schema is not None else (tool_schema(tool_info, name), "executor_registry")


async def context_for_tool(ctx):
    """The execution identity bound to a tool context or task; never a database read.

    A context bound to another owner or session is not recorded under this tool.
    """
    from trajectory import current
    context = getattr(ctx, "trace_context", None) or current()
    if context is None:
        return None
    if context.user_id != ctx.user_id or context.source_session_id != ctx.session_id:
        _not_recorded("execution context does not match the tool owner or session")
        return None
    return context


async def capture_billing(context, meter, usage, credits, purpose: str) -> None:
    """Attach the existing ledger settlement; never price or charge again."""
    if context is None or context.request_id is None or meter is None:
        return
    from trajectory import record
    await record("request.usage", {"mode": "replace", "purpose": purpose,
        "usage": {**public_value(usage or {}), "credits": str(credits) if credits is not None else None},
        "billing_usage_event_id": getattr(meter, "event_id", None),
        "source": "existing_billing_ledger", "complete": usage is not None,
        "cost_availability": "available" if credits is not None else "not_recorded",
    }, context=context, event_id=f"request:{context.request_id}:billing")


class RequestCapture:
    def __init__(self, context, ctx, purpose: str, model_id: str, capture_level: str):
        self.context = context.derive(request_id=ascending("request"), call_id=None,
                                      parent_call_id=context.call_id or context.parent_call_id,
                                      message_id=getattr(ctx, "message_id", None) or context.message_id,
                                      part_id=None) if context is not None else None
        self.ctx = ctx
        self.purpose = purpose
        self.model_id = model_id
        self.capture_level = capture_level
        self.started = time.monotonic()
        self.first_output: float | None = None
        self.first_text: float | None = None
        self.chunk_index = 0
        self.finished = False
        self.usage: dict | None = None
        self.usage_index = 0
        self.response_ended: float | None = None

    @classmethod
    async def start(cls, ctx, *, purpose: str, model_id: str, payload: Mapping,
                    capture_level: str):
        """Enqueue request.prepared and request.started; the provider call never waits on them."""
        context = await context_for_tool(ctx) if ctx is not None else None
        capture = cls(context, ctx, purpose, model_id, capture_level)
        if context is not None:
            from trajectory import record
            request_id = capture.context.request_id
            try:
                snapshot = request_snapshot(payload)
                inline_media = getattr(ctx, "_trajectory_inline_media", None)
                if inline_media:
                    snapshot = _compact_inline_media(snapshot, inline_media)
                media = getattr(ctx, "_trajectory_media_urls", None)
                if media:
                    snapshot = _service_body(snapshot, media)
                    snapshot["media_inputs"] = {_media_id(item): item for item in media.values()}
                prepared = {
                    "purpose": purpose, "model": model_id,
                    "provider": model_id.split("/", 1)[0] if "/" in model_id else "unknown",
                    "capture_level": capture_level, "input": snapshot,
                    "attempt": getattr(ctx, "_trajectory_attempt", 1),
                    "previous_request_id": context.request_id if context.call_id is None else None,
                    "parent_request_id": context.request_id if context.call_id is not None else None,
                    "sdk_internal_attempts": "not_observed",
                    "billing_usage_event_id": getattr(ctx, "_trajectory_billing_event_id", None),
                }
                sources = getattr(ctx, "_trajectory_media_sources", None)
                if sources:
                    # Digests of inline media the loop resolved from owned assets;
                    # the worker binds those bytes to the assets instead of copying them.
                    prepared["media_sources"] = dict(sources)
                await record("request.prepared", prepared, context=capture.context,
                             event_id=f"request:{request_id}:prepared")
                if context.call_id is None:
                    ctx._trajectory_request_schemas = request_tool_schemas(snapshot.get("tools"))
            except Exception as exc:
                _not_recorded("request input could not be captured", exc)
            await record("request.started", {
                "purpose": purpose, "model": model_id,
                "capture_level": capture_level, "timing_source": "producer_monotonic",
            }, context=capture.context, event_id=f"request:{request_id}:started")
            # One ToolContext belongs to one request chain. Parallel executors
            # clone it, so this handoff does not modify a sibling's identity.
            if context.call_id is None:
                ctx.trace_context = capture.context
            ctx._trajectory_active_request = capture.context
        capture.started = time.monotonic()
        return capture

    def chunk_data(self, raw: Any, *, blocks: list[dict] | None = None):
        """One recorded delta: the adapter's blocks and the provider's chunk verbatim.

        None when the chunk cannot be captured; the request goes on.
        """
        self.chunk_index += 1
        observed = time.monotonic()
        blocks = blocks or []
        if any(isinstance(block, dict) and block.get("delta") for block in blocks):
            if self.first_output is None:
                self.first_output = observed
            if self.first_text is None and any(
                isinstance(block, dict) and block.get("type") == "text" and block.get("delta") for block in blocks
            ):
                self.first_text = observed
        try:
            return {"chunk_index": self.chunk_index, "mode": "delta", "blocks": blocks, "purpose": self.purpose,
                    "raw": provider_output_snapshot(raw), "elapsed_ms": (observed - self.started) * 1000}
        except Exception as exc:
            _not_recorded("response chunk could not be captured", exc)
            return None

    async def chunk(self, raw: Any, *, blocks: list[dict] | None = None):
        if self.context is None:
            return
        data = self.chunk_data(raw, blocks=blocks)
        if data is not None:
            from trajectory import record
            await record("request.delta", data, context=self.context,
                         event_id=f"request:{self.context.request_id}:chunk:{data['chunk_index']}")

    async def stream_chunks(self, stream, block_builder):
        """Yield each provider chunk as it arrives, enqueueing its delta on the way.

        Nothing here waits for the recorder: a slow or failed emitter never
        delays token delivery.
        """
        if self.context is None:
            async for chunk in stream:
                yield chunk
            return
        from trajectory import record_stream
        from trajectory.types import now
        ended = False
        try:
            async for chunk in stream:
                observed_at = now()
                try:
                    blocks = block_builder(chunk)
                except Exception as exc:
                    _not_recorded("response chunk blocks could not be built", exc)
                    blocks = []
                data = self.chunk_data(chunk, blocks=blocks)
                if data is not None:
                    record_stream(self.context, {
                        "type": "request.delta", "data": data, "occurred_at": observed_at,
                        "event_id": f"request:{self.context.request_id}:chunk:{data['chunk_index']}",
                    })
                yield chunk
            ended = True
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except BaseException:
            ended = True
            raise
        finally:
            if ended:
                self.response_ended = time.monotonic()
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                result = closer()
                if inspect.isawaitable(result):
                    await result

    async def capture_usage(self, usage: dict):
        if not usage or usage == self.usage:
            return
        self.usage = dict(usage)
        self.usage_index += 1
        if self.context is not None:
            from trajectory import record
            await record("request.usage", {
                "mode": "replace", "usage": public_value(usage), "purpose": self.purpose,
                "source": "provider_normalized", "complete": False,
            }, context=self.context, event_id=f"request:{self.context.request_id}:usage:{self.usage_index}")

    async def finish(self, status: str, *, reason: str | None = None, error: BaseException | None = None):
        if self.finished:
            return
        self.finished = True
        ended = self.response_ended or time.monotonic()
        if self.context is None:
            return
        from trajectory import record
        await record("request.finished", {
            "status": status, "finish_reason": reason, "purpose": self.purpose,
            "error": {"type": type(error).__name__, "message": str(error)} if error else None,
            "usage": public_value(self.usage), "chunk_count": self.chunk_index,
            "duration_ms": (ended - self.started) * 1000,
            "ttft_ms": (self.first_output - self.started) * 1000 if self.first_output is not None else None,
            "first_text_ms": (self.first_text - self.started) * 1000 if self.first_text is not None else None,
            "generation_ms": (ended - self.first_output) * 1000 if self.first_output is not None else None,
            "timing_source": "producer_monotonic",
        }, context=self.context, event_id=f"request:{self.context.request_id}:finished")

    async def route_changed(self, reason: str):
        if self.context is not None:
            from trajectory import record
            await record("request.route_changed", {
                "reason": reason, "from": "native_tool_search", "to": "portable",
                "model": self.model_id,
            }, context=self.context)


def litellm_chunk_blocks(chunk) -> list[dict]:
    blocks = []
    for choice in getattr(chunk, "choices", ()) or ():
        delta = getattr(choice, "delta", None) or getattr(choice, "message", None)
        if delta is None:
            continue
        for attribute, kind in (("content", "text"), ("reasoning_content", "reasoning")):
            value = getattr(delta, attribute, None)
            if value:
                blocks.append({"type": kind, "block_id": f"{getattr(choice, 'index', 0)}:{kind}", "delta": public_value(value)})
        for call in getattr(delta, "tool_calls", ()) or ():
            function = getattr(call, "function", None)
            blocks.append({"type": "tool_arguments", "block_id": f"tool:{getattr(call, 'index', 0)}",
                           "provider_call_id": getattr(call, "id", None),
                           "tool": getattr(function, "name", None),
                           "delta": getattr(function, "arguments", None)})
    return blocks


def responses_chunk_blocks(data: dict) -> list[dict]:
    kind = data.get("type", "")
    block_type = {"response.output_text.delta": "text",
                  "response.reasoning_summary_text.delta": "reasoning",
                  "response.function_call_arguments.delta": "tool_arguments"}.get(kind)
    if block_type:
        return [{"type": block_type, "block_id": str(data.get("item_id") or data.get("output_index", 0)),
                 "delta": data.get("delta", "")}]
    if kind == "response.completed":
        result = []
        for index, item in enumerate((data.get("response") or {}).get("output") or []):
            item_kind = item.get("type")
            block_id = str(item.get("id") or index)
            if item_kind == "message":
                text = "".join(str(part.get("text", "")) for part in item.get("content", [])
                               if part.get("type") == "output_text")
                if text:
                    result.append({"type": "text", "block_id": block_id, "delta": text, "mode": "replace"})
            elif item_kind == "reasoning":
                text = "".join(str(part.get("text", "")) for part in item.get("summary", []))
                if text:
                    result.append({"type": "reasoning", "block_id": block_id, "delta": text, "mode": "replace"})
            elif item_kind == "function_call":
                result.append({"type": "tool_arguments", "block_id": block_id,
                    "delta": item.get("arguments", ""), "mode": "replace",
                    "provider_call_id": item.get("call_id"), "tool": item.get("name")})
        return result
    return []
