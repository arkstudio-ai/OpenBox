"""Explicit trajectory capture at model and executor boundaries.

Only public request fields are copied. Provider credentials and private replay
state are never passed to the recorder, even when its storage policy changes.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from core.identifier import ascending

REQUEST_FIELDS = frozenset({
    "model", "messages", "input", "instructions", "tools", "tool_choice",
    "parallel_tool_calls", "stream", "stream_options", "temperature", "top_p",
    "max_tokens", "max_completion_tokens", "max_output_tokens", "stop", "seed",
    "reasoning", "reasoning_effort", "thinking", "response_format", "text",
    "frequency_penalty", "presence_penalty", "logprobs", "top_logprobs",
    "prompt", "n", "size", "quality", "output_format", "output_compression", "background",
})
PRIVATE_FIELDS = frozenset({
    "api_key", "authorization", "headers", "extra_headers", "cookie", "cookies",
    "access_token", "refresh_token", "security_token", "password", "secret",
    "signature", "encrypted_content", "provider_metadata", "provider_specific_fields",
    "provider_replay", "provider_binding_digest", "provider_account_id",
    "canonical_tool_id", "provider_dialect", "credential", "credentials",
})


def public_value(value: Any, *, schema: bool = False) -> Any:
    """Serialize known public fields, excluding nested SDK/private state."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if schema:
        # A property called password describes a public input contract. Its
        # definition/constraints remain visible; credential examples/defaults
        # are redacted by the shared schema-aware policy.
        from trajectory.redaction import sanitize
        return sanitize(value, _schema=True)
    if isinstance(value, Mapping):
        from trajectory.redaction import _schema_node
        return {
            str(key): public_value(item, schema=(str(key).replace("_", "").lower() in {
                "schema", "parameters", "inputschema", "outputschema", "jsonschema",
            } and _schema_node(item))) for key, item in value.items()
            if not str(key).startswith("_") and str(key).lower() not in PRIVATE_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Unknown SDK objects can carry headers and credentials in repr/__dict__.
    return {"availability": "not_recorded", "reason": "unsupported_public_value"}


def request_snapshot(kwargs: Mapping[str, Any]) -> dict:
    snapshot = {key: public_value(value) for key, value in kwargs.items() if key in REQUEST_FIELDS}
    extra = kwargs.get("extra_body")
    if isinstance(extra, Mapping):
        snapshot["extra_body"] = {
            key: public_value(value) for key, value in extra.items() if key in REQUEST_FIELDS
        }
    omitted = sorted(set(kwargs) - REQUEST_FIELDS - {"extra_body"})
    if omitted:
        snapshot["omitted_fields"] = omitted
    return snapshot


def provider_output_snapshot(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        return public_value(value)
    fields = {"id", "object", "created", "model", "choices", "usage", "type", "delta",
              "item", "item_id", "output_index", "content_index", "summary_index",
              "sequence_number", "response", "error", "code", "message", "param"}
    output = {key: public_value(item) for key, item in value.items() if key in fields}
    response = value.get("response")
    if isinstance(response, Mapping):
        response_fields = {"id", "object", "model", "created_at", "completed_at", "status", "output",
                           "usage", "error", "incomplete_details"}
        output["response"] = {key: public_value(item) for key, item in response.items() if key in response_fields}
    return output


@dataclass(frozen=True)
class _ServiceScope:
    ctx: Any
    job_id: str | None
    media: dict[str, dict]


_service_scope: ContextVar[_ServiceScope | None] = ContextVar("trajectory_service_scope", default=None)
_service_request: ContextVar[Any] = ContextVar("trajectory_service_request", default=None)


def _recording_operation(function):
    from functools import wraps
    @wraps(function)
    async def wrapped(*args, **kwargs):
        from trajectory.types import RecordingError, TrajectoryError
        try:
            return await function(*args, **kwargs)
        except TrajectoryError:
            raise
        except Exception as exc:
            raise RecordingError("Provider input could not be retained") from exc
    return wrapped

# These are business bodies assembled by the named adapters. Credentials,
# transport/SDK objects and unrestricted provider kwargs never enter them.
SERVICE_FIELDS = {
    "video_generation": frozenset({"model", "prompt", "content", "resolution", "ratio", "duration",
        "generate_audio", "watermark", "return_last_frame", "seed", "metadata", "images", "image_url",
        "extra_images", "extra_videos", "size"}),
    "audio_transcription": frozenset({"model", "input", "parameters", "audio_url", "response_format"}),
    "media_composition": frozenset({"Timeline", "OutputMediaTarget", "OutputMediaConfig", "ClientToken",
        "Source", "UserData"}),
}


@_recording_operation
async def register_owned_media_inputs(ctx, urls: list[str]) -> dict[str, str]:
    """Version already-owned OSS inputs, including internal sampled media.

    Only the configured bucket and server-owned user prefixes are read. Bytes
    are fetched outside a DB transaction; intermediates become hidden assets
    so retention/deletion uses the same ownership rules as other attachments.
    """
    from trajectory import enabled
    if not enabled(ctx.user_id) or not urls:
        return {}
    from urllib.parse import unquote, urlsplit
    from types import SimpleNamespace
    from sqlalchemy import select
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from trajectory.artifacts import read_asset_bytes, capture_asset_in_tx
    from trajectory.types import OwnershipError
    import mimetypes

    trace = await context_for_tool(ctx)
    if trace is None:
        return {}
    oss = get_oss()
    result = {}
    for url in dict.fromkeys(urls):
        parsed = urlsplit(url)
        key = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in {"http", "https"} or parsed.hostname != oss.host
                or not key.startswith((f"assets/{ctx.user_id}/", f"analysis/{ctx.user_id}/"))):
            raise OwnershipError("Media input is outside the owned object namespace")
        async with get_db_session() as db:
            asset = await db.scalar(select(FileAsset).where(FileAsset.oss_key == key,
                FileAsset.user_id == ctx.user_id).order_by(FileAsset.created_at.desc()).limit(1))
        if asset is not None and (asset.is_deleted or asset.deleted_at or asset.status != "ready"):
            raise OwnershipError("Media input is deleted or unavailable")
        if asset is None:
            content = await read_asset_bytes(SimpleNamespace(oss_key=key))
            async with get_db_session() as db:
                asset = FileAsset(id=ascending("asset"), user_id=ctx.user_id,
                    workspace_id=trace.workspace_id or ctx.workspace_id,
                    session_id=ctx.session_id, project_id=getattr(ctx, "project_id", None),
                    name=key.rsplit("/", 1)[-1], oss_key=key,
                    mime=mimetypes.guess_type(key)[0] or "application/octet-stream",
                    size=len(content), status="ready", source="agent", transient=True,
                    is_deleted=False, created_at=datetime.now(timezone.utc))
                db.add(asset)
                await capture_asset_in_tx(db, trace, asset, content=content, role="input")
        result[url] = asset.id
    return result


@_recording_operation
async def retain_derived_media_inputs(ctx, urls: list[str], source_asset_id: str) -> dict[str, dict]:
    """Sampled frames/audio remain revocable with the original owned video."""
    from trajectory import enabled, ensure_trajectory_in_tx
    if not enabled(ctx.user_id) or not urls:
        return {}
    import base64
    import hashlib
    import mimetypes
    from urllib.parse import unquote, urlsplit
    from types import SimpleNamespace
    from core.oss import get_oss
    from db.base import get_db_session
    from trajectory.artifacts import read_asset_bytes, retain_request_media_in_tx
    from trajectory.types import OwnershipError
    from db.models.file_asset import FileAsset
    trace = await context_for_tool(ctx)
    if trace is None:
        return {}
    async with get_db_session() as db:
        source = await db.get(FileAsset, source_asset_id)
        if source is None or source.is_deleted or source.deleted_at or source.user_id != trace.user_id:
            raise OwnershipError("Sampled media source is missing or not owned")
    oss = get_oss()
    encoded, sources = {}, {}
    for url in dict.fromkeys(urls):
        parsed = urlsplit(url)
        key = unquote(parsed.path.lstrip("/"))
        if parsed.hostname != oss.host or not key.startswith(f"analysis/{trace.user_id}/"):
            raise OwnershipError("Sampled media is outside the owned staging namespace")
        content = await read_asset_bytes(SimpleNamespace(oss_key=key))
        sources[hashlib.sha256(content).hexdigest()] = source_asset_id
        mime = mimetypes.guess_type(key)[0] or "application/octet-stream"
        encoded[url] = f"data:{mime};base64," + base64.b64encode(content).decode()
    async with get_db_session() as db:
        trajectory = await ensure_trajectory_in_tx(db, trace)
        retained = await retain_request_media_in_tx(db, trace, list(encoded.values()),
            first_seq=trajectory.next_seq, source_asset_ids=sources)
    return {url: {"asset_id": source_asset_id, "payload": item["$media"]}
            for url, item in zip(encoded, retained)}


@_recording_operation
async def _service_inputs(ctx, asset_urls, retained_media):
    import copy
    from trajectory import enabled
    local = copy.copy(ctx)
    media = dict(retained_media or {})
    if enabled(ctx.user_id):
        trace = await context_for_tool(local)
        local.trace_context = trace
        if trace is not None and asset_urls:
            from db.base import get_db_session
            from trajectory.artifacts import prepare_asset_ids, capture_asset_ids_in_tx
            ids = list(dict.fromkeys(asset_urls.values()))
            prepared = await prepare_asset_ids(trace.user_id, trace.workspace_id, ids,
                                               root_session_id=trace.session_id)
            async with get_db_session() as db:
                retained = await capture_asset_ids_in_tx(db, trace, ids, prepared=prepared, role="input")
            if any(value.get("availability") != "available" for value in retained.values()):
                from trajectory.types import OwnershipError
                raise OwnershipError("Media input became unavailable before dispatch")
            media.update({url: {"asset_id": asset_id, "payload": retained[asset_id]}
                          for url, asset_id in asset_urls.items()})
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
    if isinstance(value, Mapping):
        return {str(key): _service_body(item, media) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_service_body(item, media) for item in value]
    if isinstance(value, str):
        if value in media:
            return "trajectory-media:" + str(media[value]["payload"].get("payload_id") or media[value]["asset_id"])
        # IMS puts compiled business objects in JSON-valued query parameters.
        if value.lstrip().startswith(("{", "[")):
            import json
            try:
                return json.dumps(_service_body(json.loads(value), media), ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
    return public_value(value)


@_recording_operation
async def _link_service_job(scope, capture):
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from trajectory import record
    from trajectory.types import OwnershipError
    async with get_db_session() as db:
        job = await db.get(VideoJob, scope.job_id, with_for_update=True)
        if job is None or job.user_id != capture.context.user_id or job.session_id != capture.context.source_session_id:
            raise OwnershipError("Provider dispatch job does not match the execution scope")
        metadata = dict(job.request_data or {})
        identifiers = list(metadata.get("_trajectory_request_ids") or [])
        identifiers.append(capture.context.request_id)
        job.request_data = {**metadata, "_trajectory_request_ids": identifiers}
        await record("job.progress", {"job_id": job.id, "status": job.status,
            "operation": "provider_dispatch", "provider_request_ids": identifiers,
            "request_id": capture.context.request_id}, context=capture.context, db=db)


@asynccontextmanager
async def capture_service_dispatch(*, purpose: str, provider: str, model: str, operation: str,
                                   body: Mapping, profile: str, capture_level="provider_wire",
                                   accepted=True):
    """Capture one observed submit, with later job polls kept out of request counts."""
    import copy
    from trajectory import current
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
    fields = SERVICE_FIELDS[profile]
    visible = _service_body({key: item for key, item in body.items() if key in fields}, scope.media if scope else {})
    manifest = {str(item["payload"].get("payload_id") or item["asset_id"]): item
                for item in scope.media.values()} if scope else {}
    dispatch_ctx = copy.copy(scope.ctx) if scope else None
    if dispatch_ctx is not None:
        # Media is settled by the job ledger, not the parent chat UsageMeter.
        dispatch_ctx._trajectory_billing_event_id = None
    capture = await RequestCapture.start(dispatch_ctx,
        purpose=purpose, model_id=f"{provider}/{model}", capture_level=capture_level,
        payload={"model": model, "input": {"operation": operation, "business_body": visible,
            "omitted_fields": sorted(set(body) - fields), "media_inputs": manifest,
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
        "request_id": trace.request_id, "provider_response": public_value(body)}, context=trace)


def tool_schema(tool_info, name: str) -> dict:
    parameters = getattr(tool_info, "parameters", None)
    schema = getattr(tool_info, "raw_schema", None)
    if schema is None and parameters is not None:
        schema = parameters.model_json_schema()
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
    from trajectory import current, enabled
    context = getattr(ctx, "trace_context", None) or current()
    if context is not None:
        if context.user_id != ctx.user_id or context.source_session_id != ctx.session_id:
            raise ValueError("trajectory execution context does not match tool owner/session")
        return context
    if not ctx.user_id or not ctx.session_id or not enabled(ctx.user_id):
        return None
    from db.base import get_db_session
    from trajectory import context_for_session
    async with get_db_session() as db:
        return await context_for_session(db, ctx.user_id, ctx.session_id)


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
        from trajectory.stream_redaction import CaptureStreamRedactor
        self._stream_redactor = CaptureStreamRedactor() if self.context is not None else None

    @classmethod
    async def start(cls, ctx, *, purpose: str, model_id: str, payload: Mapping,
                    capture_level: str):
        context = await context_for_tool(ctx) if ctx is not None else None
        capture = cls(context, ctx, purpose, model_id, capture_level)
        if context is not None:
            from trajectory import record, enabled, ensure_trajectory_in_tx
            snapshot = request_snapshot(payload)
            media = getattr(ctx, "_trajectory_media_urls", None)
            if media:
                snapshot = _service_body(snapshot, media)
                snapshot["media_inputs"] = {str(item["payload"].get("payload_id") or item["asset_id"]): item
                                             for item in media.values()}
            async def persist(db=None):
                await record("request.prepared", {
                    "purpose": purpose, "model": model_id,
                    "provider": model_id.split("/", 1)[0] if "/" in model_id else "unknown",
                    "capture_level": capture_level, "input": snapshot,
                    "attempt": getattr(ctx, "_trajectory_attempt", 1),
                    "previous_request_id": context.request_id if context.call_id is None else None,
                    "parent_request_id": context.request_id if context.call_id is not None else None,
                    "sdk_internal_attempts": "not_observed",
                    "billing_usage_event_id": getattr(ctx, "_trajectory_billing_event_id", None),
                }, context=capture.context, db=db, event_id=f"request:{capture.context.request_id}:prepared")
                await record("request.started", {
                    "purpose": purpose, "model": model_id,
                    "capture_level": capture_level, "timing_source": "producer_monotonic",
                }, context=capture.context, db=db, event_id=f"request:{capture.context.request_id}:started")
            if enabled(context.user_id):
                from db.base import get_db_session
                from trajectory.artifacts import retain_request_media_in_tx
                from trajectory.types import RecordingError, TrajectoryError
                try:
                    async with get_db_session() as db:
                        trajectory = await ensure_trajectory_in_tx(db, capture.context)
                        snapshot = await retain_request_media_in_tx(db, capture.context, snapshot,
                            first_seq=trajectory.next_seq,
                            source_asset_ids=getattr(ctx, "_trajectory_media_sources", None))
                        await persist(db)
                except TrajectoryError:
                    raise
                except Exception as exc:
                    raise RecordingError("Request input could not be recorded") from exc
            else:
                await persist()
            # One ToolContext belongs to one request chain. Parallel executors
            # clone it, so this handoff does not modify a sibling's identity.
            if context.call_id is None:
                ctx.trace_context = capture.context
                ctx._trajectory_request_schemas = request_tool_schemas(snapshot.get("tools"))
            ctx._trajectory_active_request = capture.context
        capture.started = time.monotonic()
        return capture

    def chunk_data(self, raw: Any, *, blocks: list[dict] | None = None):
        self.chunk_index += 1
        observed = time.monotonic()
        blocks = blocks or []
        if any(block.get("delta") for block in blocks):
            if self.first_output is None:
                self.first_output = observed
            if self.first_text is None and any(
                block.get("type") == "text" and block.get("delta") for block in blocks
            ):
                self.first_text = observed
        data = {
            "chunk_index": self.chunk_index, "mode": "delta", "blocks": blocks, "purpose": self.purpose,
            "raw": provider_output_snapshot(raw), "elapsed_ms": (observed - self.started) * 1000,
        }
        if self._stream_redactor is None:
            return data
        from trajectory.types import RecordingError, TrajectoryError
        try:
            return self._stream_redactor.redact(data)
        except TrajectoryError:
            raise
        except Exception as exc:
            raise RecordingError("Streaming response could not be redacted") from exc

    async def chunk(self, raw: Any, *, blocks: list[dict] | None = None):
        data = self.chunk_data(raw, blocks=blocks)
        if self.context is not None:
            from trajectory import record
            await record("request.delta", data, context=self.context,
                         event_id=f"request:{self.context.request_id}:chunk:{data['chunk_index']}")

    async def stream_chunks(self, stream, block_builder):
        """Read ahead within a byte bound; release only committed chunks.

        Recorder batching must not make the network reader wait one batch
        timer per token. The producer queues observed chunks with individual
        receipts; the consumer awaits those receipts in original order.
        """
        if self.context is None:
            async for chunk in stream:
                yield chunk
            return
        from trajectory import record_stream
        from trajectory.config import integer
        from trajectory.types import canonical, now
        queue = asyncio.Queue(maxsize=32)
        condition = asyncio.Condition()
        maximum = integer("TRAJECTORY_PENDING_BYTES", 4 * 1024 * 1024)
        pending_bytes = 0
        sentinel = object()

        async def read():
            nonlocal pending_bytes
            try:
                async for chunk in stream:
                    observed_at = now()
                    data = self.chunk_data(chunk, blocks=block_builder(chunk))
                    size = len(canonical(data))
                    async with condition:
                        while pending_bytes and pending_bytes + size > maximum:
                            await condition.wait()
                        pending_bytes += size
                    receipt = record_stream(self.context, {
                        "type": "request.delta", "data": data, "occurred_at": observed_at,
                        "event_id": f"request:{self.context.request_id}:chunk:{data['chunk_index']}",
                    })
                    await queue.put((chunk, receipt, size))
                self.response_ended = time.monotonic()
                await queue.put((sentinel, None, 0))
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self.response_ended = time.monotonic()
                await queue.put((sentinel, error, 0))

        reader = asyncio.create_task(read())
        try:
            while True:
                chunk, receipt, size = await queue.get()
                if chunk is sentinel:
                    if receipt is not None:
                        raise receipt
                    break
                await receipt
                async with condition:
                    pending_bytes -= size
                    condition.notify_all()
                yield chunk
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
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
        ended = self.response_ended or time.monotonic()
        if self.context is not None:
            from trajectory import record
            from trajectory.types import RecordingError, TrajectoryError
            try:
                finalized = self._stream_redactor.finalize()
            except TrajectoryError:
                raise
            except Exception as exc:
                raise RecordingError("Streaming response redaction could not be finalized") from exc
            if finalized is not None:
                # A buffered redaction suffix is recorder control data, not an
                # additional provider chunk or a new generation observation.
                await record("request.delta", {**finalized, "purpose": self.purpose,
                    "source": "recorder_redaction", "observed_chunk_count": self.chunk_index,
                    "elapsed_ms": (ended - self.started) * 1000}, context=self.context,
                    event_id=f"request:{self.context.request_id}:redaction:finalize")
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
        self.finished = True

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
