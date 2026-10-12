"""Per-user image channel; concrete model and credentials remain server owned."""
from db.base import get_db_session
from db.models.preference import UserPreference
from sqlalchemy import select

KEY = "image_generation_provider"


def options(config):
    provider = config.provider.get(config.image_generation.runninghub_provider)
    from tool.image_runninghub import MODELS
    available = bool(provider and provider.api_key and provider.base_url
                     and config.image_generation.runninghub_model in MODELS)
    return [{"id": "default", "name": "Default", "available": True},
            {"id": "runninghub", "name": "RunningHub", "available": available}]


async def selected(user_id):
    async with get_db_session() as db:
        extra = await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id))
    value = (extra or {}).get(KEY, "default")
    if value not in {"default", "runninghub"}:
        raise ValueError("Invalid image generation preference")
    return value
