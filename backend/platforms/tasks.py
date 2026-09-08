"""Internal-task wiring for the authorization centre."""
from cron import internal_tasks
from platforms.service import refresh_due

#: Six hours: a 3-day refresh lead needs nothing tighter, and the platform
#: rate-limits token calls.
KEEP_ALIVE_INTERVAL_SEC = 6 * 60 * 60


def register_platform_tasks() -> None:
    from platforms.desktop.tasks import INTERVAL_SEC as DESKTOP_PROBE_INTERVAL_SEC, run as run_desktop_probe

    internal_tasks.register("platform_token_keepalive", KEEP_ALIVE_INTERVAL_SEC, refresh_due)
    internal_tasks.register("desktop_login_probe", DESKTOP_PROBE_INTERVAL_SEC, run_desktop_probe)
