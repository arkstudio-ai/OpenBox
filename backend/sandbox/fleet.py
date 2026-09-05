"""ECD/DB/account snapshots and deterministic fleet reconciliation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select, update

from core.config import get_config
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.fleet import FleetAlert, FleetSnapshot
from sandbox import wuying_ecd


log = create_logger("sandbox.fleet")
SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}
RULE_SOURCES = {
    "ghost": frozenset({"ecd", "db"}),
    "orphan": frozenset({"ecd", "db"}),
    "tag_mismatch": frozenset({"ecd", "db"}),
    "expiring_soon": frozenset({"ecd"}),
    "expired": frozenset({"ecd"}),
    "channel_down": frozenset({"db"}),
    "postpaid_running": frozenset({"ecd", "db"}),
    "prewarm_below_watermark": frozenset({"ecd"}),
    "purchase_blocked": frozenset({"account"}),
    "account_balance_low": frozenset({"account"}),
}
SHARED_DESKTOP_IDS = frozenset({"ecd-4zjxaq5g45dr5qr0i"})


class FleetScopeError(RuntimeError):
    """The environment tag unexpectedly crosses the approved fleet boundary."""


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    resource_type: str
    resource_id: str
    message: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class SourceResult:
    source: str
    ok: bool
    payload: Any = None
    error: str | None = None


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _serializable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _finding(
    rule: str,
    severity: str,
    resource_type: str,
    resource_id: str,
    message: str,
    **detail: Any,
) -> Finding:
    return Finding(rule, severity, resource_type, resource_id, message, detail)


def reconcile(
    ecd: list[dict[str, Any]] | None,
    db: list[dict[str, Any]] | None,
    account: dict[str, Any] | None,
    now: datetime,
    *,
    target_prewarm: int = 5,
    renew_before_days: int = 3,
    channel_down_alert_sec: int = 600,
    auto_purchase: bool = False,
    min_balance_multiple: float = 2,
) -> list[Finding]:
    """Return current findings with no database or network I/O.

    A failed source is represented by ``None``. Rules depending on it are
    skipped; the lifecycle layer consequently neither creates nor resolves
    those alerts.
    """
    now = now.replace(tzinfo=now.tzinfo or timezone.utc).astimezone(timezone.utc)
    findings: list[Finding] = []
    ecd_by_id = {row.get("desktop_id"): row for row in (ecd or []) if row.get("desktop_id")}
    db_by_id = {
        row.get("desktop_id"): row
        for row in (db or [])
        if row.get("desktop_id") and not row.get("is_deleted")
    }

    if ecd is not None and db is not None:
        for desktop_id, local in db_by_id.items():
            remote = ecd_by_id.get(desktop_id)
            if local.get("status") == "running" and (
                remote is None or remote.get("status") in {"Deleted", "Deleting"}
            ):
                findings.append(_finding(
                    "ghost", "critical", "desktop", desktop_id,
                    f"DB marks {desktop_id} running but ECD does not",
                    db_status=local.get("status"), ecd_status=(remote or {}).get("status"),
                ))
        for desktop_id, remote in ecd_by_id.items():
            local = db_by_id.get(desktop_id)
            if local is None:
                findings.append(_finding(
                    "orphan", "warn", "desktop", desktop_id,
                    f"ECD desktop {desktop_id} has no active DB row",
                    tags=remote.get("tags") or {},
                ))
                continue
            tags = remote.get("tags") or {}
            expected_workspace = local.get("workspace_id") or ""
            actual_workspace = tags.get(wuying_ecd.TAG_WORKSPACE, "")
            expected_pool = local.get("pool_state") or "assigned"
            actual_pool = tags.get(wuying_ecd.TAG_POOL, "")
            if expected_workspace != actual_workspace or expected_pool != actual_pool:
                findings.append(_finding(
                    "tag_mismatch", "warn", "desktop", desktop_id,
                    f"ECD tags and DB ownership/state differ for {desktop_id}",
                    expected_workspace=expected_workspace,
                    actual_workspace=actual_workspace,
                    expected_pool=expected_pool,
                    actual_pool=actual_pool,
                ))

    if ecd is not None:
        prewarm = 0
        for remote in ecd:
            desktop_id = remote.get("desktop_id") or "unknown"
            status = remote.get("status")
            tags = remote.get("tags") or {}
            pool_state = tags.get(wuying_ecd.TAG_POOL)
            if pool_state == "prewarm" and status not in {"Expired", "Deleted", "Deleting"}:
                prewarm += 1
            if status == "Expired":
                findings.append(_finding(
                    "expired", "critical", "desktop", desktop_id,
                    f"ECD desktop {desktop_id} is expired", status=status,
                ))
            expires_at = _utc(remote.get("expired_time") or remote.get("expires_at"))
            if (
                expires_at is not None
                and expires_at < now + timedelta(days=renew_before_days)
                and pool_state not in {"retired", "abandon", "reserve"}
            ):
                findings.append(_finding(
                    "expiring_soon", "warn", "desktop", desktop_id,
                    f"ECD desktop {desktop_id} expires soon",
                    expires_at=expires_at.isoformat(), pool_state=pool_state,
                ))
        if prewarm < target_prewarm:
            findings.append(_finding(
                "prewarm_below_watermark", "info" if auto_purchase else "warn",
                "pool", "prewarm", f"Prewarm capacity is {prewarm}/{target_prewarm}",
                current=prewarm, target=target_prewarm,
            ))

    if db is not None:
        for local in db:
            desktop_id = local.get("desktop_id") or local.get("id") or "unknown"
            if local.get("pool_state") == "assigned" and local.get("tunnel_state") == "down":
                down_since = _utc(local.get("last_seen_at") or local.get("updated_at"))
                if down_since is None or (now - down_since).total_seconds() >= channel_down_alert_sec:
                    findings.append(_finding(
                        "channel_down", "warn", "desktop", desktop_id,
                        f"Assigned desktop {desktop_id} channel is down",
                        down_since=down_since.isoformat() if down_since else None,
                    ))

    if ecd is not None and db is not None:
        for desktop_id, remote in ecd_by_id.items():
            local = db_by_id.get(desktop_id)
            if remote.get("charge_type") != "PostPaid" or remote.get("status") != "Running":
                continue
            started_at = _utc((local or {}).get("created_at"))
            if started_at is not None and now - started_at >= timedelta(hours=24):
                findings.append(_finding(
                    "postpaid_running", "warn", "desktop", desktop_id,
                    f"PostPaid desktop {desktop_id} has run for more than 24 hours",
                    created_at=started_at.isoformat(),
                ))

    if account is not None:
        blocked = account.get("purchase_blocked")
        if blocked:
            blocked_detail = dict(blocked) if isinstance(blocked, dict) else {}
            blocked_message = str(blocked_detail.pop("message", blocked))
            findings.append(_finding(
                "purchase_blocked", "warn", "pool", "purchase",
                blocked_message, **blocked_detail,
            ))
        balance = account.get("available_balance")
        unit_price = account.get("unit_price")
        if balance is not None and unit_price is not None:
            required = float(unit_price) * min_balance_multiple
            if float(balance) < required:
                findings.append(_finding(
                    "account_balance_low", "critical", "account", "aliyun",
                    "Alibaba Cloud balance is below the fleet safety floor",
                    available_balance=float(balance), required_balance=required,
                    unit_price=float(unit_price),
                ))

    return sorted(findings, key=lambda item: (item.rule, item.resource_id))


async def _db_snapshot(now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=24)
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(CloudDesktop).where(
                    (CloudDesktop.is_deleted.is_(False))
                    | (CloudDesktop.deleted_at >= cutoff)
                )
            )
        ).scalars().all()
    return [
        {column.name: getattr(row, column.name) for column in row.__table__.columns}
        for row in rows
    ]


async def _capture(source: str, call) -> SourceResult:
    try:
        return SourceResult(source, True, await call())
    except Exception as exc:
        log.warning("Fleet snapshot source failed source=%s error=%s", source, exc)
        return SourceResult(source, False, error=f"{type(exc).__name__}: {exc}"[:4000])


async def _account_snapshot() -> dict[str, Any]:
    balance, price = await wuying_ecd.query_account_balance(), await wuying_ecd.describe_price(
        "PrePaid"
    )
    return {
        "available_balance": balance["available_balance"],
        "currency": balance.get("currency") or price.get("currency") or "CNY",
        "unit_price": price.get("trade_price"),
    }


async def _ecd_snapshot() -> list[dict[str, Any]]:
    rows = await wuying_ecd.list_fleet_desktops()
    allowlist = {
        item.strip()
        for item in get_config().pool_adopt_allowlist.split(",")
        if item.strip()
    }
    for row in rows:
        desktop_id = row.get("desktop_id")
        tags = row.get("tags") or {}
        if desktop_id in SHARED_DESKTOP_IDS:
            raise FleetScopeError(
                f"shared desktop {desktop_id} matched the OpenBox environment tag"
            )
        if tags.get("purpose") == "codex" and desktop_id not in allowlist:
            raise FleetScopeError(
                f"non-allowlisted bossip desktop {desktop_id} matched the OpenBox environment tag"
            )
    return rows


async def persist_alerts(
    findings: Iterable[Finding],
    source_health: dict[str, bool],
    now: datetime,
    *,
    resolve_missing: bool = True,
) -> None:
    current = {(item.rule, item.resource_id): item for item in findings}
    async with get_db_session() as session:
        open_alerts = (
            await session.execute(select(FleetAlert).where(FleetAlert.resolved_at.is_(None)))
        ).scalars().all()
        existing = {(row.rule, row.resource_id): row for row in open_alerts}
        for key, finding in current.items():
            row = existing.get(key)
            if row is None:
                row = FleetAlert(
                    id=ascending("flt"), rule=finding.rule, severity=finding.severity,
                    resource_type=finding.resource_type, resource_id=finding.resource_id,
                    message=finding.message, detail=_serializable(finding.detail),
                    first_seen_at=now, last_seen_at=now,
                )
                session.add(row)
                log_method = log.error if finding.severity == "critical" else log.warning
                log_method("Fleet alert opened rule=%s resource=%s", *key)
            else:
                escalated = SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER.get(row.severity, -1)
                row.severity = finding.severity
                row.resource_type = finding.resource_type
                row.message = finding.message
                row.detail = _serializable(finding.detail)
                row.last_seen_at = now
                if escalated:
                    log.error("Fleet alert escalated rule=%s resource=%s", *key)
        if not resolve_missing:
            return
        # Reconciliation is a three-source observation.  A partially healthy
        # run may update findings it can still prove, but it must never infer
        # that an absent finding has disappeared until every source succeeded.
        all_sources_healthy = all(
            source_health.get(source, False) for source in ("ecd", "db", "account")
        )
        if not all_sources_healthy:
            return
        for key, row in existing.items():
            if key in current or row.rule not in RULE_SOURCES:
                continue
            if row.muted_until is not None and _utc(row.muted_until) > now:
                continue
            row.resolved_at = now


async def open_operational_alert(finding: Finding) -> None:
    """Open/update an operation-owned alert that reconciliation won't resolve."""
    await persist_alerts(
        [finding], {"ecd": True, "db": True, "account": True},
        datetime.now(timezone.utc),
        resolve_missing=False,
    )


async def take_snapshot() -> dict[str, Any]:
    """Capture all sources independently, reconcile healthy ones, and persist."""
    now = datetime.now(timezone.utc)
    sources = [
        await _capture("ecd", _ecd_snapshot),
        await _capture("db", lambda: _db_snapshot(now)),
        await _capture("account", _account_snapshot),
    ]
    async with get_db_session() as session:
        for result in sources:
            session.add(FleetSnapshot(
                id=ascending("fsp"), taken_at=now, source=result.source, ok=result.ok,
                payload=_serializable(result.payload), error=result.error,
            ))
        await session.execute(
            delete(FleetSnapshot).where(FleetSnapshot.taken_at < now - timedelta(days=7))
        )
        if next((item for item in sources if item.source == "db" and item.ok), None):
            await session.execute(
                update(CloudDesktop)
                .where(CloudDesktop.is_deleted.is_(False))
                .values(last_snapshot_at=now)
            )

    by_source = {item.source: item for item in sources}
    config = get_config()
    findings = reconcile(
        by_source["ecd"].payload if by_source["ecd"].ok else None,
        by_source["db"].payload if by_source["db"].ok else None,
        by_source["account"].payload if by_source["account"].ok else None,
        now,
        target_prewarm=config.pool_target_prewarm,
        renew_before_days=config.pool_renew_before_days,
        channel_down_alert_sec=config.fleet_channel_down_alert_sec,
        auto_purchase=config.pool_auto_purchase,
        min_balance_multiple=config.pool_min_account_balance_multiple,
    )
    health = {item.source: item.ok for item in sources}
    await persist_alerts(findings, health, now)
    return {
        "taken_at": now.isoformat(),
        "sources": {item.source: item.ok for item in sources},
        "findings": [asdict(item) for item in findings],
    }


async def run_snapshot_task() -> None:
    """Registered-task adapter; non-Wuying deployments do no fleet I/O."""
    if get_config().sandbox_provider != "wuying":
        return
    await take_snapshot()
