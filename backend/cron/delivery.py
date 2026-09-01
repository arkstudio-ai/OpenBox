"""Delivery abstraction layer — dispatch cron results to external channels.

Current: only "none" mode (results stay in DB + inject to session).
Extensible: webhook, channel (Telegram/Slack/Feishu etc.)
"""
from __future__ import annotations

from pydantic import BaseModel

from core.log import create_logger

log = create_logger("cron.delivery")


class DeliveryResult(BaseModel):
    success: bool
    error: str | None = None
    terminal: bool = False
    retry_delay_seconds: int | None = None


async def dispatch_delivery(
    delivery_config: dict,
    job_name: str,
    job_id: str,
    status: str,
    summary_text: str | None,
    duration_ms: int,
    *,
    delivery_id: str | None = None,
    occurred_at: str | None = None,
) -> DeliveryResult:
    """Dispatch cron result to configured delivery target.

    Currently supports:
    - "none": No external delivery (default)
    - "webhook": HTTP POST to configured URL (future)
    - "channel": Push to messaging channel (future)
    """
    mode = delivery_config.get("mode", "none") if delivery_config else "none"

    if mode == "none":
        return DeliveryResult(success=True)

    if mode == "webhook":
        return await _deliver_webhook(
            delivery_config,
            job_name,
            job_id,
            status,
            summary_text,
            duration_ms,
            delivery_id=delivery_id,
            occurred_at=occurred_at,
        )

    if mode == "channel":
        # Future: Telegram, Slack, Feishu, etc.
        log.warning(f"Channel delivery not yet implemented for job {job_id}")
        return DeliveryResult(
            success=False,
            error="Channel delivery not implemented",
            terminal=True,
        )

    return DeliveryResult(
        success=False,
        error=f"Unknown delivery mode: {mode}",
        terminal=True,
    )


async def _deliver_webhook(
    config: dict,
    job_name: str,
    job_id: str,
    status: str,
    summary_text: str | None,
    duration_ms: int,
    *,
    delivery_id: str | None = None,
    occurred_at: str | None = None,
) -> DeliveryResult:
    """Deliver one at-least-once webhook attempt.

    Receivers should persist and deduplicate ``delivery_id`` (also sent in
    ``X-OpenBox-Delivery-ID``) before applying their own side effect.
    """
    url = config.get("webhook_url")
    if not url:
        return DeliveryResult(
            success=False,
            error="No webhook_url configured",
            terminal=True,
        )

    # Re-check at send time: creation-time validation cannot stop a DNS record
    # that later starts resolving to something inside our network.
    try:
        from cron.validation import check_webhook_url
        check_webhook_url(url)
    except ValueError as e:
        return DeliveryResult(success=False, error=str(e), terminal=True)

    token = config.get("webhook_token")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if delivery_id:
        headers["X-OpenBox-Delivery-ID"] = delivery_id
        headers["Idempotency-Key"] = delivery_id

    from datetime import datetime, timezone
    payload = {
        "job_id": job_id,
        "job_name": job_name,
        "status": status,
        "summary": summary_text,
        "duration_ms": duration_ms,
        "timestamp": occurred_at or datetime.now(timezone.utc).isoformat(),
        "delivery_id": delivery_id,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.is_success:
                return DeliveryResult(success=True)
            else:
                status_code = int(resp.status_code)
                terminal = 400 <= status_code < 500 and status_code != 429
                retry_delay = None
                if status_code == 429:
                    try:
                        retry_delay = min(
                            3600,
                            max(0, int(resp.headers.get("Retry-After", ""))),
                        )
                    except (TypeError, ValueError):
                        retry_delay = None
                return DeliveryResult(
                    success=False,
                    error=f"Webhook returned {status_code}",
                    terminal=terminal,
                    retry_delay_seconds=retry_delay,
                )
    except Exception as e:
        return DeliveryResult(success=False, error=str(e))
