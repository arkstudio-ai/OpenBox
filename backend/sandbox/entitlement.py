"""Authoritative sandbox access, independent of credit-meter shadow/off mode."""
from billing.service import now
from billing.subscriptions import active_subscription, utc
from core.config import get_config
from db.base import get_db_session


class SandboxSubscriptionRequired(PermissionError):
    code = "SANDBOX_SUBSCRIPTION_REQUIRED"
    detail = ("当前没有有效的付费套餐，无法使用云桌面执行任务。积分余额不能代替套餐权限；"
              "请在账单中开通或续订套餐后继续原任务。普通文字对话仍可使用。")

    def __init__(self):
        super().__init__(self.detail)
        self.payload = {"state": "subscription_required", "code": self.code,
                        "detail": self.detail, "retryable": False}


def subscription_sandbox_enabled() -> bool:
    config = get_config()
    return config.sandbox_provider == "wuying" and config.wuying_routing == "per_desktop"


async def sandbox_access_status(db, workspace_id: str) -> dict:
    """Read current execution eligibility without provisioning or granting credits."""
    required = subscription_sandbox_enabled()
    sub = await active_subscription(db, workspace_id, now()) if required and workspace_id else None
    allowed = not required or (sub is not None and sub.plan_id != "free")
    return {"allowed": allowed, "subscription_required": required,
            "code": None if allowed else SandboxSubscriptionRequired.code,
            "message": None if allowed else SandboxSubscriptionRequired.detail,
            "plan_id": sub.plan_id if sub is not None else None,
            "expires_at": utc(sub.ends_at).isoformat() if sub is not None else None}


async def require_sandbox_subscription(workspace_id: str):
    """No cache: a retained client/URL must not outlive the paid subscription."""
    async with get_db_session() as db:
        sub = await active_subscription(db, workspace_id, now()) if workspace_id else None
        if sub is None or sub.plan_id == "free":
            raise SandboxSubscriptionRequired()
        return sub


async def watch_sandbox_subscription(workspace_id: str, interval: float = 5):
    """A WebSocket watchdog; callers cancel both pumps when this raises."""
    import asyncio
    while True:
        await require_sandbox_subscription(workspace_id)
        await asyncio.sleep(interval)
