"""Private resource inventory and durable attachment input on the original Task."""
from sqlalchemy import and_, or_
from assets.service import owned_ready_query, resource_query
from assistant.commands import _authority, _project, accept_task_command, command_digest
from assistant.policy import AssistantError
from assistant.reads import _page
from assistant.transactions import read_session
from db.models.file_asset import FileAsset


async def list_assets(*, user_id, workspace_id, main_id, project_id=None, query="",
                      source=None, session_id=None, kind=None, limit=50, cursor=None, db=None):
    if (not 1 <= limit <= 50 or len(query) > 200 or source not in (None, "user", "agent")
            or kind not in (None, "image", "video", "audio")):
        raise ValueError("Invalid asset inventory filter")
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if project_id:
            await _project(db, project_id, user_id, workspace_id)
        statement = resource_query(user_id, workspace_id, owned_only=True)
        if project_id:
            statement = statement.where(FileAsset.project_id == project_id)
        if source:
            statement = statement.where(FileAsset.source == source)
        if session_id:
            statement = statement.where(FileAsset.session_id == session_id)
        if kind:
            statement = statement.where(FileAsset.mime.startswith(kind + "/"))
        if query.strip():
            statement = statement.where(or_(FileAsset.name.icontains(query.strip(), autoescape=True),
                FileAsset.description.icontains(query.strip(), autoescape=True)))
        if cursor:
            anchor = await db.scalar(statement.where(FileAsset.id == cursor))
            if anchor is None:
                raise ValueError("The resource cursor is unavailable for these filters")
            statement = statement.where(or_(FileAsset.created_at < anchor.created_at,
                and_(FileAsset.created_at == anchor.created_at, FileAsset.id < anchor.id)))
        # Generated image IDs contain random hashes, not timestamps. Sorting
        # by ID hid recent frames behind unrelated older library files.
        rows = list((await db.scalars(statement.order_by(
            FileAsset.created_at.desc(), FileAsset.id.desc()).limit(limit + 1))).all())
        # No object keys, presigned URLs, credentials or file contents enter the
        # main assistant. The execution input carries only the original IDs.
        return _page(rows, limit, lambda row: {key: getattr(row, key) for key in (
            "id", "name", "description", "mime", "size", "source", "project_id", "session_id", "created_at")})


async def sources(db, main, arguments, value):
    resources = []
    if arguments.get("project_id"):
        project = await _project(db, arguments["project_id"], main.user_id, main.workspace_id)
        resources.append({"kind": "project_filter", "id": project.id})
    items = value.get("items")
    if (not isinstance(items, list) or len(items) > 50 or any(not isinstance(x, dict) for x in items)
            or len({x.get("id") for x in items}) != len(items)):
        raise AssistantError(410, "ASSISTANT_BUSINESS_UNVERIFIED", "The asset observation is unavailable")
    for item in items:
        row = await db.scalar(owned_ready_query(main.user_id, main.workspace_id).where(FileAsset.id == item.get("id")))
        if row is None:
            raise AssistantError(410, "ASSISTANT_ASSET_UNAVAILABLE", "The original resource is unavailable")
        # Filing and renaming are progress. They do not revoke an owner's old
        # metadata observation. Replacing its object identity or losing current
        # access does. This is metadata evidence, never a hash of the file bytes.
        identity = {key: getattr(row, key) for key in ("id", "user_id", "workspace_id", "oss_key", "size", "mime")}
        resources.append({"kind": "asset", "id": row.id, "scope_digest": command_digest(identity)})
    return {"resources": resources, "tasks": []}


async def attach_assets(*, user_id, workspace_id, main_id, task_id, attachment_ids,
                        text, expected_revision, idempotency_key, source=None,
                        delivery="followup", expected_run=None):
    return await accept_task_command(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
        task_id=task_id, prompt=text, attachments=attachment_ids, expected_revision=expected_revision,
        idempotency_key=idempotency_key, source=source, delivery=delivery, expected_run=expected_run,
        command_action="asset_attach")
