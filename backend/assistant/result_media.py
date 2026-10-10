"""Carry a task's generated media into its personal-assistant report.

The original asset remains in its project. Only a file-part reference is
copied, in the same transaction as the report receipt and canonical events.
"""
from datetime import datetime, timezone

from sqlalchemy import exists, select

from db.models.agent_event import AgentEvent
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from models.message import FilePart, FileRelation


def is_output_media(part: Part) -> bool:
    data = part.data
    relation = data.get("relation") or {}
    return (part.type == "file" and bool(data.get("asset_id"))
            and str(data.get("mime_type") or "").startswith(("image/", "video/"))
            and not data.get("transient") and not data.get("ignored")
            and relation.get("role") not in {"input", "evidence"}
            and relation.get("kind") not in {"computer_screenshot", "inspection_image"})


async def execution_media(db, execution, terminal) -> list[Part]:
    """All assistant media in the completed logical turn, including tool steps.

    A turn may span question/resume generations. The canonical turn boundary
    excludes older turns and any work committed after this result settled.
    """
    if terminal is None:
        return []
    occurrence = select(AgentEvent.id).where(
        AgentEvent.session_id == execution.id, AgentEvent.user_id == execution.user_id,
        AgentEvent.part_id == Part.id, AgentEvent.message_id == Part.message_id,
        AgentEvent.kind.in_(("part.created", "part.updated")),
        AgentEvent.sequence <= terminal.sequence,
    )
    if terminal.turn_id:
        occurrence = occurrence.where(AgentEvent.turn_id == terminal.turn_id)
    else:
        occurrence = occurrence.where(AgentEvent.run_id == terminal.run_id,
                                      AgentEvent.generation == terminal.generation)
    parts = (await db.scalars(select(Part).join(Message, Message.id == Part.message_id).where(
        Part.session_id == execution.id, Part.user_id == execution.user_id, Part.type == "file",
        Message.session_id == execution.id, Message.user_id == execution.user_id,
        Message.role == "assistant", exists(occurrence),
    ).order_by(Part.created_at, Part.id))).all()
    return [part for part in parts if is_output_media(part)]


async def report_media(db, task, parts) -> list[tuple[Part, FileAsset]]:
    """Ready owned assets explicitly returned by this task, including reused media."""
    candidates = [part for ref, part in parts if ref.get("kind") == "report"
                  and part.session_id == task.execution_session_id and is_output_media(part)]
    if not candidates:
        return []
    assets = {asset.id: asset for asset in (await db.scalars(select(FileAsset).where(
        FileAsset.id.in_({p.data["asset_id"] for p in candidates}),
        FileAsset.user_id == task.user_id, FileAsset.workspace_id == task.workspace_id,
        FileAsset.source == "agent",
        FileAsset.status == "ready", FileAsset.is_deleted.is_(False), FileAsset.transient.is_(False),
    ))).all()}
    found, seen = [], set()
    for part in candidates:
        asset = assets.get(part.data["asset_id"])
        if asset is not None and asset.mime.startswith(("image/", "video/")) and asset.id not in seen:
            found.append((part, asset))
            seen.add(asset.id)
    return found


async def attach_report_media_locked(db, main, message, result, *, run_fence) -> list[dict]:
    from assistant.policy import AssistantError
    from assistant.results import validate_result_source
    from db.models.project import Project
    from session.agent_event_log import append_part_event_locked
    from session.session import record_projection_in_tx

    try:
        task, parts = await validate_result_source(db, result, user_id=main.user_id,
            workspace_id=main.workspace_id, main_id=main.id)
    except AssistantError:
        # A revoked source cannot introduce new media. The existing V2 text
        # report policy remains independent of optional media availability.
        return []
    media = await report_media(db, task, parts)
    if not media:
        return []
    project = await db.get(Project, task.project_id)
    created = []
    for source, asset in media:
        relation = FileRelation.model_validate(source.data.get("relation") or {})
        relation.metadata = {**relation.metadata, "assistant_source": {
            "task_id": task.id, "result_id": result.id, "session_id": task.execution_session_id,
            "message_id": source.message_id, "part_id": source.id,
            "project_id": task.project_id, "project_name": project.name if project else None,
            "title": task.title,
        }}
        relation.group_id = f"assistant-result:{result.id}:{relation.group_id or asset.id}"
        # A source-tool ID belongs to the project transcript, not this message.
        relation.source_part_id = None
        part = FilePart(path=source.data.get("path") or asset.name, mime_type=asset.mime,
            asset_id=asset.id, size=asset.size, relation=relation,
            session_id=main.id, message_id=message.id)
        data = part.model_dump(mode="json")
        row = Part(id=part.id, message_id=message.id, session_id=main.id,
            user_id=main.user_id, type="file", data=data, created_at=datetime.now(timezone.utc))
        db.add(row)
        await db.flush()
        await append_part_event_locked(db, main, row, message, operation="created", run_fence=run_fence)
        await record_projection_in_tx(db, main.id, main.user_id, "part.committed",
            {"part": row.data, "operation": "created"}, message_id=message.id, part_id=part.id)
        created.append({key: value for key, value in row.data.items() if key not in {"session_id", "message_id"}})
    return created
