"""Durable human evidence from final native ASR, without an assistant run.

The source row is the original transcript, not a generated summary. Its own
receipt contract uses a call-local sequence and leaves Agent run/result fields
empty: no Message, Inbox, Agent event or successful Agent terminal is invented.
Only this authenticated provider boundary creates the native source identity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import func, select

from db.base import get_db_session
from db.models.memory_pipeline import MemoryTurnCompletion
from db.models.memory_v2 import MemorySource, MemoryTombstone
from db.models.voice import VoiceCall
from memory import jobs

SOURCE_KIND = "native_voice_utterance"
CAPTURE_VERSION = "native-asr-v1"


class OccurrenceClock:
    """Server-observed item times; delayed final ASR must not reorder evidence.

    No client/provider clock is trusted. If start/item/delta was omitted, the
    final event's receipt time is the earliest evidence we actually possess.
    """
    def __init__(self, now=None):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.items = {}

    def observe(self, event, provider_session: int):
        if event.item_id and (event.kind in {"user_started", "user_transcribing", "user_transcript"}
                              or event.kind == "item_created" and event.role == "user"):
            return self.items.setdefault((provider_session, event.item_id), self.now())
        return None


def _body_hash(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def forwarded_hash(text: str) -> str:
    # The existing human Inbox adapter normalizes whitespace when forwarding.
    return _body_hash(" ".join(text.split()))


def source_id(*, user_id, workspace_id, session_id, call_id, provider_session, item_id):
    return "msv_" + jobs._hash([CAPTURE_VERSION, user_id, workspace_id, session_id,
                               call_id, provider_session, item_id])[:48]


def _branch(call_id: str) -> str:
    return "voice:" + _body_hash(call_id)[:40]


def _receipt_id(identity: str) -> str:
    return jobs._hash([CAPTURE_VERSION, identity])


def is_native_receipt(receipt) -> bool:
    return bool(receipt and receipt.run_id == "" and receipt.result_message_id == ""
                and receipt.run_generation == 0 and receipt.branch_id.startswith("voice:"))


def cancelled_reason(receipt) -> str | None:
    """Recovery must retain the saving decision made when the words arrived."""
    if is_native_receipt(receipt) and len(receipt.source_boundaries or []) == 1:
        return receipt.source_boundaries[0].get("capture_cancelled") or None
    return None


def unavailable_status() -> dict:
    return {"status": "unavailable", "capture_confirmed": False, "memory_saved": False,
            "this_call_applied": False, "execution_confirmed": False, "handoff_created": False,
            "note": "本轮记忆状态暂不可用，不能确认长期记忆已保存；不要另行转交或承诺已记住。"}


async def capture_status(*, user_id, workspace_id, session_id, call_id, provider_session,
                         item_id, identity, text) -> dict:
    """Read the current utterance's existing receipt; never enqueue or run a model.

    The link supplies identity from its code-owned item map. Recompute it and
    recheck live source/call authority here, rather than trusting that map as
    permanent authority or treating SUCCEEDED with no writes as a saved fact.
    """
    from db.models.memory import UserMemory
    from db.models.memory_pipeline import MemoryExtractionJob
    from db.models.memory_v2 import MemorySourceLink
    from memory.policy import active_memory_predicates, resolve_access_scope
    from memory.service import memory_sources_available, source_body_is_available
    from memory.settings import saving_paused_locked

    if (not item_id or type(provider_session) is not int or provider_session < 0
            or identity != source_id(user_id=user_id, workspace_id=workspace_id, session_id=session_id,
                call_id=call_id, provider_session=provider_session, item_id=item_id)):
        return unavailable_status()
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id)
        source = await db.get(MemorySource, identity)
        call = await db.get(VoiceCall, call_id)
        if (source is None or call is None or call.status != "active"
                or (call.user_id, call.workspace_id, call.main_session_id) != (user_id, workspace_id, session_id)
                or not source.body or forwarded_hash(source.body) != forwarded_hash(text)
                or not await source_body_is_available(db, access, source)):
            return unavailable_status()
        receipt = await db.get(MemoryTurnCompletion, _receipt_id(identity))
        if not is_native_receipt(receipt):
            return unavailable_status()
        job = await db.scalar(select(MemoryExtractionJob).where(
            MemoryExtractionJob.completion_id == receipt.id,
            MemoryExtractionJob.pipeline_version == receipt.pipeline_version))
        result = {**unavailable_status(), "capture_confirmed": True,
                  "source_id": identity, "source_kind": SOURCE_KIND}
        if (job is None or (job.user_id, job.workspace_id, job.session_id, job.logical_turn_id,
                           job.branch_id, job.input_hash, job.project_id)
                != (user_id, workspace_id, session_id, identity, receipt.branch_id, receipt.input_hash, None)):
            return result
        result["extraction_state"] = job.state
        reason = cancelled_reason(receipt)
        paused = await saving_paused_locked(db, user_id, session_id)
        if reason or job.state != "SUCCEEDED" and (paused or not jobs.extraction_write_enabled(user_id)):
            is_paused = reason == "memory_paused" or not reason and paused
            return {**result, "status": "paused" if is_paused else "disabled",
                    "note": "原话已接收，但长期记忆保存已暂停，本轮没有确认保存；恢复后不会补采这条暂停期间的原话。"
                            if is_paused and reason else "原话已接收，但当前暂停或未启用长期记忆保存，不能说已保存。"}
        if job.state == "SUCCEEDED":
            saved = []
            if job.result_memory_ids:
                rows = (await db.scalars(select(UserMemory).join(MemorySourceLink,
                    (MemorySourceLink.memory_id == UserMemory.id) & (MemorySourceLink.revision == UserMemory.revision))
                    .where(*access.predicates(UserMemory), *active_memory_predicates(),
                           UserMemory.id.in_(job.result_memory_ids), MemorySourceLink.source_id == identity,
                           MemorySourceLink.source_revision == source.source_revision,
                           MemorySourceLink.relation == "SUPPORTS").distinct().limit(20))).all()
                for row in rows:
                    if await memory_sources_available(db, access, row):
                        saved.append({"id": row.id, "revision": row.revision,
                                      "summary": (row.value or {}).get("summary", "")})
            fields = await _saved_settings(db, user_id, job, source)
            committed = bool(saved or fields)
            return {**result, "status": "saved" if committed else "not_saved", "memory_saved": committed,
                    "saved_memories": saved, "saved_settings": fields,
                    "note": "已核验回执中列出的记忆或设置当前可用；只可确认这些具体内容，不代表整句要求都已保存或永久保留。"
                            if committed else "原话已接收，后台整理已结束，但没有核验到当前可用的长期记忆或设置写入，不能说已保存。"}
        statuses = {"PENDING": ("pending", "原话已接收，等待后台整理和验证；尚未确认长期记忆已保存。"),
                    "RUNNING": ("processing", "原话已接收，后台正在整理和验证；尚未确认长期记忆已保存。"),
                    "RETRY": ("retrying", "原话已接收，后台处理遇到问题并等待重试；尚未确认长期记忆已保存。"),
                    "DEAD": ("failed", "原话已接收，但本轮长期记忆整理失败，不能说已保存。"),
                    "CANCELLED": ("cancelled", "原话已接收，本轮长期记忆整理已取消，不能说已保存。")}
        status, note = statuses.get(job.state, ("unavailable", result["note"]))
        return {**result, "status": status, "note": note}


async def _saved_settings(db, user_id, job, source) -> dict:
    """A historical profile receipt alone cannot assert that its values still apply."""
    from assistant import profile
    from db.models.memory_protocol import MemoryWriteReceipt
    from db.models.preference import UserPreference
    from memory.preferences import REVISION_KEY
    receipt = await db.get(MemoryWriteReceipt, (user_id, f"extraction-settings:{job.id}"))
    if (receipt is None or receipt.kind != "profile" or receipt.state != "saved"
            or not any(ref.get("source_id") == source.id and ref.get("source_kind") == SOURCE_KIND
                       and ref.get("content_hash") == source.content_hash
                       and ref.get("source_revision") == source.source_revision
                       and ref.get("session_id") == source.session_id for ref in receipt.source_refs or [])):
        return {}
    extra = await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id)) or {}
    if int(extra.get(REVISION_KEY) or 0) != receipt.result.get("revision"):
        return {}
    current = profile.from_extra(extra)
    return {key: value for key, value in (receipt.result.get("fields") or {}).items()
            if hasattr(current, key) and getattr(current, key) == value}


async def capture_final(scope, *, text: str, item_id: str, provider_session: int,
                        occurred_at: datetime | None = None) -> str | None:
    """Persist one final ASR before the Bridge can use it. Never call a model.

    Identical retries return the original identity, including forgotten or
    paused sources; changing a final transcript under that identity is refused.
    """
    if not text.strip():
        return None
    if not item_id or type(provider_session) is not int or provider_session < 0:
        raise jobs.ExtractionSourceInvalid("native_asr_identity_missing")
    if occurred_at is not None and (not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None):
        raise jobs.ExtractionSourceInvalid("native_asr_occurrence_invalid")
    from assistant.commands import _authority
    from memory.policy import resolve_access_scope
    from memory.service import lock_memory_authority
    from memory.settings import saving_paused_locked
    from session.internal_parts import begin_session_write, _lock_fenced

    identity = source_id(user_id=scope.user_id, workspace_id=scope.workspace_id,
        session_id=scope.main_session_id, call_id=scope.call_id,
        provider_session=provider_session, item_id=item_id)
    async with get_db_session() as db:
        await begin_session_write(db)
        session = await _lock_fenced(db, scope.main_session_id, scope.user_id)
        await lock_memory_authority(db, user_id=scope.user_id)
        await _authority(db, user_id=scope.user_id, workspace_id=scope.workspace_id, main_id=session.id)
        if session.parent_id:
            raise jobs.ExtractionSourceInvalid("native_main_unavailable")
        call = await db.get(VoiceCall, scope.call_id)
        if (call is None or call.user_id != scope.user_id or call.workspace_id != scope.workspace_id
                or call.main_session_id != session.id):
            raise jobs.ExtractionSourceInvalid("native_call_unavailable")
        existing = await db.get(MemoryTurnCompletion, _receipt_id(identity))
        if existing:
            boundaries = existing.source_boundaries or []
            if (not is_native_receipt(existing) or len(boundaries) != 1
                    or boundaries[0].get("content_hash") != _body_hash(text)):
                raise jobs.ExtractionSourceInvalid("native_asr_revision_changed")
            return identity
        if call.status != "active":
            raise jobs.ExtractionSourceInvalid("native_call_closed")
        # Source tombstones survive purging the body/receipt; never recreate one.
        if await db.scalar(select(MemoryTombstone.id).where(
                MemoryTombstone.object_kind == "source", MemoryTombstone.object_id == identity)):
            raise jobs.ExtractionSourceInvalid("source_forgotten")
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        paused = await saving_paused_locked(db, scope.user_id, session.id)
        cancellation = "memory_paused" if paused else (
            None if jobs.extraction_write_enabled(scope.user_id) else "extraction_write_disabled")
        branch = _branch(scope.call_id)
        ordinal = 1 + (await db.scalar(select(func.max(MemoryTurnCompletion.ordinal)).where(
            MemoryTurnCompletion.session_id == session.id, MemoryTurnCompletion.branch_id == branch)) or 0)
        now = await jobs._now(db)
        said_at = occurred_at.astimezone(timezone.utc) if occurred_at is not None else now
        metadata = {"capture_version": CAPTURE_VERSION, "actor_user_id": scope.user_id,
                    "voice_call_id": scope.call_id, "provider_session": provider_session, "item_id": item_id}
        boundary = {"id": identity, "source_kind": SOURCE_KIND, "source_revision": 1,
            "session_id": session.id, "branch_id": branch, "turn_id": identity,
            "message_id": None, "part_id": None, "start_seq": ordinal, "end_seq": ordinal,
            "content_hash": _body_hash(text), "occurred_at": said_at.isoformat(),
            "source_metadata": metadata, "forwarded_hash": forwarded_hash(text),
            "capture_cancelled": cancellation}
        source = MemorySource(id=identity, user_id=scope.user_id, workspace_id=scope.workspace_id,
            project_id=None, visibility="PERSONAL", source_kind=SOURCE_KIND, source_revision=1,
            session_id=session.id, branch_id=branch, turn_id=identity, message_id=None, part_id=None,
            start_seq=ordinal, end_seq=ordinal, content_hash=boundary["content_hash"], body=text,
            source_metadata=metadata, acl_epoch=access.acl_epoch, status="ACTIVE",
            occurred_at=said_at, created_at=now)
        receipt = MemoryTurnCompletion(id=_receipt_id(identity), user_id=scope.user_id,
            workspace_id=scope.workspace_id, project_id=None, session_id=session.id, branch_id=branch,
            logical_turn_id=identity, run_id="", run_generation=0, result_message_id="",
            ordinal=ordinal, start_sequence=ordinal, end_sequence=ordinal,
            source_boundaries=[boundary], input_hash=jobs._hash([boundary]),
            acl_hash=await jobs._acl_hash(db, session), pipeline_version=jobs.PIPELINE_VERSION, created_at=now)
        db.add_all([source, receipt])
        await db.flush()
        job = await jobs.enqueue_completion_locked(db, receipt, cancelled=cancellation)
        if cancellation:
            await jobs._advance_cursor_locked(db, job, now)
    return identity


async def prefetch_bindings(db, access, sources):
    """Read native evidence rows once within a single read-only authority pass.

    Only rows are reused, never an authorization verdict. The normal checks
    below still validate every source. These bindings must not cross a SQL
    session, provider wait, mutation or a reader's scope.
    """
    from db.models.session import Session
    from session.policy import active_membership
    def call_id(source):
        metadata = source.source_metadata
        value = metadata.get("voice_call_id") if isinstance(metadata, dict) else None
        return value if isinstance(value, str) else None
    from sqlalchemy import and_, literal, union_all
    bindings = {source.id: (None, None, None) for source in sources}
    requested = [(source.id, call_id(source), _receipt_id(source.id)) for source in sources
                 if call_id(source) is not None]
    for start in range(0, len(requested), 200):
        # Bind each source to its exact deterministic receipt. One fresh SQL
        # snapshot replaces call / per-main / receipt round trips; the normal
        # source_available checks still validate hashes and immutable fields.
        wanted = union_all(*(select(literal(identity).label("source_id"),
            literal(call).label("call_id"), literal(receipt).label("receipt_id"))
            for identity, call, receipt in requested[start:start + 200])).subquery()
        rows = (await db.execute(select(wanted.c.source_id, VoiceCall, Session, MemoryTurnCompletion)
            .select_from(wanted).join(VoiceCall, VoiceCall.id == wanted.c.call_id)
            .join(Session, Session.id == VoiceCall.main_session_id)
            .outerjoin(MemoryTurnCompletion, and_(MemoryTurnCompletion.id == wanted.c.receipt_id,
                MemoryTurnCompletion.user_id == access.user_id,
                MemoryTurnCompletion.workspace_id == access.workspace_id))
            .where(VoiceCall.user_id == access.user_id, VoiceCall.workspace_id == access.workspace_id,
                Session.user_id == access.user_id, Session.workspace_id == access.workspace_id,
                Session.is_deleted.is_(False), Session.kind == "assistant", Session.visibility == "private",
                Session.memory_policy == "assistant_isolated",
                active_membership(access.user_id, access.workspace_id)))).all()
        bindings.update({identity: (call, session, receipt) for identity, call, session, receipt in rows})
    return bindings



async def source_available(db, access, source, *, binding=None) -> bool:
    """Native kind alone is never provenance: check the code-owned call binding."""
    from assistant.commands import _authority
    from assistant.policy import AssistantError
    metadata = source.source_metadata
    if not isinstance(metadata, dict):
        return False
    if (source.source_kind != SOURCE_KIND or source.project_id is not None
            or source.occurred_at is None or source.body is None
            or source.source_revision != 1 or source.message_id or source.part_id
            or metadata.get("capture_version") != CAPTURE_VERSION
            or metadata.get("actor_user_id") != access.user_id
            or type(metadata.get("provider_session")) is not int or metadata["provider_session"] < 0
            or not isinstance(metadata.get("item_id"), str) or not metadata["item_id"]
            or not isinstance(metadata.get("voice_call_id"), str)):
        return False
    call = binding[0] if binding is not None else await db.get(VoiceCall, metadata["voice_call_id"])
    if (call is None or call.user_id != access.user_id or call.workspace_id != access.workspace_id
            or call.main_session_id != source.session_id):
        return False
    try:
        session = binding[1] if binding is not None else await _authority(db, user_id=access.user_id,
            workspace_id=access.workspace_id, main_id=call.main_session_id)
    except AssistantError:
        return False
    if session is None or session.parent_id:
        return False
    identity = source_id(user_id=access.user_id, workspace_id=access.workspace_id, session_id=session.id,
        call_id=call.id, provider_session=metadata["provider_session"], item_id=metadata["item_id"])
    if source.id != identity or source.turn_id != identity or source.branch_id != _branch(call.id):
        return False
    receipt = binding[2] if binding is not None else await db.get(MemoryTurnCompletion, _receipt_id(identity))
    if (not is_native_receipt(receipt) or receipt.user_id != access.user_id
            or receipt.workspace_id != access.workspace_id or receipt.session_id != session.id
            or receipt.logical_turn_id != identity or len(receipt.source_boundaries or []) != 1
            or jobs._hash(receipt.source_boundaries) != receipt.input_hash):
        return False
    boundary = receipt.source_boundaries[0]
    return (boundary.get("id") == identity and boundary.get("source_metadata") == metadata
            and boundary.get("source_kind") == SOURCE_KIND
            and boundary.get("content_hash") == source.content_hash
            and boundary.get("source_revision") == source.source_revision
            and boundary.get("start_seq") == source.start_seq == source.end_seq == receipt.ordinal
            and boundary.get("occurred_at") == jobs._aware(source.occurred_at).isoformat())


async def human_identity(db, access, source):
    """An exact-read provenance view after the same native authority check."""
    from memory.service import source_is_available
    if not await source_is_available(db, access, source):
        raise jobs.ExtractionSourceInvalid("native_source_unavailable")
    return {"source_kind": SOURCE_KIND, "source_id": source.id,
            "origin_ref": deepcopy(source.source_metadata), "full_text_hash": source.content_hash,
            "source_span": {"start": 0, "end": len(source.body), "total_chars": len(source.body), "complete": True}}


async def validate_sources(db, job, receipt, acl_hash):
    """Called at every frozen-input boundary, including immediately before commit."""
    from memory.policy import resolve_access_scope
    from memory.service import source_body_is_available
    boundaries = receipt.source_boundaries or []
    if (not is_native_receipt(receipt) or len(boundaries) != 1
            or receipt.project_id is not None or job.project_id is not None
            or receipt.logical_turn_id != job.logical_turn_id
            or receipt.branch_id != job.branch_id or receipt.ordinal != job.ordinal
            or receipt.start_sequence != receipt.ordinal or receipt.end_sequence != receipt.ordinal
            or jobs._hash(boundaries) != job.input_hash or cancelled_reason(receipt)):
        raise jobs.ExtractionSourceInvalid("native_boundary_invalid")
    boundary = boundaries[0]
    source = await db.get(MemorySource, boundary.get("id"))
    access = await resolve_access_scope(db, user_id=job.user_id, workspace_id=job.workspace_id)
    if (source is None or source.source_kind != SOURCE_KIND or source.session_id != job.session_id
            or not source.body or source.content_hash != boundary.get("content_hash")
            or not await source_body_is_available(db, access, source)):
        raise jobs.ExtractionSourceInvalid("native_source_unavailable")
    if len(source.body) > jobs.MAX_SOURCE_CHARS:
        raise OverflowError("source_budget_exceeded")
    return [{**deepcopy(boundary), "body": source.body, "occurred_at": jobs._aware(source.occurred_at)}], acl_hash


async def exclude_forwarded_sources(db, session, boundaries, messages):
    """An exact server-bound forward is the same evidence, even after forgetting.

    Keep the immutable receipt (no body) as its dedupe proof. Looking only at
    currently readable sources would re-learn deleted or paused speech later.
    """
    kept = []
    for boundary in boundaries:
        part = next((part for part in messages[boundary["message_id"]].get("parts", [])
                     if part["id"] == boundary["part_id"]), {})
        data = jobs._part_data(part)
        origin = data.get("origin_ref") or {}
        identity = origin.get("native_voice_source_id")
        if not identity:
            kept.append(boundary)
            continue
        receipt = await db.get(MemoryTurnCompletion, _receipt_id(identity))
        native = (receipt.source_boundaries or [None])[0] if receipt else None
        metadata = (native or {}).get("source_metadata") or {}
        if (not is_native_receipt(receipt) or not native or len(receipt.source_boundaries) != 1
                or receipt.user_id != session.user_id or receipt.workspace_id != session.workspace_id
                or receipt.session_id != session.id or receipt.logical_turn_id != identity
                or jobs._hash(receipt.source_boundaries) != receipt.input_hash
                or origin.get("actor_user_id") != session.user_id
                or metadata.get("voice_call_id") != origin.get("voice_call_id")
                or metadata.get("item_id") != (origin.get("voice_context") or {}).get("spoken_input_id")
                or native.get("forwarded_hash") != forwarded_hash(str(data.get("text") or ""))):
            raise jobs.ExtractionSourceInvalid("native_forward_binding_invalid")
    return kept
