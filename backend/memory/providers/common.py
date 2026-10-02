import os


class MemoryProviderError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def bailian_key() -> str:
    key = os.getenv("MEMORY_BAILIAN_API_KEY") or os.getenv("BAILIAN_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise MemoryProviderError("provider_not_configured")
    return key


def jev_key() -> str:
    key = os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_KEY")
    if not key:
        raise MemoryProviderError("provider_not_configured")
    return key


def response_json(response):
    if response.status_code >= 400:
        code = "rate_limited" if response.status_code == 429 else "authentication_error" if response.status_code in (401, 403) else "provider_error"
        raise MemoryProviderError(code, response.status_code)
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise MemoryProviderError("invalid_response") from None
    if not isinstance(data, dict):
        raise MemoryProviderError("invalid_response")
    return data
