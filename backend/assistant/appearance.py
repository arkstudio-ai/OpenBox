"""Account-owned companion appearance. Visual preferences never enter model prompts."""
import re
import struct

from core.oss import OssNotConfigured, get_oss
from db.repository.preference_repo import PgPreferenceRepo

KEY = "assistant_companion"
PRESETS = ("orb", "sprout", "cat", "custom")
MAX_IMAGE_BYTES = 1024 * 1024


def image_key(user_id: str, value) -> str | None:
    """Only this account's immutable avatar objects can ever be signed."""
    prefix = f"assistant-avatars/{user_id}/"
    if isinstance(value, str) and value.startswith(prefix) and re.fullmatch(r"[a-f0-9]{64}\.png", value[len(prefix):]):
        return value
    return None


def png_dimensions(data: bytes) -> tuple[int, int]:
    # The browser normalizes decoded images to a small PNG. Do not accept SVG,
    # filenames, URLs, unbounded dimensions, or an uploaded MIME declaration.
    if not 45 <= len(data) <= MAX_IMAGE_BYTES or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82":
        raise ValueError("ASSISTANT_AVATAR_INVALID")
    width, height = struct.unpack(">II", data[16:24])
    if not 0 < width <= 1024 or not 0 < height <= 1024:
        raise ValueError("ASSISTANT_AVATAR_INVALID")
    return width, height


async def load(user_id: str) -> dict:
    extra = ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}
    raw = extra.get(KEY)
    value = raw if isinstance(raw, dict) else {}
    return {
        "enabled": value.get("enabled") is not False,
        "preset": value.get("preset") if value.get("preset") in PRESETS else "orb",
        "image_key": image_key(user_id, value.get("image_key")),
    }


async def view(user_id: str) -> dict:
    value = await load(user_id)
    key = value.pop("image_key")
    url = None
    if key:
        try:
            url = get_oss().presign_get(key)
        except OssNotConfigured:
            pass
    return {**value, "has_image": bool(key), "image_url": url}


async def save(user_id: str, patch: dict) -> None:
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    # Merge under the same lock as the other account preferences, so changing
    # a character and toggling the entry cannot overwrite each other.
    async with get_db_session() as db:
        row = await locked_preference(db, user_id)
        extra = row.extra or {}
        previous = extra.get(KEY) if isinstance(extra.get(KEY), dict) else {}
        if patch.get("preset") == "custom" and not image_key(user_id, patch.get("image_key") or previous.get("image_key")):
            raise ValueError("ASSISTANT_AVATAR_MISSING")
        row.extra = {**extra, KEY: {**previous, **patch}}
