from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory.wiki import exchange, export
from memory.wiki.api import _call, _identity, _no_store
from memory.wiki.organization_api import StrictBody
from wiki_compiler.exchange import ExchangeError, MAX_UPLOAD

router = APIRouter(prefix="/api/memory-wiki/exchange", tags=["memory-wiki"],
                   dependencies=[Depends(get_workspace), Depends(_no_store)])


async def call(operation):
    try:
        return await _call(operation)
    except ExchangeError as exc:
        raise HTTPException(422, {"code": str(exc).upper(), "message": str(exc)}) from exc


class DecisionBody(StrictBody):
    expected_revision: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    action: Literal["approve", "reject", "rename"]
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    acknowledge_warnings: bool = False


@router.post("/preview")
async def preview(file: UploadFile = File(...), project_id: str | None = Form(None), user: dict = Depends(get_current_user)):
    data = await file.read(MAX_UPLOAD + 1)
    return await call(exchange.preview(**_identity(user), project_id=project_id or None, data=data))


@router.get("/bundles")
async def bundles(project_id: str | None = None, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await call(exchange.list_bundles(**_identity(user), project_id=project_id, offset=offset))


@router.get("/bundles/{bundle_id}")
async def bundle(bundle_id: str, user: dict = Depends(get_current_user)):
    return await call(exchange.detail(**_identity(user), bundle_id=bundle_id))


@router.post("/documents/{document_id}/decision")
async def decision(document_id: str, body: DecisionBody, user: dict = Depends(get_current_user)):
    return await call(exchange.decide(**_identity(user), document_id=document_id, **body.model_dump()))


@router.get("/export")
async def download(format: Literal["okf", "json", "jsonld", "graphml", "marp", "llms"] = "okf",
                   project_id: str | None = None, user: dict = Depends(get_current_user)):
    data, media, name = await call(export.export(**_identity(user), project_id=project_id, format=format))
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                    "Cache-Control": "no-store"})
