"""Sample media named in the Light JSON files (gallery samples, first-video templates).

A file may point at a private bucket object as ``oss:<key>``; whatever serves
it to the App swaps that for a signed GET link. Plain ``https://`` links pass
through. Signing never fails a response: without OSS the field is ``None``
and the App shows its tinted tile.
"""
from __future__ import annotations

from core.log import create_logger

log = create_logger(__name__)

OSS_PREFIX = "oss:"
#: Validation pattern for a media field in the JSON files.
MEDIA_PATTERN = r"^(https://|oss:)\S+$"
#: The App refetches the entries and templates on every visit; half a day covers a long session.
LINK_SECONDS = 12 * 3600


def sign_media(value: str | None) -> str | None:
    if not value or not value.startswith(OSS_PREFIX):
        return value
    key = value[len(OSS_PREFIX):]
    try:
        from core.oss import get_oss
        return get_oss().presign_get(key, LINK_SECONDS)
    except Exception as exc:
        log.warning("light media not signed key=%s error=%s", key, type(exc).__name__)
        return None
