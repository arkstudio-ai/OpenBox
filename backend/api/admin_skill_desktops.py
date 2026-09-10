"""Live, explicitly scoped desktop skill management; never acquires compute."""

import asyncio
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.user import User
from db.models.user_skill import UserSkill
from db.models.workspace import Workspace, WorkspaceMember
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox.channel import ChannelNotReady, route_for_record
from sandbox.client import SandboxClient, user_scope_for

router = APIRouter(dependencies=[Depends(require_admin)])


class RemoveInstalled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(skill|mcp)$")
    install_dir: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


async def _members(workspace_id: str | None, fallback: str | None) -> list[dict]:
    async with get_db_session() as session:
        if workspace_id:
            active = await session.scalar(
                select(Workspace.id).where(
                    Workspace.id == workspace_id, Workspace.is_deleted.is_(False)
                )
            )
            if not active:
                return []
            member_ids = select(WorkspaceMember.user_id).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.status == "active",
            )
            owner_ids = select(Workspace.owner_user_id).where(
                Workspace.id == workspace_id
            )
            where = or_(User.id.in_(member_ids), User.id.in_(owner_ids))
        else:
            where = User.id == fallback
        rows = (
            await session.execute(
                select(User.id, User.username, User.email).where(
                    where, User.is_deleted.is_(False)
                )
            )
        ).all()
    return [{"id": r.id, "username": r.username, "email": r.email} for r in rows]


@router.get("/desktops")
async def desktops(
    request: Request,
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    admin: dict = Depends(require_admin),
):
    stmt = (
        select(CloudDesktop, Workspace.name, User.username)
        .outerjoin(Workspace, Workspace.id == CloudDesktop.workspace_id)
        .outerjoin(
            User,
            User.id == func.coalesce(Workspace.owner_user_id, CloudDesktop.user_id),
        )
        .where(
            CloudDesktop.is_deleted.is_(False),
            CloudDesktop.pool_state == "assigned",
            CloudDesktop.desktop_id.is_not(None),
            or_(CloudDesktop.workspace_id.is_(None), Workspace.is_deleted.is_(False)),
        )
    )
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                CloudDesktop.desktop_id.ilike(pattern),
                User.username.ilike(pattern),
                User.email.ilike(pattern),
                Workspace.name.ilike(pattern),
            )
        )
    async with get_db_session() as session:
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await session.execute(
                stmt.order_by(CloudDesktop.updated_at.desc(), CloudDesktop.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    items = []
    for desktop, workspace_name, username in rows:
        members = await _members(desktop.workspace_id, desktop.user_id)
        items.append(
            {
                "id": desktop.id,
                "desktop_id": desktop.desktop_id,
                "workspace_id": desktop.workspace_id,
                "workspace_name": workspace_name,
                "username": username,
                "status": desktop.status,
                "channel_state": desktop.tunnel_state,
                "members": members,
            }
        )
    await record(
        admin["user_id"],
        None,
        "admin.skill.view_desktops",
        "cloud_desktop",
        None,
        {"offset": offset, "limit": limit},
        request,
    )
    return {"items": items, "total": int(total or 0), "offset": offset, "limit": limit}


async def _target(desktop_id: str, user_id: str) -> tuple[dict, SandboxClient]:
    desktop = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
    if (
        desktop is None
        or desktop.get("is_deleted")
        or desktop.get("pool_state") != "assigned"
    ):
        raise HTTPException(404, detail="Assigned desktop not found")
    members = await _members(desktop.get("workspace_id"), desktop.get("user_id"))
    if user_id not in {m["id"] for m in members}:
        raise HTTPException(
            404, detail="User does not belong to this desktop's workspace"
        )
    if desktop.get("status") != "running":
        raise HTTPException(
            409, detail="Desktop is not running; scanning does not start it"
        )
    try:
        host, port, key = route_for_record(desktop)
    except (ChannelNotReady, RuntimeError) as exc:
        raise HTTPException(
            503, detail="Desktop channel is unavailable; installed state is unknown"
        ) from exc
    return desktop, SandboxClient(
        host=host,
        port=port,
        api_key=key,
        desktop_id=desktop_id,
        workspace_id=desktop.get("workspace_id"),
        user_scope=user_scope_for(user_id),
        catalogue_ttl_seconds=0,
    )


def _project(rows: list[dict], kind: str) -> list[dict]:
    result = {}
    for row in rows:
        name = str(row.get("name") or "")
        target = str(row.get("install_dir") or name)
        source = row.get("source") or "unknown"
        if target in result:
            item = result[target]
            if name not in item["names"]:
                item["names"].append(name)
            # Collections are uninstalled by physical directory, not subskill.
            item["name"] = target
            item["removable"] = item["removable"] and (
                kind == "mcp" or source == "container"
            )
            continue
        result[target] = {
            "kind": kind,
            "name": name,
            "install_dir": target,
            "names": [name],
            "description": str(row.get("description") or "")[:4000],
            "source": source,
            "icon": str(row.get("icon") or "")[:2048],
            "removable": kind == "mcp" or source == "container",
        }
    return list(result.values())


@router.get("/desktops/{desktop_id}/skills")
async def scan(
    desktop_id: str,
    request: Request,
    user_id: str = Query(min_length=1),
    admin: dict = Depends(require_admin),
):
    desktop, client = await _target(desktop_id, user_id)
    try:
        async with asyncio.timeout(12):
            results = await asyncio.gather(
                client.list_skills(), client.list_mcp_servers(), return_exceptions=True
            )
    except TimeoutError as exc:
        raise HTTPException(
            504, detail="Desktop scan timed out; installed state is unknown"
        ) from exc
    items, unavailable = [], []
    for kind, result in zip(("skill", "mcp"), results):
        if isinstance(result, list) and all(isinstance(row, dict) for row in result):
            items.extend(_project(result, kind))
        else:
            unavailable.append(kind)
    if len(unavailable) == 2:
        raise HTTPException(
            503, detail="Desktop cannot be reached; installed state is unknown"
        )
    await record(
        admin["user_id"],
        desktop.get("workspace_id"),
        "admin.skill.scan",
        "cloud_desktop",
        desktop_id,
        {"user_id": user_id, "count": len(items), "unavailable": unavailable},
        request,
    )
    return {
        "items": items,
        "unavailable": unavailable,
        "desktop_id": desktop_id,
        "user_id": user_id,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/desktops/{desktop_id}/uninstall")
async def uninstall(
    desktop_id: str,
    body: RemoveInstalled,
    request: Request,
    admin: dict = Depends(require_admin),
):
    # Never interpolate untrusted paths into a command/URL or accept a broad target.
    if (
        not body.reason.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", body.install_dir)
        or ".." in body.install_dir
    ):
        raise HTTPException(
            422, detail="A safe package identifier and a reason are required"
        )
    desktop, client = await _target(desktop_id, body.user_id)
    try:
        async with asyncio.timeout(15):
            live = await (
                client.list_skills()
                if body.kind == "skill"
                else client.list_mcp_servers()
            )
            matches = [
                row
                for row in _project(live, body.kind)
                if row["install_dir"] == body.install_dir
            ]
            if len(matches) != 1:
                raise HTTPException(
                    409, detail="Package changed or is absent; scan again"
                )
            if not matches[0]["removable"]:
                raise HTTPException(
                    409,
                    detail="System or unknown-source packages cannot be uninstalled here",
                )
            result = await (
                client.uninstall_skill(body.install_dir)
                if body.kind == "skill"
                else client.remove_mcp_server(body.install_dir)
            )
            if isinstance(result, dict) and (
                result.get("ok") is False or result.get("error")
            ):
                raise HTTPException(
                    502, detail="Desktop rejected uninstall; scan again"
                )
            after = await (
                client.list_skills()
                if body.kind == "skill"
                else client.list_mcp_servers()
            )
            if any(
                row["install_dir"] == body.install_dir
                for row in _project(after, body.kind)
            ):
                raise HTTPException(
                    502, detail="Package is still present after uninstall; scan again"
                )
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(
            504, detail="Uninstall timed out; scan to verify the actual result"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            502, detail="Uninstall could not be verified; scan again"
        ) from exc
    # Preserve authored packages and other users' installations. Disable only
    # this owner's automatic restore; uninstall must not resurrect on next GET.
    if body.kind == "skill":
        async with get_db_session() as session:
            owned = (
                (
                    await session.execute(
                        select(UserSkill)
                        .where(
                            UserSkill.owner_id == body.user_id,
                            UserSkill.workspace_id == desktop.get("workspace_id"),
                            UserSkill.install_dir == body.install_dir,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in owned:
                row.metadata_data = {
                    **(row.metadata_data or {}),
                    "admin_restore_disabled": True,
                }
    await record(
        admin["user_id"],
        desktop.get("workspace_id"),
        "admin.skill.uninstall",
        "cloud_desktop",
        desktop_id,
        {
            "user_id": body.user_id,
            "kind": body.kind,
            "install_dir": body.install_dir,
            "reason": body.reason,
        },
        request,
    )
    return {
        "ok": True,
        "desktop_id": desktop_id,
        "user_id": body.user_id,
        "install_dir": body.install_dir,
    }
