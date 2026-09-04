"""Loopback-facing infrastructure callbacks."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, Query
from fastapi.responses import PlainTextResponse

from core.config import get_config
from db.repository.cloud_desktop_repo import cloud_desktop_repo

router = APIRouter(prefix="/api/internal", tags=["internal"], include_in_schema=False)


@router.get("/tunnel-keys", response_class=PlainTextResponse)
async def tunnel_keys(
    fingerprint: str = Query(min_length=8, max_length=128),
    x_internal_token: str = Header(default=""),
):
    expected = get_config().internal_api_token
    if not expected or not hmac.compare_digest(x_internal_token, expected):
        return PlainTextResponse("", status_code=403)
    record = await cloud_desktop_repo.get_by_fingerprint(fingerprint)
    if (
        not record
        or record.get("tunnel_state") == "revoked"
        or not record.get("tunnel_pubkey")
        or not record.get("tunnel_port")
        or not record.get("tunnel_bind")
    ):
        return PlainTextResponse("")
    options = (
        'restrict,port-forwarding,'
        f'permitlisten="{record["tunnel_bind"]}:{record["tunnel_port"]}"'
    )
    return PlainTextResponse(f'{options} {record["tunnel_pubkey"]}\n')
