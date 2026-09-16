"""Which way a person's videos go to Douyin, per user (设置 → 视频发布).

Two routes exist behind the publish tools:

- ``desktop`` — 创作者后台: `desktop_publish` fills the creator-centre form
  using the login state on the workspace's cloud desktop. Fully automatic,
  no authorization step; internally this is publish mode ``auto``.
- ``api``     — 抖音开放平台 API: `douyin_publish` produces the 开放平台 QR
  posting package and the person taps 发布 in the Douyin app. Internally this
  is publish mode ``package``.

Nothing chosen means "follow the deployment default" (`desktop_publish.default_mode`
in openbox.json). A choice made here outranks the mode a template or the
model asks for — it is the person's own instruction — and is outranked only
by the per-account risk breaker, which nobody but a person can lift.

Stored in the shared per-user preferences row, same as `session/browser_pref.py`.
"""
from db.repository.preference_repo import PgPreferenceRepo

#: Key inside UserPreference.extra.
PREF_KEY = "publish_route"

ROUTES = ("desktop", "api")
_ROUTE_TO_MODE = {"desktop": "auto", "api": "package"}
_MODE_TO_ROUTE = {"auto": "desktop", "package": "api"}


class InvalidPublishRoute(ValueError):
    pass


def route_to_mode(route: str | None) -> str | None:
    """'desktop' → 'auto', 'api' → 'package', None/unknown → None."""
    return _ROUTE_TO_MODE.get(route or "")


def mode_to_route(mode: str) -> str:
    return _MODE_TO_ROUTE.get(mode, "desktop")


async def get_publish_route(user_id: str) -> str | None:
    """The user's stored choice, or None when they never chose."""
    prefs = await PgPreferenceRepo().get(user_id)
    route = ((prefs or {}).get("extra") or {}).get(PREF_KEY)
    return route if route in ROUTES else None


async def set_publish_route(user_id: str, route: str | None) -> str | None:
    """Persist a choice; None (or "") clears it back to the deployment default."""
    route = route or None
    if route is not None and route not in ROUTES:
        raise InvalidPublishRoute(f"publish route must be one of {', '.join(ROUTES)}, got {route!r}")
    # The repository shallow-merges `extra` (a bag other settings share), so a
    # cleared choice is written as null rather than by dropping the key.
    await PgPreferenceRepo().upsert(user_id, extra={PREF_KEY: route})
    return route
