"""Voice-call settings for one call: enablement, key, endpoint and proxy plan."""
import os
import re
from urllib.parse import quote, urlsplit

from core.config import VoiceConfig, get_config


def voice_config() -> VoiceConfig:
    return get_config().voice


def api_key(config: VoiceConfig) -> str:
    """Custom gateways require an explicit key; never leak the official fallback."""
    if config.api_key:
        return config.api_key
    host = (urlsplit(config.endpoint).hostname or "").lower()
    if host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com"):
        return os.getenv("DASHSCOPE_API_KEY", "")
    return ""


def enabled(config: VoiceConfig | None = None) -> bool:
    """Both switched on and able to authenticate; clients hide the button otherwise."""
    config = config or voice_config()
    return bool(config.enabled and api_key(config))


def realtime_url(config: VoiceConfig) -> str:
    endpoint = config.endpoint
    if config.workspace_id:
        host = (urlsplit(endpoint).hostname or "").lower()
        if not (host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com")):
            raise ValueError("voice.workspace_id must be empty when using a gateway")
        # A configuration value, but it becomes a host name: refuse anything else.
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", config.workspace_id):
            raise ValueError("voice.workspace_id must be a business-space ID")
        endpoint = f"wss://{config.workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    return f"{endpoint}?model={quote(config.model, safe='')}"


def proxy_plan(config: VoiceConfig) -> list[bool | None]:
    """One entry per attempt: True uses the environment proxy, None connects directly.

    Measured 2026-10-07: through the environment proxy 5/5 handshakes took
    0.14-0.24 s; direct 4/5 succeeded and one hung for 12 s. Later QA calls
    saw the proxy hang too. "env" therefore uses both routes, and the
    provider races them (the proxy first, direct after a head start).
    """
    if config.proxy == "none":
        return [None] * config.connect_attempts
    return [True if attempt % 2 == 0 else None for attempt in range(config.connect_attempts)]
