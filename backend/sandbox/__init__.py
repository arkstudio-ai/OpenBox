from sandbox.manager import SandboxManager, sandbox_manager
from sandbox.client import SandboxClient
from sandbox.provider import SandboxProvider


_provider_instance: SandboxProvider | None = None


def _create_provider() -> SandboxProvider:
    from core.config import get_config
    config = get_config()
    if config.sandbox_provider != "wuying":
        raise RuntimeError(
            "OpenBox Agent execution requires SANDBOX_PROVIDER=wuying; "
            "Docker Compose is reserved for infrastructure services"
        )
    from sandbox.wuying import WuyingProvider
    return WuyingProvider()


def get_provider() -> SandboxProvider:
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = _create_provider()
    return _provider_instance


class _LazyProviderProxy:
    def __getattr__(self, name):
        return getattr(get_provider(), name)


provider: SandboxProvider = _LazyProviderProxy()  # type: ignore[assignment]
