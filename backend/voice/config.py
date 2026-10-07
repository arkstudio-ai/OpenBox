"""Voice-call settings for one call: enablement, key, endpoint and proxy plan."""
import os
import re
from urllib.parse import quote

from core.config import VoiceConfig, get_config


def voice_config() -> VoiceConfig:
    return get_config().voice


def api_key(config: VoiceConfig) -> str:
    """The configured key, else DASHSCOPE_API_KEY, the same variable the memory providers read."""
    return config.api_key or os.getenv("DASHSCOPE_API_KEY", "")


def enabled(config: VoiceConfig | None = None) -> bool:
    """Both switched on and able to authenticate; clients hide the button otherwise."""
    config = config or voice_config()
    return bool(config.enabled and api_key(config))


def realtime_url(config: VoiceConfig) -> str:
    endpoint = config.endpoint
    if config.workspace_id:
        # A configuration value, but it becomes a host name: refuse anything else.
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", config.workspace_id):
            raise ValueError("voice.workspace_id must be a business-space ID")
        endpoint = f"wss://{config.workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    return f"{endpoint}?model={quote(config.model, safe='')}"


def proxy_plan(config: VoiceConfig) -> list[bool | None]:
    """One entry per attempt: True uses the environment proxy, None connects directly.

    Measured 2026-10-07: through the environment proxy 5/5 handshakes took
    0.14-0.24 s; direct 4/5 succeeded and one hung for 12 s. So "env" starts
    with the proxy and alternates, which also survives a broken proxy.
    """
    if config.proxy == "none":
        return [None] * config.connect_attempts
    return [True if attempt % 2 == 0 else None for attempt in range(config.connect_attempts)]
