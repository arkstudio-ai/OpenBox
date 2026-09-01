"""Session routes: CRUD + messages + agent control."""
import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

from auth.middleware import get_current_user
from auth.quota import check_session_quota, check_concurrent_agents
from core.config import get_config
from session import session as session_mod
from models.message import SessionStatus

_background_tasks = set()  # prevent GC of background tasks

router = APIRouter(dependencies=[Depends(get_current_user)])

_ACTIVE_SESSION_STATUSES = {SessionStatus.BUSY, SessionStatus.COMPACTING}
ReasoningVariant = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
]


class CreateSessionBody(BaseModel):
    model: str = ""
    agent: str = "build"
    variant: ReasoningVariant | None = None
    title: str | None = None
    #: Project the session runs in. Accepts an id or a slug; omitting it files
    #: the session under the user's default project.
    project_id: str | None = None


class PromptBody(BaseModel):
    text: str = Field(min_length=1, max_length=65_536)
    agent: str | None = None
    model: str | None = None
    #: Video model picked in the composer. Recorded on the session so the
    #: video tools can read it when the agent reaches them; a segment then
    #: freezes it at submission, leaving in-flight work untouched.
    video_model: str | None = None
    #: Omitted inherits the conversation; explicit null selects the model's default.
    variant: ReasoningVariant | None = None
    client_message_id: str | None = Field(default=None, max_length=64)
    #: followup waits for the next turn, steer joins the next step of a live
    #: turn, and inject joins that boundary without waking an idle Session.
    delivery: Literal["followup", "steer", "inject"] = "followup"
    # {"type": "json_schema", "schema": {...}} to require a structured answer.
    format: dict | None = None
    #: Ready file_assets ids — pulled from OSS into the sandbox before the
    #: agent loop starts, and attached to the user message as file parts.
    attachments: list[str] | None = Field(default=None, max_length=32)

    @field_validator("client_message_id")
    @classmethod
    def _reserve_platform_message_ids(cls, value: str | None) -> str | None:
        if value and value.startswith(("sjr:", "tabort:")):
            raise ValueError("client_message_id uses a platform-reserved prefix")
        return value

    @field_validator("attachments")
    @classmethod
    def _validate_attachment_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not asset_id or len(asset_id) > 64 for asset_id in value):
            raise ValueError("attachment ids must be 1..64 characters")
        if len(set(value)) != len(value):
            raise ValueError("attachment ids must be unique")
        return value


class UpdateSessionBody(BaseModel):
    title: str | None = None
    agent: str | None = None
    model: str | None = None
    variant: ReasoningVariant | None = None
    #: The video model this conversation generates with. Independent of
    #: `model`: segments snapshot it at submission, so a switch only reaches
    #: work not yet started.
    video_model: str | None = None


class CommandBody(BaseModel):
    command: str
    arguments: str | None = None


async def _reserve_prompt_run(session_id: str, user_id: str):
    """Reserve one driver before the prompt is accepted.

    Reservation happens synchronously, before the user message and before the
    background task wake.  Concurrent prompts therefore cannot both observe
    ``idle`` and start independent loops.  A later prompt preempts the current
    generation and retries the fenced claim.
    """
    from agent.driver import (
        DriverBusyError,
        DriverQuotaExceededError,
        DriverRecoveryRequiredError,
        reserve_run,
    )
    from session.status import discard_pending_abort

    # An old stop with no owner belongs to work the user has moved on from.
    discard_pending_abort(session_id)
    deadline = time.monotonic() + 20.0
    while True:
        try:
            return await reserve_run(session_id, user_id)
        except DriverQuotaExceededError as exc:
            raise HTTPException(
                429,
                detail={
                    "code": "CONCURRENT_AGENT_QUOTA_EXCEEDED",
                    "message": (
                        "Concurrent agent quota exceeded: "
                        f"{exc.used}/{exc.limit} agents running."
                    ),
                    "used": exc.used,
                    "limit": exc.limit,
                },
                headers={"X-Error-Code": "CONCURRENT_AGENT_QUOTA_EXCEEDED"},
            ) from exc
        except DriverRecoveryRequiredError as exc:
            # An expired non-idle marker is the only durable identity of the
            # interrupted turn. Repair it under an exact recovery claim before
            # accepting a replacement prompt; never overwrite it in reserve.
            if time.monotonic() >= deadline:
                raise HTTPException(
                    409,
                    "The previous agent run still needs recovery; retry shortly.",
                )
            from agent.recovery import repair_interrupted_session

            await repair_interrupted_session(
                session_id,
                user_id,
                recovered=exc.record,
            )
            continue
        except DriverBusyError as exc:
            if time.monotonic() >= deadline:
                raise HTTPException(
                    409,
                    "The previous agent run is still finalizing; retry shortly.",
                )
            from session.abort import abort_session_turn

            # Stop only the generation that made this reservation fail. A
            # concurrent prompt may replace it while we wait; stale cleanup
            # then loses its exact maintenance claim instead of touching the
            # replacement's trigger or todo list. The loop retries against
            # whatever generation is current after that.
            await abort_session_turn(
                session_id,
                user_id,
                reason="preempted",
                was_active=True,
                expected_run_id=exc.run_id,
                expected_generation=exc.generation,
            )


async def _remember_model(session, requested: str | None, user_id: str) -> str:
    """Settle on a model for this turn and keep it on the session.

    Each conversation carries its own model, so reopening one restores what it
    was last using instead of snapping back to the global default. A model the
    deployment no longer offers is replaced here rather than at the provider,
    where it returns an opaque "no channel for model X" that the retry layer
    attempts five times before the turn fails.
    """
    from agent.model_resolve import resolve as resolve_model

    chosen, _dropped = resolve_model(requested or session.model, get_config(),
                                     context=f"session {session.id}")
    if chosen and chosen != session.model:
        await session_mod.update_session(session.id, model=chosen, user_id=user_id)
    return chosen


async def _remember_video_model(session, requested: str | None, user_id: str) -> None:
    """Keep the composer's video pick on the session.

    Unlike the chat model this is not resolved or substituted: an unknown id
    must reach the submit path and fail there by name, because silently
    swapping a video model would spend real money on something the user did
    not choose. An empty string clears the pick back to the deployment default.
    """
    if requested is None:
        return
    value = requested.strip() or None
    if value != getattr(session, "video_model", None):
        await session_mod.update_session(session.id, video_model=value, user_id=user_id)


def _resolve_prompt_model(session, requested: str | None) -> str:
    """Resolve the queued turn without mutating a currently running Session."""
    from agent.model_resolve import resolve as resolve_model

    chosen, _dropped = resolve_model(
        requested or session.model,
        get_config(),
        context=f"session {session.id} inbox",
    )
    return chosen


def _checked_variant(model_id: str, requested: str | None) -> str | None:
    """Validate a reasoning choice against the exact configured model route."""
    from agent.llm import validate_reasoning_variant

    try:
        return validate_reasoning_variant(model_id, requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _resolve_prompt_variant(session, body: PromptBody, model_id: str) -> str | None:
    """Resolve tri-state reasoning without mutating a live Agent generation."""
    explicit = "variant" in body.model_fields_set
    selected = body.variant if explicit else getattr(session, "variant", None)
    if selected is None:
        return None
    try:
        return _checked_variant(model_id, selected)
    except HTTPException:
        # An explicit unsupported choice is a client error. An inherited value
        # from a previous model is safely cleared for this queued turn.
        if explicit:
            raise
        return None


async def _accept_prompt(session, body: PromptBody, user_id: str):
    """Commit acceptance before any driver reservation or sandbox wake."""
    from agent.inbox import (
        InboxAttachmentError,
        InboxIdempotencyConflict,
        accept_inbox_item,
    )

    chosen_model = _resolve_prompt_model(session, body.model)
    chosen_variant = _resolve_prompt_variant(session, body, chosen_model)
    try:
        return await accept_inbox_item(
            session_id=session.id,
            user_id=user_id,
            delivery=body.delivery,
            prompt=body.text,
            attachments=body.attachments or (),
            client_id=body.client_message_id,
            agent=body.agent or session.agent,
            model=chosen_model,
            video_model=(
                body.video_model.strip() or None
                if body.video_model is not None
                else session.video_model
            ),
            variant=chosen_variant,
            output_format=body.format,
        )
    except InboxIdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except InboxAttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _remember_prompt_history(user_id: str, text: str) -> None:
    try:
        from api.prompt_history import save_prompt_history_async

        asyncio.create_task(save_prompt_history_async(user_id, text))
    except Exception:
        pass


async def _hydrate_inbox_result(session_id: str, user_id: str, receipt):
    """Hydrate the Message bound to this exact item, not merely the newest."""
    target_id = receipt.result_message_id or receipt.message_id
    if target_id is None:
        raise HTTPException(409, "The accepted prompt has no materialized result")
    messages = await session_mod.get_messages(session_id, user_id=user_id)
    for message in messages:
        if message.id == target_id:
            return message
    raise HTTPException(409, "The accepted prompt result is unavailable")


async def _require_session_owned(session_id: str, user_id: str):
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


async def _hydrate_completed_message(session_id: str, user_id: str, result):
    """Return the final persisted Message rather than run_loop's light pointer.

    The loop deliberately keeps only the terminal Assistant identity in memory
    after a stop.  The synchronous HTTP endpoint, however, promises a complete
    message.  Re-read the newest row so its model, finish, error and Parts match
    what a reconnect immediately observes.
    """
    latest = await session_mod.get_messages(
        session_id,
        user_id=user_id,
        latest=True,
        limit=1,
    )
    if latest and latest[-1].id == result.id:
        return latest[-1]
    return result


# ─── Sandbox Status ───

@router.get("/sandbox/status")
async def sandbox_status(current_user: dict = Depends(get_current_user)):
    """Check if a sandbox container is available and healthy."""
    user_id = current_user["user_id"]
    from sandbox.manager import sandbox_manager
    return await sandbox_manager.check_health(user_id=user_id)


@router.get("/session/{session_id}/sandbox")
async def get_session_sandbox(session_id: str, current_user: dict = Depends(get_current_user)):
    """Get sandbox container info bound to this session."""
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")

    from sandbox.manager import sandbox_manager
    info = sandbox_manager.get_info(session_id)
    if info and info.user_id and info.user_id != user_id:
        return {"available": False}
    if not info:
        # No sandbox tracked for this session — fall back to health check
        health = await sandbox_manager.check_health(user_id=user_id)
        if health.get("available"):
            return {
                "available": True,
                "container_id": health["container_id"],
                "container_name": health.get("container_name"),
            }
        return {"available": False}
    return {
        "available": True,
        "container_id": info.container_id,
        "port": info.port,
        "project_id": session.project_id,
    }


# ─── Session CRUD ───

@router.post("/session")
async def create_session(
    body: CreateSessionBody = CreateSessionBody(),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    config = get_config()
    await check_session_quota(user_id, config)
    # Validate at birth so a retired model never gets stored in the first place.
    from agent.model_resolve import resolve as resolve_model
    model, _ = resolve_model(body.model, config, context="new session")
    variant = _checked_variant(model, body.variant)
    session = await session_mod.create_session(
        model=model,
        agent=body.agent,
        variant=variant,
        title=body.title,
        user_id=user_id,
        project_id=body.project_id,
    )
    return session.model_dump()


@router.get("/session")
async def list_sessions(
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Sessions for the user; pass project_id to narrow to one project."""
    user_id = current_user["user_id"]
    sessions = await session_mod.list_sessions(project_id=project_id, user_id=user_id)
    return [s.model_dump() for s in sessions]


@router.get("/session/{session_id}")
async def get_session(session_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")
    from project.workspace import get_project, workdir_for_session
    data = session.model_dump()
    # The namespaced project directory this session's tools run in.
    # The files panel scopes its tree here rather than to the whole sandbox.
    data["directory"] = await workdir_for_session(session)
    project = await get_project(session.project_id, user_id)
    data["project_name"] = project.name if project else ""
    return data


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    deleted = await session_mod.delete_session(session_id, user_id=user_id)
    if not deleted:
        raise HTTPException(404, "Session not found")
    return {"ok": True}


@router.patch("/session/{session_id}")
async def update_session(session_id: str, body: UpdateSessionBody, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    current = await _require_session_owned(session_id, user_id)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    target_model = updates.get("model") or current.model
    if "variant" in body.model_fields_set:
        # Keep explicit null: unlike the other optional PATCH fields it means
        # "return to this model's default", not "leave unchanged".
        updates["variant"] = _checked_variant(target_model, body.variant)
    elif "model" in body.model_fields_set and current.variant is not None:
        # A model switch cannot carry an adapter-owned value into a different
        # family. Preserve it only when the new route advertises the same id.
        try:
            updates["variant"] = _checked_variant(target_model, current.variant)
        except HTTPException:
            updates["variant"] = None
    session = await session_mod.update_session(session_id, user_id=user_id, **updates)
    if not session:
        raise HTTPException(404, "Session not found")
    return session.model_dump()


# ─── Messages ───

@router.get("/session/{session_id}/message")
async def get_messages(session_id: str, offset: int = 0, limit: int = 200, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    messages = await session_mod.get_messages(
        session_id,
        offset=offset,
        limit=limit,
        user_id=user_id,
        latest=True,
    )
    return [m.model_dump() for m in messages]


@router.post("/session/{session_id}/message")
async def send_message(
    session_id: str,
    body: PromptBody,
    current_user: dict = Depends(get_current_user),
):
    """Accept one durable input and wait for that exact item to terminate."""
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")
    receipt = await _accept_prompt(session, body, user_id)
    if receipt.created:
        _remember_prompt_history(user_id, body.text)

    from agent.inbox import wait_for_inbox_terminal, wake_inbox_session

    if body.delivery == "inject" and receipt.state not in {"canceled", "settled"}:
        from agent.inbox import get_inbox_item

        current = await get_inbox_item(
            receipt.id,
            user_id=user_id,
            session_id=session_id,
        )
        current = current or receipt
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "inboxId": current.id,
                "state": current.state,
                "runId": current.run_id,
            },
        )
    if receipt.state == "accepted" and body.delivery != "inject":
        await wake_inbox_session(session_id, user_id)
    terminal = await wait_for_inbox_terminal(receipt.id, user_id=user_id)
    if terminal.state == "canceled":
        detail = (terminal.error or {}).get("message", "Prompt canceled")
        raise HTTPException(409, detail)
    return (
        await _hydrate_inbox_result(session_id, user_id, terminal)
    ).model_dump()


@router.post("/session/{session_id}/prompt_async")
async def send_message_async(
    session_id: str,
    body: PromptBody,
    current_user: dict = Depends(get_current_user),
):
    """Durably accept input, then best-effort wake without preempting busy work."""
    user_id = current_user["user_id"]
    config = get_config()
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status not in _ACTIVE_SESSION_STATUSES:
        # Replacing this Session's own active turn reuses its existing quota
        # slot. New work on an idle Session must acquire a fresh slot.
        await check_concurrent_agents(user_id, config)

    receipt = await _accept_prompt(session, body, user_id)
    if receipt.created:
        _remember_prompt_history(user_id, body.text)

    run_id = receipt.run_id
    if receipt.state == "accepted" and body.delivery != "inject":
        from agent.inbox import wake_inbox_session

        run_id = await wake_inbox_session(session_id, user_id) or run_id
    from agent.inbox import get_inbox_item

    current = await get_inbox_item(receipt.id, user_id=user_id, session_id=session_id)
    return {
        "ok": True,
        "inboxId": receipt.id,
        "state": current.state if current is not None else receipt.state,
        "runId": (current.run_id if current is not None else None) or run_id,
    }


@router.delete("/session/{session_id}/inbox/{item_id}")
async def cancel_inbox_item(
    session_id: str,
    item_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Cancel an input until an exact Agent generation has claimed it."""
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    from agent.inbox import cancel_inbox_items, get_inbox_item

    receipt = await get_inbox_item(item_id, user_id=user_id, session_id=session_id)
    if receipt is None:
        raise HTTPException(404, "Inbox item not found")
    if receipt.state == "canceled":
        return {"ok": True, "inboxId": item_id, "state": "canceled"}
    if receipt.state != "accepted":
        raise HTTPException(409, "Inbox item has already been claimed")
    canceled = await cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        item_ids=(item_id,),
        reason="user_canceled",
    )
    if not canceled:
        current = await get_inbox_item(item_id, user_id=user_id, session_id=session_id)
        if current is not None and current.state == "canceled":
            return {"ok": True, "inboxId": item_id, "state": "canceled"}
        raise HTTPException(409, "Inbox item has already been claimed")
    return {"ok": True, "inboxId": item_id, "state": "canceled"}


async def _attach_file_parts(
    session_id: str,
    message_id: str,
    user_id: str,
    asset_ids: list[str],
    *,
    run_fence: tuple[str, str, int] | None = None,
) -> None:
    from sqlalchemy import select
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.session import Session as SessionRow
    from models.message import FilePart, FileRelation
    from project.workspace import asset_sandbox_path

    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(FileAsset).where(
                    FileAsset.id.in_(asset_ids),
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                )
            )
        ).scalars().all()
        # An attachment picked before the chat existed has no home yet; sending
        # it is what files it, so the resource centre sees it under this
        # conversation's project.
        project_id = (
            await db.execute(
                select(SessionRow.project_id).where(SessionRow.id == session_id)
            )
        ).scalar_one_or_none()
        changed = False
        for row in rows:
            if not row.session_id:
                row.session_id = session_id
                changed = True
            if not row.project_id and project_id:
                row.project_id = project_id
                changed = True
        if changed:
            await db.commit()
    by_id = {r.id: r for r in rows}
    for asset_id in asset_ids:
        row = by_id.get(asset_id)
        if not row:
            continue
        await session_mod.save_part(
            FilePart(
                id=ascending("part"),
                path=asset_sandbox_path(
                    user_id,
                    project_id,
                    row.name,
                    asset_id=row.id,
                ),
                mime_type=row.mime,
                asset_id=row.id,
                oss_key=row.oss_key,
                size=row.size,
                relation=FileRelation(
                    group_id=f"message:{message_id}:attachments",
                    role="input",
                    kind="user_attachment",
                    label=row.name,
                ),
                session_id=session_id,
                message_id=message_id,
            ),
            is_new=True,
            user_id=user_id,
            run_fence=run_fence,
        )


# ─── Session fork ───

class ForkBody(BaseModel):
    message_id: str | None = None


class ReactionBody(BaseModel):
    reaction: str | None = None  # "up" | "down" | null to clear


class RegenerateBody(BaseModel):
    # Optional: retry on a different model. A turn that failed because of the
    # model it ran on is the main reason anyone presses regenerate.
    model: str | None = None
    #: Independently switchable — a turn can fail on the video model alone.
    video_model: str | None = None


@router.get("/session/{session_id}/diff/step")
async def get_step_diff(
    session_id: str,
    from_snapshot: str,
    to_snapshot: str,
    current_user: dict = Depends(get_current_user),
):
    """Line-level diff for one step's snapshot range.

    The session diff is cumulative (first snapshot → last); a per-turn change
    card needs exactly what that step touched, which is this.
    """
    from snapshot import snapshot
    await _require_session_owned(session_id, current_user["user_id"])
    if not from_snapshot or not to_snapshot or from_snapshot == to_snapshot:
        return []
    return await snapshot.diff_full(
        from_snapshot, to_snapshot, session_id=session_id, user_id=current_user["user_id"]
    )


@router.post("/session/{session_id}/message/{message_id}/reaction")
async def set_message_reaction(
    session_id: str,
    message_id: str,
    body: ReactionBody,
    current_user: dict = Depends(get_current_user),
):
    """Thumbs up / down on an assistant reply (frontend-v2 message meta bar)."""
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    if body.reaction not in (None, "up", "down"):
        raise HTTPException(400, "reaction must be 'up', 'down' or null")
    await session_mod.set_message_reaction(
        message_id,
        session_id,
        body.reaction,
        user_id=user_id,
    )
    return {"ok": True, "reaction": body.reaction}


@router.delete("/session/{session_id}/message/{message_id}")
async def dismiss_failed_turn(
    session_id: str,
    message_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove a turn that failed, once the user has moved past it.

    Only errored assistant messages qualify — see `delete_failed_turn`. A 404
    here means "that message is not a failure", not "no such session".
    """
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    removed = await session_mod.delete_failed_turn(session_id, message_id, user_id=user_id)
    if not removed:
        raise HTTPException(404, "No failed turn to dismiss at that message")
    return {"ok": True, "removed": removed}


@router.post("/session/{session_id}/regenerate/{message_id}")
async def regenerate_message(
    session_id: str,
    message_id: str,
    body: RegenerateBody | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Answer the same prompt again, discarding this assistant turn.

    `message_id` is the assistant message to replace. It and everything after
    it are removed, leaving the user message that prompted it as the newest in
    the history — which is exactly the state `run_loop` expects, so no new user
    message is created and the prompt is not duplicated.

    The common case is a turn that failed: the error is the only thing the user
    can see, and without this there is nothing to act on but retyping.
    """
    user_id = current_user["user_id"]
    config = get_config()
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")

    if session.status not in _ACTIVE_SESSION_STATUSES:
        await check_concurrent_agents(user_id, config)

    lease = await _reserve_prompt_run(session_id, user_id)

    try:
        # Let the caller switch models on the way, so "it failed on this model"
        # and "try it on another one" are a single action.
        if body and body.model:
            await _remember_model(session, body.model, user_id)
        # Independent of the chat model: a retry may switch only the video model.
        if body:
            await _remember_video_model(session, body.video_model, user_id)

        last_user = await session_mod.delete_messages_from(
            session_id,
            message_id,
            user_id=user_id,
            replacement_run_id=lease.run_id,
            replacement_generation=lease.generation,
        )
        if not last_user:
            raise HTTPException(404, "Nothing to regenerate: no prompt precedes that message")
    except BaseException:
        await lease.release(session_status="idle")
        raise

    # No explicit "messages were deleted" event: the loop is about to emit a
    # fresh message stream for this same prompt, which is what any other open
    # tab needs anyway. The caller invalidates its own snapshot on success.
    task = asyncio.create_task(_run_loop_with_log(session_id, user_id, lease=lease))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "from_message": last_user, "runId": lease.run_id}


@router.post("/session/{session_id}/fork")
async def fork_session_endpoint(
    session_id: str,
    body: ForkBody,
    current_user: dict = Depends(get_current_user),
):
    """Fork a session from a specific message point."""
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")

    from session.fork import fork_session
    try:
        new_session = await fork_session(
            source_session_id=session_id,
            up_to_message_id=body.message_id,
            user_id=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return new_session


# ─── Plan accept / reject ───

@router.post("/session/{session_id}/plan/accept")
async def accept_plan(session_id: str, current_user: dict = Depends(get_current_user)):
    """User accepted the plan — switch to build agent."""
    user_id = current_user["user_id"]
    from tool.plan import _update_plan_part_status
    from session.session import get_session, plan_path_for

    await _require_session_owned(session_id, user_id)
    lease = await _reserve_prompt_run(session_id, user_id)
    try:
        await _update_plan_part_status(
            session_id,
            "accepted",
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        session = await get_session(session_id, user_id=user_id)
        plan_path = await plan_path_for(session) if session else ""
        message = await session_mod.create_user_message(
            session_id=session_id,
            text=f"The plan at {plan_path} has been approved, you can now edit files. Execute the plan",
            agent="build",
            model=session.model if session else None,
            synthetic=True,
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
            bind_trigger=True,
        )
    except BaseException:
        await lease.release(session_status="idle")
        raise

    task = asyncio.create_task(_run_loop_with_log(session_id, user_id, lease=lease))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "runId": lease.run_id}


@router.post("/session/{session_id}/plan/reject")
async def reject_plan(session_id: str, current_user: dict = Depends(get_current_user)):
    """User rejected the plan — plan agent regenerates."""
    user_id = current_user["user_id"]
    from tool.plan import _update_plan_part_status
    from session.session import get_session

    await _require_session_owned(session_id, user_id)
    lease = await _reserve_prompt_run(session_id, user_id)
    try:
        await _update_plan_part_status(
            session_id,
            "rejected",
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        session = await get_session(session_id, user_id=user_id)
        message = await session_mod.create_user_message(
            session_id=session_id,
            text="The user rejected the plan. Please revise and create a better plan based on their feedback.",
            agent="plan",
            model=session.model if session else None,
            synthetic=True,
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
            bind_trigger=True,
        )
    except BaseException:
        await lease.release(session_status="idle")
        raise

    task = asyncio.create_task(_run_loop_with_log(session_id, user_id, lease=lease))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "runId": lease.run_id}


@router.post("/session/{session_id}/abort")
async def abort_session(session_id: str, current_user: dict = Depends(get_current_user)):
    """Abort a running session."""
    from session.abort import abort_session_turn

    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    # Stop means stop the conversation, including accepted followups that have
    # not yet acquired an exact generation. Claimed input remains owned by the
    # generation below and is settled by its normal abort/finalization path.
    from agent.inbox import cancel_inbox_items

    canceled = await cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        reason="user_stop",
    )
    # Cancellation and a local wake race through the same durable Session row.
    # Snapshot the Driver only *after* cancellation commits, then CAS-abort that
    # exact generation. A stale Session.status read must never turn Stop into a
    # no-op after the wake has already claimed the item.
    from agent.driver import get_driver_state

    state = await get_driver_state(session_id)
    if state is not None and state.phase != "idle" and state.run_id:
        marked = await abort_session_turn(
            session_id,
            user_id,
            reason="user_stop",
            was_active=True,
            expected_run_id=state.run_id,
            expected_generation=state.generation,
        )
    else:
        marked = False
    return {"ok": True, "marked": marked, "canceledInbox": len(canceled)}


# ─── Compaction ───

@router.post("/session/{session_id}/summarize")
async def summarize_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger context compaction.

    Creates the compaction request, then runs the agent loop in the background
    so the compaction is actually processed (matching opencode's flow).
    """
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    lease = await _reserve_prompt_run(session_id, user_id)
    from agent.compaction import create_compaction
    try:
        message = await create_compaction(
            session_id,
            auto=False,
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
            bind_trigger=True,
        )
        if message is None:
            raise HTTPException(500, "Could not create compaction request")
    except BaseException:
        await lease.release(session_status="idle")
        raise

    # Run agent loop so the compaction gets processed
    from agent.loop import run_loop
    background_tasks.add_task(run_loop, session_id, user_id, lease=lease)
    return {"ok": True, "runId": lease.run_id}


# ─── Revert ───

@router.post("/session/{session_id}/revert/{message_id}")
async def revert_to_message(session_id: str, message_id: str, current_user: dict = Depends(get_current_user)):
    """Revert session to a specific message (undo changes)."""
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    from session.revert import revert_to_message as do_revert
    success = await do_revert(session_id, message_id, user_id=user_id)
    if not success:
        raise HTTPException(400, "Revert failed: no snapshot found for message")
    return {"ok": True}


@router.post("/session/{session_id}/unrevert")
async def unrevert(session_id: str, current_user: dict = Depends(get_current_user)):
    """Undo a revert."""
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    from session.revert import unrevert as do_unrevert
    success = await do_unrevert(session_id, user_id=user_id)
    if not success:
        raise HTTPException(400, "Unrevert failed: no revert to undo")
    return {"ok": True}


# ─── Command ───

@router.post("/session/{session_id}/command")
async def execute_command(
    session_id: str,
    body: CommandBody,
    current_user: dict = Depends(get_current_user),
):
    """Execute a slash command."""
    user_id = current_user["user_id"]
    from command.command import get_command, execute_command as resolve_command

    # Look up the command
    cmd_info = await get_command(body.command)
    if not cmd_info:
        raise HTTPException(404, f"Command '{body.command}' not found")

    # Resolve the command template with arguments
    resolved_text = await resolve_command(body.command, body.arguments or "")

    # Validate session
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status in _ACTIVE_SESSION_STATUSES:
        raise HTTPException(409, "Session is busy")

    lease = await _reserve_prompt_run(session_id, user_id)

    # Use command's agent if specified, otherwise fall back to session's agent
    agent = cmd_info.agent or session.agent

    # Create a user message with the resolved command text
    try:
        message = await session_mod.create_user_message(
            session_id=session_id,
            text=resolved_text,
            agent=agent,
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
            bind_trigger=True,
        )
    except BaseException:
        await lease.release(session_status="idle")
        raise

    # Launch agent loop in background (results arrive via SSE)
    task = asyncio.create_task(_run_loop_with_log(session_id, user_id, lease=lease))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"ok": True, "runId": lease.run_id}


# ─── Todo ───

@router.get("/session/{session_id}/todo")
async def get_todo(session_id: str, current_user: dict = Depends(get_current_user)):
    # Ownership check is mandatory; todo key is session_id-based.
    # Without this, users can read others' todo lists by guessing IDs.
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)
    from session.todo import get_todo
    todo = await get_todo(session_id)
    return todo.model_dump()


class TodoAddBody(BaseModel):
    subject: str
    #: Insert right after this item; append when absent or unknown.
    after_id: str | None = None


async def _record_todo_edit(session_id: str, user_id: str, items: list) -> None:
    """Append the edited list to the conversation, as a TodoPart.

    The card is rebuilt from the part stream, not from the todo store, so an
    edit that only touched the store would be invisible on reload and would
    leave the tool rows around it attributed to the wrong task. The part
    hangs off the newest assistant message — a session with none has no card
    to edit yet, so there is nothing to record.
    """
    from models.message import TodoPart
    from session.session import get_messages, save_part

    messages = await get_messages(session_id, user_id=user_id)
    anchor = next((m for m in reversed(messages) if m.role == "assistant"), None)
    if anchor is None:
        return
    await save_part(
        TodoPart(
            items=items,
            source="user",
            session_id=session_id,
            message_id=anchor.id,
        ),
        is_new=True,
        user_id=user_id,
    )


@router.post("/session/{session_id}/todo/items")
async def add_todo_item(
    session_id: str, body: TodoAddBody, current_user: dict = Depends(get_current_user)
):
    """Add a task the user typed on the card."""
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)

    subject = body.subject.strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Task cannot be empty")

    from session.todo import add_notice, add_todo_item as add_item
    todo = await add_item(session_id, subject, after_id=body.after_id, user_id=user_id)
    await add_notice(session_id, f"- added: {subject}")
    await _record_todo_edit(session_id, user_id, todo.items)
    return todo.model_dump()


@router.delete("/session/{session_id}/todo/items/{item_id}")
async def remove_todo_item(
    session_id: str, item_id: str, current_user: dict = Depends(get_current_user)
):
    """Drop a task the user dismissed.

    Their own item goes away; one the model planned is marked cancelled, so
    the card keeps a struck-through trace and the model can see it was
    overruled rather than silently losing a step.
    """
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)

    from session.todo import add_notice, get_todo, remove_todo_item as remove_item
    before = await get_todo(session_id)
    target = next((t for t in before.items if t.id == item_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Task not found")

    todo = await remove_item(session_id, item_id, user_id=user_id)
    await add_notice(session_id, f"- dropped: {target.subject}")
    await _record_todo_edit(session_id, user_id, todo.items)
    return todo.model_dump()


# ─── Plan ───

class PlanUpdateBody(BaseModel):
    content: str


@router.get("/session/{session_id}/plan")
async def get_plan(session_id: str, current_user: dict = Depends(get_current_user)):
    """Read plan file content from sandbox."""
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")

    from session.session import plan_path_for
    pp = await plan_path_for(session)

    from sandbox import sandbox_manager
    client = await sandbox_manager.get_client(session_id, user_id=user_id)
    if not client:
        raise HTTPException(404, "No sandbox for session")

    try:
        content = await client.read_file_raw(pp)
        return {"content": content, "path": pp}
    except (FileNotFoundError, Exception):
        return {"content": None, "path": pp}


@router.put("/session/{session_id}/plan")
async def update_plan(session_id: str, body: PlanUpdateBody, current_user: dict = Depends(get_current_user)):
    """Save the user's edits to the plan.

    Both copies have to move together. The file is what the build agent reads
    once the plan is approved; the plan part is what the card shows. Writing
    only the file would leave the user looking at the text they replaced while
    the agent went off and built something else.
    """
    user_id = current_user["user_id"]
    session = await session_mod.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(404, "Session not found")

    from session.session import plan_path_for
    pp = await plan_path_for(session)

    from sandbox import sandbox_manager
    client = await sandbox_manager.get_client(session_id, user_id=user_id)
    if not client:
        raise HTTPException(404, "No sandbox for session")

    await client.write_file(pp, body.content)
    await _sync_plan_part_content(session_id, body.content, user_id)
    return {"ok": True, "path": pp}


async def _sync_plan_part_content(session_id: str, content: str, user_id: str) -> None:
    """Point the newest plan part at what the user just saved."""
    from session.session import get_messages, update_part_data

    messages = await get_messages(session_id, user_id=user_id)
    for msg in reversed(messages):
        for part in reversed(msg.parts or []):
            p = part if isinstance(part, dict) else (
                part.model_dump() if hasattr(part, "model_dump") else {}
            )
            if isinstance(p, dict) and p.get("type") == "plan":
                p["content"] = content
                await update_part_data(p["id"], p, publish=True, user_id=user_id)
                return


# ─── Diff ───

@router.get("/session/{session_id}/diff")
async def get_diff(session_id: str, full: bool = False, current_user: dict = Depends(get_current_user)):
    """Get file change diff for a session.

    Uses the first and last snapshots in the session to compute the diff.
    Pass ?full=true to include hunks with line-level detail.
    """
    from snapshot import snapshot
    from db.base import get_db_session
    from db.models.part import Part as PartORM
    from sqlalchemy import select
    user_id = current_user["user_id"]
    await _require_session_owned(session_id, user_id)

    # Find first step-start and last step-finish snapshots directly from parts table.
    # This avoids loading all messages/parts (which can be expensive on long sessions).
    first_snapshot = None
    last_snapshot = None
    async with get_db_session() as db:
        result = await db.execute(
            select(PartORM.type, PartORM.data)
            .where(
                PartORM.session_id == session_id,
                PartORM.type.in_(("step-start", "step-finish")),
            )
            .order_by(PartORM.created_at.asc())
        )
        rows = result.all()

    if not rows:
        return []

    for part_type, part_data in rows:
        p = part_data if isinstance(part_data, dict) else {}
        snapshot_id = p.get("snapshot")
        if not snapshot_id:
            continue
        if part_type == "step-start" and not first_snapshot:
            first_snapshot = snapshot_id
        elif part_type == "step-finish":
            last_snapshot = snapshot_id

    if not first_snapshot or not last_snapshot:
        return []

    if full:
        return await snapshot.diff_full(
            first_snapshot, last_snapshot, session_id=session_id, user_id=user_id
        )
    else:
        diffs = await snapshot.diff(
            first_snapshot, last_snapshot, session_id=session_id, user_id=user_id
        )
        return [{"path": d.path, "additions": d.additions, "deletions": d.deletions, "status": d.status} for d in diffs]


async def _run_loop_with_log(
    session_id: str,
    user_id: str,
    attachment_ids: list[str] | None = None,
    *,
    lease,
):
    """Wrapper that logs exceptions from run_loop.

    Attachments land in the session's tenant/project namespace BEFORE the loop
    starts, so every path already written into the message is resolvable.
    """
    loop_started = False
    try:
        if attachment_ids:
            try:
                await _deliver_attachments(session_id, user_id, attachment_ids)
            except Exception as e:
                import logging
                logging.getLogger("sessions").warning(f"attachment delivery failed for {session_id}: {e}")
        from agent.loop import run_loop
        loop_started = True
        await run_loop(session_id, user_id=user_id, lease=lease)
    except Exception as e:
        import logging
        logging.getLogger("sessions").error(f"run_loop FAILED for {session_id}: {e}", exc_info=True)
    finally:
        if not loop_started:
            await lease.release(session_status="error")


async def _deliver_attachments(session_id: str, user_id: str, asset_ids: list[str]) -> None:
    """Pull the message's OSS assets into the sandbox via obx-file."""
    from sandbox.assets import deliver_asset_ids

    await deliver_asset_ids(session_id, user_id, asset_ids)
