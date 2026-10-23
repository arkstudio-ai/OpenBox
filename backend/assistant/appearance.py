"""Account-owned companion appearance. Visual preferences never enter model prompts."""
import re
import struct

from core.oss import OssNotConfigured, get_oss
from db.repository.preference_repo import PgPreferenceRepo

KEY = "assistant_companion"
BUILTIN_PRESETS = tuple(f"ip-{number:02d}" for number in range(1, 35))
DEFAULT_PRESET = "ip-01"
# Old accounts and older browser tabs keep working without a data migration.
LEGACY_PRESETS = {"orb": "ip-01", "sprout": "ip-10", "cat": "ip-12"}
PRESETS = (*BUILTIN_PRESETS, *LEGACY_PRESETS, "custom")
# Character voice assignments, including animals; not inferred from an upload.
MALE_PRESETS = frozenset({
    "ip-02", "ip-04", "ip-06", "ip-08", "ip-11", "ip-12", "ip-14",
    "ip-15", "ip-18", "ip-24", "ip-26", "ip-29", "ip-31",
})
ANIMAL_PRESETS = frozenset({
    "ip-08", "ip-10", "ip-11", "ip-12", "ip-13", "ip-14", "ip-21", "ip-29", "ip-31", "ip-33",
})
MAX_IMAGE_BYTES = 1024 * 1024


def normalize_preset(value) -> str:
    if not isinstance(value, str) or value not in PRESETS:
        return DEFAULT_PRESET
    return LEGACY_PRESETS.get(value, value)


def voice_gender(preset) -> str:
    return "male" if normalize_preset(preset) in MALE_PRESETS else "female"


def preset_catalog() -> list[dict]:
    """The picker and call defaults share these character assignments."""
    return [{"id": preset, "category": "pets" if preset in ANIMAL_PRESETS else voice_gender(preset),
             "voice_gender": voice_gender(preset)} for preset in BUILTIN_PRESETS]


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
        "preset": normalize_preset(value.get("preset")),
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
    return {**value, "has_image": bool(key), "image_url": url, "presets": preset_catalog()}


async def save(user_id: str, patch: dict) -> None:
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    if "preset" in patch:
        if patch["preset"] not in PRESETS:
            raise ValueError("ASSISTANT_AVATAR_INVALID")
        patch = {**patch, "preset": normalize_preset(patch["preset"])}
    # Merge under the same lock as the other account preferences, so changing
    # a character and toggling the entry cannot overwrite each other.
    async with get_db_session() as db:
        row = await locked_preference(db, user_id)
        extra = row.extra or {}
        previous = extra.get(KEY) if isinstance(extra.get(KEY), dict) else {}
        if patch.get("preset") == "custom" and not image_key(user_id, patch.get("image_key") or previous.get("image_key")):
            raise ValueError("ASSISTANT_AVATAR_MISSING")
        row.extra = {**extra, KEY: {**previous, **patch}}
