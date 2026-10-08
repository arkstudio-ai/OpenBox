"""One consumer upload action; parsing, indexing and Wiki conversion are automatic."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory.documents import service
from memory.documents.parser import DocumentError, MAX_BYTES
from memory.wiki.api import _call, _identity, _no_store

router = APIRouter(prefix="/api/memory-documents", tags=["memory-documents"],
    dependencies=[Depends(get_workspace), Depends(_no_store)])


async def call(operation):
    try:
        return await _call(operation)
    except DocumentError as exc:
        raise HTTPException(422, {"code": str(exc).upper(), "message": str(exc)}) from None


@router.post("")
async def upload(file: UploadFile = File(...), project_id: str | None = Form(default=None),
                 user: dict = Depends(get_current_user)):
    try:
        data = await file.read(MAX_BYTES + 1)
        return await call(service.submit(**_identity(user), project_id=project_id or None,
            filename=file.filename, data=data))
    finally:
        await file.close()


@router.get("")
async def listing(project_id: str | None = None, offset: int = Query(default=0, ge=0),
                  user: dict = Depends(get_current_user)):
    return await call(service.list_documents(**_identity(user), project_id=project_id, offset=offset))


@router.post("/{document_id}/retry")
async def retry(document_id: str, user: dict = Depends(get_current_user)):
    return await call(service.retry(**_identity(user), document_id=document_id))


@router.delete("/{document_id}")
async def remove(document_id: str, user: dict = Depends(get_current_user)):
    return await call(service.delete(**_identity(user), document_id=document_id))


@router.get("/{document_id}/original")
async def original(document_id: str, user: dict = Depends(get_current_user)):
    filename, data = await call(service.original(**_identity(user), document_id=document_id))
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
