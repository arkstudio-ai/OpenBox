"""Audited CRUD and bounded multi-ZIP import for super administrators."""

import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from audit import record
from auth.middleware import require_admin
from skill import catalog_admin
from skill.package_validation import MAX_ZIP_BYTES, validate_zip

router = APIRouter(dependencies=[Depends(require_admin)])


class CreateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="skill", pattern="^(skill|mcp)$")
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    icon: str = Field(default="", max_length=2048)
    content: str = Field(default="", max_length=65536)
    config: dict = Field(default_factory=dict, max_length=20)
    listing: str = Field(default="delisted", pattern="^(listed|delisted)$")


class EditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    icon: str | None = Field(default=None, max_length=2048)
    content: str | None = Field(default=None, max_length=65536)
    config: dict | None = Field(default=None, max_length=20)
    expected_revision: int = Field(ge=0)


class DeleteEntries(BaseModel):
    catalog_ids: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=1000)


def error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, detail=str(exc))
    if isinstance(exc, (FileExistsError, IntegrityError)):
        return HTTPException(
            409, detail="Entry already exists or changed; refresh and retry"
        )
    return HTTPException(422, detail=str(exc))


@router.post("/store")
async def create(
    body: CreateEntry, request: Request, admin: dict = Depends(require_admin)
):
    try:
        result = await catalog_admin.create_entry(body.model_dump(), admin["user_id"])
    except (ValueError, FileExistsError, IntegrityError) as exc:
        raise error(exc) from exc
    await record(
        admin["user_id"],
        None,
        "admin.skill.create",
        "catalog_entry",
        result["catalog_id"],
        {"kind": body.kind},
        request,
    )
    return result


@router.post("/store/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    admin: dict = Depends(require_admin),
):
    """Independent results per ZIP; never execute package hooks during import."""
    if len(files) > 20:
        for file in files:
            await file.close()
        raise HTTPException(422, detail="Select at most 20 ZIP files per batch")
    results = []
    total = 0
    for file in files:
        filename = (file.filename or "archive.zip").rsplit("/", 1)[-1][:255]
        try:
            if not filename.lower().endswith(".zip"):
                raise ValueError("Only ZIP files are accepted")
            data = await file.read(MAX_ZIP_BYTES + 1)
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise ValueError(
                    "Batch exceeds 128 MB; upload remaining files in a new batch"
                )
            metadata = await asyncio.to_thread(validate_zip, data)
            result = await catalog_admin.create_entry(
                metadata, admin["user_id"], archive=data
            )
            await record(
                admin["user_id"],
                None,
                "admin.skill.upload",
                "catalog_entry",
                result["catalog_id"],
                {"sha256": metadata["sha256"], "size": len(data)},
                request,
            )
            results.append({"filename": filename, "ok": True, **result})
        except (ValueError, FileExistsError, IntegrityError) as exc:
            failure = error(exc)
            results.append(
                {
                    "filename": filename,
                    "ok": False,
                    "status": failure.status_code,
                    "error": failure.detail,
                }
            )
        finally:
            await file.close()
    return {"items": results}


@router.get("/store/{catalog_id}")
async def get_entry(
    catalog_id: str, request: Request, admin: dict = Depends(require_admin)
):
    try:
        result = await catalog_admin.detail(catalog_id)
    except (ValueError, LookupError) as exc:
        raise error(exc) from exc
    await record(
        admin["user_id"],
        result.get("workspace_id"),
        "admin.skill.view_content",
        "catalog_entry",
        catalog_id,
        None,
        request,
    )
    return result


@router.patch("/store/{catalog_id}")
async def edit(
    catalog_id: str,
    body: EditEntry,
    request: Request,
    admin: dict = Depends(require_admin),
):
    values = body.model_dump(exclude_none=True, exclude={"expected_revision"})
    try:
        result = await catalog_admin.update_entry(
            catalog_id, values, admin["user_id"], body.expected_revision
        )
    except (ValueError, LookupError, FileExistsError, IntegrityError) as exc:
        raise error(exc) from exc
    await record(
        admin["user_id"],
        None,
        "admin.skill.edit",
        "catalog_entry",
        catalog_id,
        {"fields": sorted(values), "revision": result["revision"]},
        request,
    )
    return result


@router.post("/store/batch-delete")
async def delete_batch(
    body: DeleteEntries, request: Request, admin: dict = Depends(require_admin)
):
    if not body.reason.strip():
        raise HTTPException(422, detail="A deletion reason is required")
    results = []
    for key in dict.fromkeys(body.catalog_ids):
        try:
            result = await catalog_admin.delete_entry(key, admin["user_id"])
            if result["changed"]:
                await record(
                    admin["user_id"],
                    result.get("workspace_id"),
                    "admin.skill.delete",
                    "catalog_entry",
                    key,
                    {"reason": body.reason, "installed_copies": "unchanged"},
                    request,
                )
            results.append({"catalog_id": key, "ok": True})
        except (ValueError, LookupError, IntegrityError) as exc:
            results.append({"catalog_id": key, "ok": False, "error": error(exc).detail})
    return {"items": results}


@router.post("/store/{catalog_id}/restore")
async def restore(
    catalog_id: str, request: Request, admin: dict = Depends(require_admin)
):
    try:
        result = await catalog_admin.restore_entry(catalog_id, admin["user_id"])
    except LookupError as exc:
        raise error(exc) from exc
    await record(
        admin["user_id"],
        None,
        "admin.skill.restore",
        "catalog_entry",
        catalog_id,
        None,
        request,
    )
    return result
