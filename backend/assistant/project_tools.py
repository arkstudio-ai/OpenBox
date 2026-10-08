"""Creating a project the user asked for (docs/ASSISTANT_VOICE_FIX_PLAN.md 2).

The assistant creates a project only on the user's explicit request, or when
the user names a project that does not exist, and then hands work to it in the
same turn. A project of the user's with that name (case and spacing aside) is
returned instead of a duplicate, the default project included.

No cloud desktop is started or created for it: the project's directory is made
by the first run in it (sandbox.manager), so the assistant never borrows
api/projects.py's sandbox acquisition, which may start the user's desktop.
"""
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import select

from assistant.commands import _authority, _project, _tool_source_locked, command_digest, tool_command_key
from assistant.policy import AssistantError, lock_actor
from core.identifier import generate_id
from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.project import Project
from memory.redaction import redact_credentials
from session.internal_parts import begin_session_write

MAX_NAME = 128
MAX_DESCRIPTION = 500


def link(project_id: str) -> str:
    """A new conversation in this project."""
    return f"/app?project={project_id}"


def _clean(text: str | None) -> str:
    return redact_credentials(re.sub(r"\s+", " ", text or "").strip())


async def _command(db, *, user_id, workspace_id, main_id, key):
    return await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
        AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
        AssistantCommand.idempotency_key == key))


async def existing_project(db, *, user_id: str, workspace_id: str, name: str):
    """The user's own live project with this name, or the default one for its directory name."""
    from project.workspace import DEFAULT_SLUG, slugify
    wanted = _clean(name).casefold()
    rows = list((await db.scalars(select(Project).where(Project.user_id == user_id,
        Project.workspace_id == workspace_id, Project.is_deleted.is_(False))
        .order_by(Project.created_at, Project.id).limit(1000))).all())
    named = next((row for row in rows if _clean(row.name).casefold() == wanted), None)
    if named is None and slugify(name) == DEFAULT_SLUG:
        named = next((row for row in rows if row.slug == DEFAULT_SLUG), None)
    return named


def _plain(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").casefold()


async def _said(db, source_ref: dict, name: str) -> bool:
    """Whether the user's cited words contain the name (spacing, punctuation and case aside)."""
    from db.models.part import Part
    ids = [ref["part_id"] for ref in source_ref.get("source_refs") or [] if ref.get("part_id")]
    texts = (await db.scalars(select(Part.data).where(Part.id.in_(ids)))).all() if ids else []
    return bool(_plain(name)) and _plain(name) in _plain(" ".join(str((data or {}).get("text") or "") for data in texts))


async def _create(*, user_id: str, workspace_id: str, name: str, description: str | None):
    from project import workspace
    try:
        return await workspace.create_project(user_id, workspace_id, name, description=description)
    except workspace.ProjectError:
        # The name is valid, so its directory name is the problem: reserved,
        # kept by another project (names change, directories never do) or a
        # generated one that collided. A random one is always the user's own.
        pass
    try:
        return await workspace.create_project(user_id, workspace_id, name, description=description,
                                              slug=f"project-{uuid4().hex[:8]}")
    except workspace.ProjectError as exc:
        raise AssistantError(400, "ASSISTANT_PROJECT_INVALID", str(exc)) from exc


async def create_project(*, user_id: str, workspace_id: str, main_id: str, name: str,
                         description: str | None = None, source=None) -> dict:
    """Create the named project, or return the user's existing one with that name.

    A tool call is one command (its persisted call is the key): repeating the
    call returns its first receipt and never creates a second project.
    """
    name, description = _clean(name), _clean(description) or None
    if not 1 <= len(name) <= MAX_NAME:
        raise AssistantError(400, "ASSISTANT_PROJECT_INVALID", f"A project name of 1 to {MAX_NAME} characters is required")
    if description and len(description) > MAX_DESCRIPTION:
        raise AssistantError(400, "ASSISTANT_PROJECT_INVALID", f"A description is at most {MAX_DESCRIPTION} characters")
    key = tool_command_key(main_id, source.part_id) if source is not None else None
    digest = command_digest({"action": "project_create", "name": name, "description": description,
        "source": {"part_id": source.part_id, "source_message_ids": list(source.source_message_ids)}
        if source is not None else {"origin": "human"}})
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        main = await _authority(db, **scope)
        prior = await _command(db, **scope, key=key) if key else None
        if prior is not None:
            if prior.payload_digest != digest:
                raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
            await _project(db, prior.target_id, user_id, workspace_id)
            return dict(prior.receipt)
        source_ref = (await _tool_source_locked(db, main, source, "project_create") if source is not None else
                      {"actor_user_id": user_id, "entrypoint": "assistant_project_create"})
        if source is not None and not await _said(db, source_ref, name):
            # Measured on a voice turn: a fast model named it after an older topic, then made a second one.
            raise AssistantError(400, "ASSISTANT_PROJECT_NAME_UNSAID",
                "The cited user words do not contain this project name. Use the name exactly as the user said it "
                "(their words, not a summary); if they left the name to you, propose one and ask first.")
        found = await existing_project(db, user_id=user_id, workspace_id=workspace_id, name=name)
        project = (found.id, found.name, "existing") if found is not None else None
    if project is None:
        created = await _create(user_id=user_id, workspace_id=workspace_id, name=name, description=description)
        project = (created.id, created.name, "created")
    project_id, project_name, state = project
    receipt = {"project_id": project_id, "name": project_name, "state": state, "link": link(project_id)}
    if key is None:
        return receipt
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        await _authority(db, **scope)
        prior = await _command(db, **scope, key=key)
        if prior is not None:
            return dict(prior.receipt)
        now = datetime.now(timezone.utc)
        command = AssistantCommand(id=generate_id(), actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=key, action="project_create", target_type="project",
            target_id=project_id, payload_digest=digest, source_ref=source_ref, state="applied", receipt={},
            created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        receipt = {"command_id": command.id, **receipt}
        command.receipt = receipt
    return receipt
