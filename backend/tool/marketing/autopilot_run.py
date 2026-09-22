"""autopilot_run: the deterministic half of the marketing-autopilot skill.

The skill decides what to make; this tool keeps the money and the tolerances
honest: it turns the template into a plan (model, resolution, how many
videos the cap affords), reserves every paid step against the cap, judges
takes under the tolerances, and writes the run report from real billing rows.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from autopilot import ledger
from autopilot.template import AutopilotTemplate
from autopilot.tiers import describe_tiers
from tool.tool import ToolContext, ToolResult, define_tool


class AutopilotRunArgs(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["tiers", "validate_template", "start", "reserve", "record", "judge_shot", "status", "report"]
    #: start / validate_template: the template JSON (from the cron prompt block or the person's choices).
    template: dict | None = None
    #: reserve / record: what the credits are for (analysis | generation | compose | publish | hot_trends | other).
    kind: str | None = Field(default=None, max_length=32)
    #: reserve: the estimate the tool just returned (credits).
    credits: str | float | None = None
    note: str = Field(default="", max_length=500)
    data: dict | None = None
    #: judge_shot
    shot: str | None = Field(default=None, max_length=64)
    planned_sec: float | None = Field(default=None, ge=0)
    actual_sec: float | None = Field(default=None, ge=0)
    similarity: float | None = Field(default=None, ge=0, le=1)
    spoken: bool = True
    #: report: one entry per video attempted.
    items: list[dict] | None = None

    @model_validator(mode="after")
    def _by_action(self) -> "AutopilotRunArgs":
        if self.action in ("start", "validate_template") and self.template is None:
            raise ValueError(f"{self.action} requires template")
        if self.action == "reserve" and (self.kind is None or self.credits is None):
            raise ValueError("reserve requires kind and credits")
        if self.action == "record" and self.kind is None:
            raise ValueError("record requires kind")
        if self.action == "judge_shot" and (self.shot is None or self.planned_sec is None):
            raise ValueError("judge_shot requires shot and planned_sec")
        if self.action == "report" and self.items is None:
            raise ValueError("report requires items (one per video attempted; [] if none)")
        return self


def _need_run(ctx: ToolContext) -> ledger.RunLedger | None:
    return ledger.get(ctx.session_id or "")


async def execute(args: AutopilotRunArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "tiers":
        tiers = describe_tiers()
        lines = [f"- {t['tier']}（{t['label']}）：{t['model_id']} @ {t['resolution']}，每 {t['reference_seconds']} 秒约 {t['reference_credits'] or '价格未定'} 积分 — {t['pitch']}" for t in tiers]
        return ToolResult(title="Model tiers", output="\n".join(lines), metadata={"tiers": tiers})
    if args.action == "validate_template":
        try:
            tpl = AutopilotTemplate.model_validate(args.template)
        except Exception as exc:
            return ToolResult(title="Template invalid", output=str(exc)[:1200], metadata={"valid": False})
        return ToolResult(title="Template valid", output="valid=true\n" + json.dumps(tpl.public(), ensure_ascii=False),
                          metadata={"valid": True, "template": tpl.public()})
    if args.action == "start":
        try:
            run = ledger.start(session_id=ctx.session_id or "", workspace_id=ctx.workspace_id, user_id=ctx.user_id, template=args.template)
        except Exception as exc:
            return ToolResult(title="Template invalid", output=str(exc)[:1200], metadata={"valid": False})
        p = ledger.plan(run)
        lines = [f"run_id={run.run_id}", f"cap={run.cap} remaining={run.remaining}",
                 f"model={p['model_id']} resolution={p['resolution']} ratio=9:16 (tier {p['tier']}, fixed by the template — never switch; video_generate refuses anything else)"
                 + (f"\nresolution_note={p['resolution_note']}" if p.get("resolution_note") else ""),
                 f"videos_planned={p['videos_planned']} (requested {p['videos_requested']}, ≥{p['min_credits_per_video']} credits each)",
                 f"publish_mode={p['publish_mode']} visibility={p['visibility']} hot_source={p['hot_source']} categories={p['categories'] or '*'}",
                 f"content_forms={p['content_forms']} topics_blocklist={p['topics_blocklist']}",
                 f"tolerances={p['tolerances']}",
                 "Paid tools (video_generate, video_compose, video_analyze, hot_trends live fetch) reserve their own estimate against this cap automatically; "
                 "a refusal that says 预算上限 means stop new paid work. Use action=reserve only for costs no tool reserves (e.g. publish)."]
        return ToolResult(title="Autopilot run started", output="\n".join(lines), metadata={"run": run.public(), "plan": p})
    run = _need_run(ctx)
    if run is None:
        return ToolResult(title="No autopilot run", output="Call action=start with the template first (this session has no run).",
                          metadata={"error": "no_run"})
    if args.action == "reserve":
        try:
            amount = Decimal(str(args.credits))
        except InvalidOperation:
            return ToolResult(title="Bad estimate", output=f"credits {args.credits!r} is not a number")
        ok, msg = ledger.reserve(run, kind=args.kind or "other", credits=amount, note=args.note)
        return ToolResult(title="Reserved" if ok else "Budget stop", output=f"allowed={'true' if ok else 'false'}\n{msg}",
                          metadata={"allowed": ok, "run": run.public()})
    if args.action == "record":
        blocked = None
        if args.kind == "candidate" and args.data:
            blocked = ledger.blocked_topic(run, str(args.data.get("title", "")), " ".join(map(str, args.data.get("topics") or [])))
        ledger.record(run, kind=args.kind or "note", note=args.note, data={**(args.data or {}), **({"blocked_by": blocked} if blocked else {})})
        out = f"recorded {args.kind}"
        if args.kind == "candidate":
            out += f"\nblocked_topic={blocked or 'none'}"
            if args.data and run.template.content_forms and args.data.get("form") and args.data["form"] not in run.template.content_forms:
                out += f"\nform_allowed=false ({args.data['form']} not in template content_forms)"
            else:
                out += "\nform_allowed=true" if args.data and args.data.get("form") else ""
        return ToolResult(title="Recorded", output=out, metadata={"run": run.public(), "blocked_topic": blocked})
    if args.action == "judge_shot":
        verdict = ledger.judge_shot(run, shot=args.shot or "", planned_sec=args.planned_sec or 0.0, actual_sec=args.actual_sec,
                                    similarity=args.similarity, spoken=args.spoken)
        lines = [f"verdict={verdict['verdict']} attempt={verdict['attempt']}"] + [f"- {p}" for p in verdict["problems"]]
        if verdict["verdict"] == "regenerate":
            lines.append(f"Regenerate this shot once more (reserve its estimate first); {verdict['regenerations_left']} regeneration(s) left after this.")
        elif verdict["verdict"] == "drop":
            lines.append("Drop this video from the run and say why in the report; do not regenerate again.")
        return ToolResult(title=f"Shot {verdict['verdict']}", output="\n".join(lines), metadata=verdict)
    if args.action == "status":
        return ToolResult(title="Autopilot run", output=json.dumps(run.public(), ensure_ascii=False), metadata={"run": run.public(), "plan": ledger.plan(run)})
    rep = await ledger.report(run, items=args.items or [])
    return ToolResult(title="Autopilot report", output=rep["markdown"], metadata={k: v for k, v in rep.items() if k != "markdown"})


AUTOPILOT_RUN_DESCRIPTION = """Bookkeeping for a marketing-autopilot run (the skill decides; this tool keeps money and tolerances honest).
- tiers: the three model tiers with reference prices (for the selection card).
- validate_template: check a template JSON before creating the cron job.
- start: begin this session's run from the template → model/resolution to use, how many videos the cap affords, tolerances.
- reserve: before EVERY paid submit (video_generate, video_compose, video_analyze, hot_trends live fetch, publish), \
pass the estimate; allowed=false means the cap is reached: stop new paid work, publish what is finished, report.
- record: log candidates (kind=candidate with data {title, topics, form} → blocked_topic / form_allowed), analyses, outcomes.
- judge_shot: accept | regenerate | drop for a take under the template tolerances (counts attempts per shot).
- report: the run report (markdown) with spend read from this session's billing rows; pass items (one per video)."""

autopilot_run_tool = define_tool(
    "autopilot_run",
    description=AUTOPILOT_RUN_DESCRIPTION,
    parameters=AutopilotRunArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
