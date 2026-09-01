"""Config and metadata routes."""
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from auth.middleware import get_current_user, require_admin
from pydantic import BaseModel

router = APIRouter(dependencies=[Depends(get_current_user)])


class InstallSkillBody(BaseModel):
    url: str | None = None
    name: str | None = None
    content: str | None = None


class AddMcpServerBody(BaseModel):
    name: str
    type: str = "stdio"
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout: int = 60


@router.get("/config")
async def get_config():
    """Get application configuration."""
    from core.config import get_config
    config = get_config()

    # The context window and vision support are resolved here rather than
    # passed through raw, so the frontend always receives a real number and a
    # real boolean — it has no business re-deriving either from a model id.
    from agent.agent import default_agent_name
    from agent.compaction import get_model_context_limit
    from agent.vision import supports_vision

    # Build model list: use explicit models list if configured, else fall back to single model
    if config.models:
        models = []
        for m in config.models:
            models.append({
                "id": m.id,
                "name": m.name or m.id.split("/")[-1],
                "provider": m.provider or (m.id.split("/")[0] if "/" in m.id else ""),
                "max_tokens": m.max_tokens,
                "context_limit": get_model_context_limit(m.id),
                "vision": supports_vision(m.id),
            })
    else:
        models = [{
            "id": config.model,
            "name": config.model.split("/")[-1],
            "provider": config.model.split("/")[0] if "/" in config.model else "",
            "max_tokens": 200000,
            "context_limit": get_model_context_limit(config.model),
            "vision": supports_vision(config.model),
        }]

    video_models = _video_models(config)
    configured_video_default = config.video_generation.model
    default_video_model = next(
        (
            row["id"]
            for row in video_models
            if row["id"] == configured_video_default
        ),
        video_models[0]["id"] if video_models else "",
    )

    return {
        "models": _chat_models(
            config,
            context_limit=get_model_context_limit,
            supports_vision=supports_vision,
        ),
        "default_model": config.model,
        "default_agent": default_agent_name(),
        "video_models": video_models,
        "default_video_model": default_video_model,
    }


def _chat_models(config, *, context_limit, supports_vision) -> list[dict]:
    """Frontend chat-model catalogue with route-owned reasoning controls."""

    from agent.llm import reasoning_profile

    declared = list(config.models or [])
    if not declared:
        profile = reasoning_profile(config.model)
        return [{
            "id": config.model,
            "name": config.model.split("/")[-1],
            "provider": config.model.split("/")[0] if "/" in config.model else "",
            "max_tokens": 200000,
            "context_limit": context_limit(config.model),
            "vision": supports_vision(config.model),
            "variants": list(profile.variants),
            "default_variant": profile.default_variant,
        }]

    rows = []
    for model in declared:
        profile = reasoning_profile(model.id)
        rows.append({
            "id": model.id,
            "name": model.name or model.id.split("/")[-1],
            "provider": model.provider or (model.id.split("/")[0] if "/" in model.id else ""),
            "max_tokens": model.max_tokens,
            "context_limit": context_limit(model.id),
            "vision": supports_vision(model.id),
            "variants": list(profile.variants),
            "default_variant": profile.default_variant,
        })
    return rows


def _video_models(config) -> list[dict]:
    """Selectable video models for the composer picker.

    Declared entries are authoritative. When a deployment declares none, the
    single configured default is still returned so the picker always has
    something real to show rather than an empty menu.
    """
    settings = config.video_generation
    declared = list(settings.models or [])
    if not declared:
        if not _video_model_is_bound(config, settings.model):
            return []
        return [{
            "id": settings.model,
            "name": settings.model,
            "channel": "ark",
            "tier": "",
            "resolutions": [],
            "max_duration_seconds": None,
        }]
    allowed = set(settings.allowed_models or [])
    return [
        {
            "id": m.id,
            "name": m.name or m.id,
            "channel": m.channel,
            "tier": m.tier,
            "resolutions": list(m.resolutions or []),
            "max_duration_seconds": m.max_duration_seconds,
        }
        for m in declared
        # An allowed_models whitelist, when set, also governs what the picker
        # may offer — otherwise the UI would advertise a model the submit path
        # refuses.
        if (not allowed or m.id in allowed) and _video_model_is_bound(config, m.id)
    ]


def _video_model_is_bound(config, model_id: str) -> bool:
    """Keep the picker aligned with the exact non-network submit resolver."""
    try:
        from tool.video_providers import resolve_route

        resolve_route(model_id, config)
    except Exception:
        return False
    return True


@router.get("/agent")
async def list_agents():
    """List available agents."""
    from agent.agent import list_agents
    agents = list_agents()
    return [
        {
            "name": a.name,
            "description": a.description,
            "model": a.model or "",
            "temperature": a.temperature,
            "tools": a.tools,
            "mode": a.mode,
            "color": a.color,
        }
        for a in agents
    ]


@router.get("/skill")
async def list_skills(current_user: dict = Depends(get_current_user)):
    """List available skills — container and host merged, container winning.

    The same union the agent's skill tool advertises: showing only the
    container's list hid host-side skills that the loop could in fact load.
    Personal skills also have a durable owner snapshot.  Restore missing live
    copies when possible, but keep the library row visible when the sandbox is
    offline or restoration fails so downloads do not disappear with compute.
    """
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]

    library_available = False
    owned_skills: list[dict] = []
    try:
        from skill.user_library import (
            annotate_installed_skills,
            list_owned_skills,
            restore_personal_skills_to_sandbox,
        )

        owned_skills = await list_owned_skills(user_id)
        library_available = True
    except (ImportError, RuntimeError):
        # Single-user mode deliberately runs without the central database.
        pass

    merged: list[dict] = []
    client = None
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            container = await client.list_skills()
            if isinstance(container, list):
                if library_available:
                    container = await restore_personal_skills_to_sandbox(
                        user_id,
                        client,
                        owned_skills=owned_skills,
                        installed_skills=container,
                    )
                merged.extend(container)
    except Exception:
        pass

    try:
        from skill.skill import list_skills
        seen = {s.get("name") for s in merged}
        for s in await list_skills():
            if s.name in seen:
                continue
            merged.append({"name": s.name, "description": s.description, "source": s.source})
    except ImportError:
        pass

    if not library_available:
        return merged

    try:
        annotated = await annotate_installed_skills(user_id, merged)
    except RuntimeError:
        return merged

    live_library_ids = {
        item.get("library_id")
        for item in annotated
        if item.get("category") == "personal" and item.get("library_id")
    }
    occupied_keys = {
        item.get("install_dir") or item.get("name")
        for item in annotated
        if item.get("install_dir") or item.get("name")
    }
    library_only: list[dict] = []
    for owned in owned_skills:
        if owned.get("id") in live_library_ids:
            continue
        item = {**owned, "source": "library"}
        # The frontend groups by install_dir. If another live package owns the
        # author's old path (for example a store copy with the same slug), use
        # the durable id as this non-live row's action key so both stay visible.
        key = item.get("install_dir") or item.get("name")
        if key in occupied_keys:
            item["install_dir"] = item["id"]
        library_only.append(item)

    return library_only + annotated


@router.get("/skill/{name}")
async def get_skill(name: str, current_user: dict = Depends(get_current_user)):
    """Get skill details."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            return await client.get_skill(name)
    except Exception:
        pass
    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


@router.post("/skill/{name}/publish")
async def publish_skill(name: str, current_user: dict = Depends(get_current_user)):
    """Publish the owner's current personal-skill snapshot to the shared store."""
    from sandbox.manager import sandbox_manager
    from skill.user_library import (
        annotate_installed_skills,
        get_owned_skill,
        publish_personal_skill,
        upsert_personal_snapshot,
    )

    user_id = current_user["user_id"]
    owned = await get_owned_skill(user_id, name)
    if not owned:
        # A filesystem copy is not proof of authorship.  In particular, store
        # installs and manually uploaded archives must never become publishable
        # merely because somebody guessed this endpoint.
        raise HTTPException(status_code=404, detail="Personal skill not found")
    client = await sandbox_manager.get_client_any(user_id=user_id)
    if not client:
        raise HTTPException(status_code=503, detail="No sandbox available")
    try:
        install_dir = owned["install_dir"]
        info = await client.get_skill(install_dir)
        annotated = await annotate_installed_skills(user_id, [info])
        if not annotated or annotated[0].get("category") != "personal":
            raise HTTPException(
                status_code=409,
                detail="The live package is not this user's personal skill",
            )
        archive = await client.download_skill_archive(install_dir)
        await upsert_personal_snapshot(user_id, info, archive)
        return await publish_personal_skill(user_id, install_dir)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/skill/{name}/download")
async def download_skill(name: str, current_user: dict = Depends(get_current_user)):
    """Download a user-owned personal skill as a portable ZIP archive."""
    from sandbox.manager import sandbox_manager
    from skill.user_library import (
        annotate_installed_skills,
        get_owned_skill,
        upsert_personal_snapshot,
    )

    user_id = current_user["user_id"]
    owned = await get_owned_skill(user_id, name)
    if not owned:
        raise HTTPException(status_code=404, detail="Personal skill not found")
    # Prefer a fresh snapshot so edits made after creation are included. The
    # durable snapshot remains a fallback when the user's sandbox is offline.
    # Refresh only when the live directory is still registered as personal;
    # a store install reusing the same slug must not overwrite the author's
    # durable package.
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            info = await client.get_skill(owned["install_dir"])
            annotated = await annotate_installed_skills(user_id, [info])
            if annotated and annotated[0].get("category") == "personal":
                archive = await client.download_skill_archive(owned["install_dir"])
                await upsert_personal_snapshot(user_id, info, archive)
    except Exception:
        pass

    row = await get_owned_skill(user_id, owned["id"], include_archive=True)
    if not row:
        raise HTTPException(status_code=404, detail="Personal skill not found")
    filename = f"{row['name']}.zip"
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        BytesIO(row["archive_data"]),
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(row["archive_size"]),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/skill/install")
async def install_skill(body: InstallSkillBody, current_user: dict = Depends(get_current_user)):
    """Install a skill into the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available. Send a message first to create one.")
        return await client.install_skill(
            url=body.url,
            name=body.name,
            content=body.content,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/skill/{name}")
async def uninstall_skill(name: str, current_user: dict = Depends(get_current_user)):
    """Uninstall a skill from the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")

        category = None
        target = name
        from skill.user_library import (
            annotate_installed_skills,
            get_owned_skill,
        )

        # Ownership is durable database state, not a property inferred only
        # from a currently reachable filesystem entry. Fail closed if this
        # lookup is unavailable: deleting an unclassified personal package
        # without its tombstone/fence would let the next restore revive it.
        owned = await get_owned_skill(user_id, name)
        try:
            live = await client.get_skill(name)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            missing_live = status == 404 or isinstance(
                exc,
                (FileNotFoundError, LookupError),
            )
            if owned and missing_live:
                category = "personal"
                target = owned.get("install_dir") or name
            else:
                raise
        else:
            if isinstance(live, dict):
                target = live.get("install_dir") or name
                annotated = await annotate_installed_skills(
                    user_id,
                    [{**live, "source": live.get("source") or "container"}],
                )
                if annotated:
                    category = annotated[0].get("category")

        if category == "personal":
            from skill.user_library import uninstall_owned_skill

            # Personal uninstall is one fenced lifecycle operation: advance
            # the durable generation before the execution-plane delete, then
            # commit the database tombstone only after that delete succeeds.
            result = await uninstall_owned_skill(user_id, target, client)
        else:
            result = await client.uninstall_skill(target)
        try:
            if category == "store":
                from skill.user_library import remove_community_installation

                await remove_community_installation(user_id, target)
        except RuntimeError:
            pass
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill/upload")
async def upload_skill_archive(
    file: UploadFile = File(...),
    name: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """Install a skill from an uploaded archive (zip/tar/tar.gz/tgz/rar)."""
    from sandbox.manager import sandbox_manager
    from skill.archive import (
        SKILL_ARCHIVE_MAX_COMPRESSED_BYTES,
        SkillArchiveValidationError,
    )
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        file_bytes = await file.read(SKILL_ARCHIVE_MAX_COMPRESSED_BYTES + 1)
        if len(file_bytes) > SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Skill archive exceeds the compressed size limit",
            )
        return await client.upload_skill_archive(file_bytes, file.filename or "archive.zip", name)
    except HTTPException:
        raise
    except SkillArchiveValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/catalog")
async def get_catalog(current_user: dict = Depends(get_current_user)):
    """The skill store's catalogue, annotated with what is already installed.

    Installed state is resolved here rather than in the browser: the store and
    the "mine" tab would otherwise each derive it from two lists and drift.
    """
    from skill.catalog import load_catalog
    from sandbox.manager import sandbox_manager

    user_id = current_user["user_id"]
    catalog = await load_catalog()
    try:
        from skill.user_library import list_published_catalog_entries

        catalog["skills"].extend(await list_published_catalog_entries())
    except RuntimeError:
        pass

    installed_skills: set[str] = set()
    installed_mcp: set[str] = set()
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            for s in await client.list_skills() or []:
                if s.get("name"):
                    installed_skills.add(s["name"])
                if s.get("install_dir"):
                    installed_skills.add(s["install_dir"])
            for m in await client.list_mcp_servers() or []:
                if m.get("name"):
                    installed_mcp.add(m["name"])
    except Exception:
        # An unreachable sandbox still leaves a browsable store; entries just
        # cannot say whether they are installed yet.
        pass

    for entry in catalog["mcp"]:
        entry["installed"] = entry["name"] in installed_mcp
    for entry in catalog["skills"]:
        entry["installed"] = (
            entry["name"] in installed_skills
            or entry.get("install", {}).get("name") in installed_skills
        )
        # Tell the browser which dependencies are still missing so the install
        # dialog can offer them, rather than making it join two lists itself.
        entry["missing_mcp"] = [
            dep for dep in entry.get("requires_mcp", []) if dep not in installed_mcp
        ]

    return catalog


class InstallCatalogBody(BaseModel):
    id: str
    kind: str = "skill"
    #: Catalogue MCP ids to install alongside a skill.
    with_mcp: list[str] = []
    #: Values for entries that declare required_env, keyed by server id.
    env: dict[str, dict[str, str]] = {}


@router.post("/catalog/install")
async def install_from_catalog(
    body: InstallCatalogBody, current_user: dict = Depends(get_current_user),
):
    """Install one catalogue entry, plus any MCP servers it was asked to bring.

    Dependencies install first: a skill whose server is missing loads fine and
    then fails at its first tool call, which reads as a broken skill rather
    than a missing dependency.
    """
    from skill.catalog import catalog_index, load_catalog
    from sandbox.manager import sandbox_manager

    user_id = current_user["user_id"]
    # Resolve the install from the exact same validated built-in + operator
    # overlay source that GET /catalog renders. Accepting only the opaque id in
    # the request keeps clone/content/MCP config out of user-controlled input.
    index = catalog_index(await load_catalog())

    community_row: dict | None = None
    entry: dict | None = None
    if body.kind == "skill" and body.id.startswith("community:"):
        from skill.user_library import get_published_skill

        community_row = await get_published_skill(body.id, include_archive=True)
        if not community_row:
            raise HTTPException(status_code=404, detail="Published skill not found")
    else:
        entry = index.get(f"{body.kind}:{body.id}")
        if not entry:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown catalog entry: {body.kind}:{body.id}",
            )

    client = await sandbox_manager.get_client_any(user_id=user_id)
    if not client:
        raise HTTPException(status_code=503, detail="No sandbox available")

    installed: list[dict] = []

    async def install_mcp(mcp_entry: dict) -> dict:
        config = {k: v for k, v in mcp_entry["config"].items()}
        supplied = body.env.get(mcp_entry["id"], {})
        if supplied:
            config["env"] = {**(config.get("env") or {}), **supplied}
        await client.add_mcp_server(name=mcp_entry["name"], config=config)
        # Connect immediately: a configured-but-unconnected server contributes
        # no tools, so the skill that needed it is still broken.
        try:
            await client.connect_mcp(mcp_entry["name"])
            status = "connected"
            error = None
        except Exception as e:
            status = "error"
            error = str(e)
        return {"kind": "mcp", "id": mcp_entry["id"], "name": mcp_entry["name"],
                "status": status, "error": error}

    # Dependencies install before either a built-in or community skill.  The
    # V2 install dialog submits catalogue MCP ids in ``with_mcp`` for both.
    for dep_id in body.with_mcp:
        dep = index.get(f"mcp:{dep_id}")
        if dep:
            installed.append(await install_mcp(dep))

    # Community entries are immutable database snapshots, not URLs supplied by
    # their authors. Resolve by opaque id and install the reviewed ZIP directly
    # into this user's sandbox.
    if body.kind == "skill" and body.id.startswith("community:"):
        from skill.user_library import (
            annotate_installed_skills,
            record_community_installation,
        )

        row = community_row
        assert row is not None

        existing = await client.list_skills() or []
        conflict = next(
            (
                item
                for item in existing
                if (
                    item.get("install_dir") == row["install_dir"]
                    or item.get("name") == row["name"]
                )
            ),
            None,
        )
        if conflict:
            # list_skills is already scoped to the sandbox. Older scanners did
            # not emit source, so normalize that omission before provenance
            # annotation instead of turning an exact retry into a false 409.
            annotated = await annotate_installed_skills(
                user_id,
                [{**conflict, "source": conflict.get("source") or "container"}],
            )
            provenance = annotated[0] if annotated else {}
            if (
                provenance.get("category") == "store"
                and provenance.get("catalog_id") == body.id
            ):
                installed.append({
                    "kind": "skill",
                    "id": body.id,
                    "name": row["name"],
                    "status": "installed",
                })
                return {"ok": True, "installed": installed}
            raise HTTPException(
                status_code=409,
                detail=f"A skill named '{row['name']}' is already installed; remove it before installing this store copy.",
            )

        result = await client.upload_skill_archive(
            row["archive_data"],
            f"{row['install_dir']}.zip",
            row["install_dir"],
        )
        reported_dir = result.get("install_dir") if isinstance(result, dict) else None
        installed_dir = (
            reported_dir
            if isinstance(reported_dir, str) and reported_dir
            else row["install_dir"]
        )
        try:
            await record_community_installation(
                user_id=user_id,
                user_skill_id=row["id"],
                name=row["name"],
                install_dir=installed_dir,
            )
        except Exception as exc:
            rollback_error = None
            try:
                await client.uninstall_skill(installed_dir)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            if rollback_error:
                detail = (
                    "The skill archive was uploaded, but store provenance could not be "
                    f"recorded ({exc}). Rollback of '{installed_dir}' also failed: "
                    f"{rollback_error}"
                )
            else:
                detail = (
                    "The skill archive was uploaded, but store provenance could not be "
                    f"recorded ({exc}). The uploaded package '{installed_dir}' was rolled back."
                )
            raise HTTPException(status_code=500, detail=detail) from exc
        installed.append({
            "kind": "skill",
            "id": body.id,
            "name": row["name"],
            "status": "installed",
        })
        return {"ok": True, "installed": installed}

    assert entry is not None

    if body.kind == "mcp":
        installed.append(await install_mcp(entry))
    else:
        spec = entry.get("install", {})
        target_name = spec.get("name") or entry["name"]
        existing = await client.list_skills() or []
        conflict = next(
            (
                item
                for item in existing
                if item.get("install_dir") == target_name
                or item.get("name") == target_name
            ),
            None,
        )
        if conflict:
            # Built-in/operator catalogues have no immutable per-user install
            # record comparable to community provenance. Never overwrite an
            # unproven live package merely because it shares the catalogue slug.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A skill named '{target_name}' is already installed; remove it "
                    "before installing this catalog copy."
                ),
            )
        result = await client.install_skill(
            url=spec.get("url"), name=target_name, content=spec.get("content"),
        )
        installed.append({"kind": "skill", "id": entry["id"],
                          "name": result.get("name") or entry["name"], "status": "installed"})

    return {"ok": True, "installed": installed}


@router.get("/command")
async def list_commands():
    """List available slash commands."""
    try:
        from command.command import list_commands
        return await list_commands()
    except ImportError:
        return []


@router.get("/mcp")
async def get_mcp_status(current_user: dict = Depends(get_current_user)):
    """Get MCP server status (from container)."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            return await client.list_mcp_servers()
    except Exception:
        pass
    return []


@router.post("/mcp")
async def add_mcp_server(body: AddMcpServerBody, current_user: dict = Depends(get_current_user)):
    """Add a new MCP server configuration to the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        config = body.model_dump(exclude={"name"}, exclude_none=True)
        return await client.add_mcp_server(name=body.name, config=config)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/mcp/{name}")
async def remove_mcp_server(name: str, current_user: dict = Depends(get_current_user)):
    """Remove an MCP server from the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        return await client.remove_mcp_server(name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mcp/{name}/connect")
async def connect_mcp(name: str, current_user: dict = Depends(get_current_user)):
    """Connect to an MCP server in the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        return await client.connect_mcp(name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mcp/{name}/disconnect")
async def disconnect_mcp(name: str, current_user: dict = Depends(get_current_user)):
    """Disconnect from an MCP server in the container."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        return await client.disconnect_mcp(name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mcp/{name}/refresh")
async def refresh_mcp(name: str, current_user: dict = Depends(get_current_user)):
    """Refresh tools/resources/prompts for a connected MCP server."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        result = await client.refresh_mcp_server(name)
        if result.get("tools_changed"):
            from bus import bus
            bus.publish("mcp.tools_changed", {"userId": user_id, "server": name, "tools": result.get("tools", 0)})
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mcp/resources")
async def list_mcp_resources(current_user: dict = Depends(get_current_user)):
    """List all MCP resources from connected servers."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            return await client.list_mcp_resources()
    except Exception:
        pass
    return []


class ReadResourceBody(BaseModel):
    server: str
    uri: str


@router.post("/mcp/resources/read")
async def read_mcp_resource(body: ReadResourceBody, current_user: dict = Depends(get_current_user)):
    """Read a specific MCP resource."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        return await client.read_mcp_resource(body.server, body.uri)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mcp/prompts")
async def list_mcp_prompts(current_user: dict = Depends(get_current_user)):
    """List all MCP prompts from connected servers."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if client:
            return await client.list_mcp_prompts()
    except Exception:
        pass
    return []


class GetPromptBody(BaseModel):
    server: str
    name: str
    arguments: dict | None = None


@router.post("/mcp/prompts/get")
async def get_mcp_prompt(body: GetPromptBody, current_user: dict = Depends(get_current_user)):
    """Get a specific MCP prompt."""
    from sandbox.manager import sandbox_manager
    user_id = current_user["user_id"]
    try:
        client = await sandbox_manager.get_client_any(user_id=user_id)
        if not client:
            raise HTTPException(status_code=503, detail="No sandbox available")
        return await client.get_mcp_prompt(body.server, body.name, body.arguments)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── MCP OAuth (F8) ── PKCE + Dynamic Registration + Auto-Discovery ──

# In-memory state for pending OAuth flows
_oauth_pending: dict[str, dict] = {}  # state -> {server_name, user_id, code_verifier, oauth_config}


class McpOAuthStartBody(BaseModel):
    server_name: str


@router.post("/mcp/oauth/start")
async def start_mcp_oauth(
    body: McpOAuthStartBody,
    current_user: dict = Depends(get_current_user),
):
    """Initiate OAuth flow for an MCP server. Returns the authorize URL.

    Supports:
    - Manual OAuth config (client_id, client_secret, etc.)
    - Auto-discovery via /.well-known/oauth-authorization-server (RFC 8414)
    - Dynamic client registration (RFC 7591) if discovery reveals registration_endpoint
    - PKCE (RFC 7636) for enhanced security
    """
    from core.config import get_config
    from core.identifier import generate_id
    from mcp.oauth_provider import (
        OAuthConfig, build_authorize_url, generate_pkce,
        discover_oauth_metadata, dynamic_register,
    )

    config = get_config()
    mcp_config = config.mcp.get(body.server_name)
    if not mcp_config:
        raise HTTPException(404, f"MCP server '{body.server_name}' not configured")

    redirect_uri = f"http://localhost:{config.port}/api/agent/mcp/oauth/callback"
    oauth_cfg = getattr(mcp_config, "oauth", None)

    # If no OAuth config but server has a URL, try auto-discovery
    if (not oauth_cfg or not isinstance(oauth_cfg, dict)) and mcp_config.url:
        metadata = await discover_oauth_metadata(mcp_config.url)
        if metadata and "authorization_endpoint" in metadata:
            reg_endpoint = metadata.get("registration_endpoint")
            if reg_endpoint:
                # Dynamic client registration (RFC 7591)
                reg_result = await dynamic_register(reg_endpoint, redirect_uri)
                oauth_cfg = {
                    "client_id": reg_result["client_id"],
                    "client_secret": reg_result.get("client_secret", ""),
                    "authorize_url": metadata["authorization_endpoint"],
                    "token_url": metadata["token_endpoint"],
                    "scope": " ".join(metadata.get("scopes_supported", [])),
                }
            else:
                raise HTTPException(400,
                    f"Server requires OAuth but no client_id configured and no registration endpoint available")
        elif not metadata:
            raise HTTPException(400, f"MCP server '{body.server_name}' has no OAuth configuration")

    if not oauth_cfg or not isinstance(oauth_cfg, dict):
        raise HTTPException(400, f"MCP server '{body.server_name}' has no OAuth configuration")

    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce()

    state = generate_id()
    _oauth_pending[state] = {
        "server_name": body.server_name,
        "user_id": current_user["user_id"],
        "code_verifier": code_verifier,
        "oauth_config": oauth_cfg,  # Store for callback (may be from dynamic registration)
    }

    oa = OAuthConfig(
        client_id=oauth_cfg["client_id"],
        client_secret=oauth_cfg.get("client_secret", ""),
        authorize_url=oauth_cfg["authorize_url"],
        token_url=oauth_cfg["token_url"],
        redirect_uri=oauth_cfg.get("redirect_uri", redirect_uri),
        scope=oauth_cfg.get("scope", ""),
    )
    url = build_authorize_url(oa, state, code_challenge=code_challenge)
    return {"authorize_url": url, "state": state}


@router.get("/mcp/oauth/callback")
async def mcp_oauth_callback(code: str, state: str):
    """Handle OAuth callback, exchange code for token (with PKCE verifier)."""
    from core.config import get_config
    from mcp.oauth_provider import OAuthConfig, exchange_code

    pending = _oauth_pending.pop(state, None)
    if not pending:
        raise HTTPException(400, "Invalid or expired OAuth state")

    server_name = pending["server_name"]
    user_id = pending["user_id"]
    code_verifier = pending.get("code_verifier")
    oauth_cfg = pending.get("oauth_config")

    config = get_config()

    # Use stored oauth_cfg (may be from dynamic registration)
    if not oauth_cfg:
        mcp_config = config.mcp.get(server_name)
        if not mcp_config:
            raise HTTPException(404, f"MCP server '{server_name}' not found")
        oauth_cfg = getattr(mcp_config, "oauth", None) or {}

    redirect_uri = f"http://localhost:{config.port}/api/agent/mcp/oauth/callback"
    oa = OAuthConfig(
        client_id=oauth_cfg["client_id"],
        client_secret=oauth_cfg.get("client_secret", ""),
        authorize_url=oauth_cfg["authorize_url"],
        token_url=oauth_cfg["token_url"],
        redirect_uri=oauth_cfg.get("redirect_uri", redirect_uri),
        scope=oauth_cfg.get("scope", ""),
    )

    token = await exchange_code(oa, code, server_name, user_id, code_verifier=code_verifier)

    try:
        from bus.bus import publish_toast
        publish_toast(user_id, "success", f"MCP server '{server_name}' authorized via OAuth")
    except Exception:
        pass

    return {
        "ok": True,
        "server_name": server_name,
        "message": "OAuth authorization successful. You can close this window.",
    }
