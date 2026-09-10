"""Per-run budget ledger and shot verdicts for marketing-autopilot (plan §2.3, §7 D2/D4).

The skill is prose; money and tolerances are not. A run `start`s from its
template, `reserve`s every paid step's estimate before submitting it (denied
when the cap would be exceeded — the run then stops, publishes what it has,
and says so), `record`s outcomes, asks `judge_shot` whether a take is
accepted / regenerated / dropped under the template's tolerances, and ends
with `report`, whose spend figure is read from `usage_events` of the run's
session so it matches the billing page rather than our own estimates.

State lives in process memory keyed by session: a cron run is one session on
one backend; if the process dies the run dies with it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from autopilot.template import AutopilotTemplate
from autopilot.tiers import tier

_RUNS: dict[str, "RunLedger"] = {}
_MAX_RUNS = 500


@dataclass
class Reservation:
    kind: str
    credits: Decimal
    note: str
    at: float


@dataclass
class RunLedger:
    run_id: str
    session_id: str
    workspace_id: str
    user_id: str
    template: AutopilotTemplate
    started_at: datetime
    reservations: list[Reservation] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    #: shot key -> attempts so far
    attempts: dict[str, int] = field(default_factory=dict)
    stopped_reason: str | None = None

    @property
    def cap(self) -> Decimal:
        return Decimal(self.template.credits_cap_per_run)

    @property
    def reserved(self) -> Decimal:
        return sum((r.credits for r in self.reservations), Decimal("0"))

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.reserved

    def public(self) -> dict:
        return {
            "run_id": self.run_id, "cap": str(self.cap), "reserved": str(self.reserved), "remaining": str(self.remaining),
            "stopped_reason": self.stopped_reason, "reservations": len(self.reservations), "events": len(self.events),
        }


def _key(session_id: str) -> str:
    return session_id or "__no_session__"


def start(*, session_id: str, workspace_id: str, user_id: str, template: dict | AutopilotTemplate) -> RunLedger:
    from core.identifier import ascending

    tpl = template if isinstance(template, AutopilotTemplate) else AutopilotTemplate.model_validate(template)
    if len(_RUNS) >= _MAX_RUNS:
        oldest = min(_RUNS, key=lambda k: _RUNS[k].started_at)
        _RUNS.pop(oldest, None)
    run = RunLedger(run_id=ascending("aprun"), session_id=session_id, workspace_id=workspace_id, user_id=user_id,
                    template=tpl, started_at=datetime.now(timezone.utc))
    _RUNS[_key(session_id)] = run
    return run


def get(session_id: str) -> RunLedger | None:
    return _RUNS.get(_key(session_id))


def plan(run: RunLedger) -> dict:
    """Tier → model, and how many videos the cap can afford (plan §2.3: 与预算取小)."""
    from autopilot.tiers import minimum_credits_per_video

    from autopilot.tiers import resolve_resolution

    t = tier(run.template.model_tier)
    resolution, res_note = resolve_resolution(t.model_id, t.resolution)
    per_video = minimum_credits_per_video(run.template.model_tier)
    affordable = int(run.cap // per_video) if per_video > 0 else run.template.videos_per_run
    return {
        "model_id": t.model_id, "resolution": resolution, "resolution_note": res_note, "ratio": "9:16", "tier": t.key, "tier_label": t.label,
        "videos_planned": max(0, min(run.template.videos_per_run, affordable)),
        "videos_requested": run.template.videos_per_run, "min_credits_per_video": str(per_video),
        "publish_mode": run.template.publish_mode, "visibility": run.template.visibility,
        "content_forms": run.template.content_forms, "topics_blocklist": run.template.topics_blocklist,
        "categories": run.template.categories, "hot_source": run.template.hot_source,
        "tolerances": run.template.tolerances.model_dump(),
    }


def reserve(run: RunLedger, *, kind: str, credits: Decimal | str | float, note: str = "") -> tuple[bool, str]:
    """Allow a paid step only if it fits under the cap. A denial stops the run."""
    amount = Decimal(str(credits))
    if amount < 0:
        return False, "negative estimate"
    if run.stopped_reason:
        return False, f"run already stopped: {run.stopped_reason}"
    if run.reserved + amount > run.cap:
        run.stopped_reason = (f"预算上限 {run.cap} 积分：已预留 {run.reserved}，再加 {kind} {amount}（{note}）会超出；"
                              "停止新的付费步骤，已完成的照常发布并写入报告")
        run.events.append({"kind": "budget_stop", "note": run.stopped_reason, "at": time.time()})
        return False, run.stopped_reason
    run.reservations.append(Reservation(kind=kind, credits=amount, note=note, at=time.time()))
    return True, f"reserved {amount} for {kind}; remaining {run.remaining}"


def record(run: RunLedger, *, kind: str, note: str = "", data: dict | None = None) -> None:
    run.events.append({"kind": kind, "note": note[:500], "data": data or {}, "at": time.time()})


def blocked_topic(run: RunLedger, *texts: str) -> str | None:
    """The blocklist word present in any of the texts, or None."""
    joined = " ".join(t or "" for t in texts)
    for word in run.template.topics_blocklist:
        if word and word in joined:
            return word
    return None


def judge_shot(run: RunLedger, *, shot: str, planned_sec: float, actual_sec: float | None, similarity: float | None,
               spoken: bool) -> dict:
    """accept | regenerate | drop for one take, under the template tolerances.

    A spoken shot needs similarity ≥ stt_similarity; every shot needs
    |actual − planned| ≤ duration_deviation_sec. A failing take may be
    regenerated at most `max_regenerations` times, then the video is dropped.
    """
    tol = run.template.tolerances
    problems: list[str] = []
    if actual_sec is not None and abs(actual_sec - planned_sec) > tol.duration_deviation_sec:
        problems.append(f"时长 {actual_sec:.1f}s 偏离计划 {planned_sec:.1f}s 超过 {tol.duration_deviation_sec}s")
    if spoken:
        if similarity is None:
            problems.append("口播段没有转写相似度")
        elif similarity < tol.stt_similarity:
            problems.append(f"转写相似度 {similarity:.2f} 低于 {tol.stt_similarity}")
    attempt = run.attempts.get(shot, 0) + 1
    run.attempts[shot] = attempt
    if not problems:
        verdict = "accept"
    elif attempt <= tol.max_regenerations:
        verdict = "regenerate"
    else:
        verdict = "drop"
    out = {"shot": shot, "attempt": attempt, "verdict": verdict, "problems": problems,
           "regenerations_left": max(0, tol.max_regenerations - attempt) if verdict != "accept" else None}
    record(run, kind="shot_verdict", note=f"{shot} attempt {attempt}: {verdict}", data=out)
    return out


async def session_spend(session_id: str) -> tuple[Decimal, dict[str, Decimal], int]:
    """Total credits and per-kind totals billed to this session (shadow or charged)."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.billing import UsageEvent

    async with get_db_session() as db:
        rows = (await db.execute(select(UsageEvent.kind, UsageEvent.credits).where(
            UsageEvent.session_id == session_id, UsageEvent.status.in_(("charged", "shadow")),
        ))).all()
    per_kind: dict[str, Decimal] = {}
    total = Decimal("0")
    for kind, credits in rows:
        if credits is None:
            continue
        c = Decimal(str(credits))
        per_kind[kind] = per_kind.get(kind, Decimal("0")) + c
        total += c
    return total, per_kind, len(rows)


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


async def report(run: RunLedger, *, items: list[dict]) -> dict:
    """The run report (plan §2.4 / D4). `items` is the skill's per-video list:
    {source, hot_title, form, analysis_summary, shots, final_asset_id, title, intro, topics,
     publish: {mode, status, item_id | reason}, dropped_reason}."""
    total, per_kind, n = await session_spend(run.session_id)
    published = [i for i in items if (i.get("publish") or {}).get("status") == "published"]
    degraded = [i for i in items if (i.get("publish") or {}).get("status") in ("package", "degraded")]
    dropped = [i for i in items if i.get("dropped_reason")]
    from zoneinfo import ZoneInfo

    lines = [f"# 自动营销运行报告 · {run.started_at.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}",
             f"模型档位 {tier(run.template.model_tier).label}（{tier(run.template.model_tier).model_id}）· 预算上限 {_fmt(run.cap)} 积分 · "
             f"本次实际落账 {_fmt(total)} 积分（{n} 笔）" + (f" · 预留估价 {_fmt(run.reserved)}" if run.reservations else ""),
             f"成片 {len([i for i in items if i.get('final_asset_id')])} 条 · 已发布 {len(published)} · 待扫码/降级 {len(degraded)} · 弃用 {len(dropped)}"]
    lines.append("费用口径：统计到报告生成这一刻本次运行会话的账单行；报告之后的回复不计。")
    if run.stopped_reason:
        lines.append(f"⚠️ {run.stopped_reason}")
    for idx, it in enumerate(items, 1):
        pub = it.get("publish") or {}
        lines.append("")
        lines.append(f"## {idx}. {it.get('title') or it.get('hot_title') or '（未命名）'}")
        if it.get("hot_title"):
            lines.append(f"- 热点：{it['hot_title']}（{it.get('source') or '?'}）· 形态 {it.get('form') or '?'}")
        if it.get("analysis_summary"):
            lines.append(f"- 拆解：{str(it['analysis_summary'])[:200]}")
        if it.get("shots"):
            lines.append(f"- 分段：{it['shots']}")
        if it.get("intro") or it.get("topics"):
            lines.append(f"- 简介：{it.get('intro') or ''} " + " ".join(f"#{t}" for t in (it.get("topics") or [])))
        if it.get("credits"):
            lines.append(f"- 花费：{it['credits']} 积分")
        if it.get("dropped_reason"):
            lines.append(f"- ❌ 弃用：{it['dropped_reason']}")
        elif pub:
            status = pub.get("status")
            if status == "published":
                lines.append(f"- ✅ 已发布（{pub.get('visibility') or ''}）" + (f" 作品 {pub['item_id']}" if pub.get("item_id") else ""))
            elif status in ("package", "degraded"):
                lines.append(f"- 🟡 待你扫码发布：{pub.get('reason') or '按模版走投稿包'}")
            else:
                lines.append(f"- ⚪ 发布：{status or '未发布'} {pub.get('reason') or ''}".rstrip())
    if per_kind:
        lines.append("")
        lines.append("花费明细：" + "，".join(f"{k} {_fmt(v)}" for k, v in sorted(per_kind.items())))
    record(run, kind="report", note="report generated", data={"total": str(total), "items": len(items)})
    return {"run_id": run.run_id, "markdown": "\n".join(lines), "total_credits": _fmt(total), "per_kind": {k: _fmt(v) for k, v in per_kind.items()},
            "usage_events": n, "reserved": _fmt(run.reserved), "cap": _fmt(run.cap), "stopped_reason": run.stopped_reason,
            "published": len(published), "degraded": len(degraded), "dropped": len(dropped), "items": items}


# ── enforcement hooks used by the paid tools ────────────────────────────────
class BudgetStop(Exception):
    """Raised by a paid tool when the run's cap would be exceeded."""

    public_message = True  # the text is ours, safe to show the model verbatim


class LockViolation(Exception):
    """Raised by video_generate when a run is active and the request leaves the template's model/resolution."""

    public_message = True


def guard_paid_step(session_id: str, *, kind: str, credits, note: str = "") -> str | None:
    """Reserve `credits` on this session's run, if one exists. Returns the reservation
    message, None when no run is active, raises BudgetStop when the cap is reached."""
    run = get(session_id or "")
    if run is None:
        return None
    ok, msg = reserve(run, kind=kind, credits=credits, note=note)
    if not ok:
        raise BudgetStop(msg)
    return msg


def check_lock(session_id: str, *, model_id: str | None, resolution: str | None) -> None:
    """A run fixes model and resolution; any paid generation outside them is refused."""
    run = get(session_id or "")
    if run is None:
        return
    p = plan(run)
    if model_id and model_id != p["model_id"]:
        raise LockViolation(f"自动营销运行锁定了模型 {p['model_id']}（模版档位 {p['tier_label']}），不能用 {model_id}")
    if resolution and resolution != p["resolution"]:
        raise LockViolation(f"自动营销运行锁定了分辨率 {p['resolution']}，不能用 {resolution}"
                            + (f"（{p['resolution_note']}）" if p.get("resolution_note") else ""))

