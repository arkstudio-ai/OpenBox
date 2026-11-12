"""The six numbers the dispatch plan is judged by (docs/VOICE_DISPATCH_PLAN_20261112.md §4.7).

Read-only, over one time window of calls. Three come from rows every call
already writes (turns, Inbox items, assistant commands); three need what was
actually said, which only ``voice.line_log`` keeps (``voice_lines``), and
report ``measured: False`` for a window without it.

``unbacked_claims`` is a text heuristic, not a verdict: an assistant sentence
that sounds like "handed over / being done" while no work command had come
from this call by then. It lists where to look (call and line number), and a
person or a judge model decides. The other five are counts and times.
"""
import re
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select

# Commands that give a task work (voice/assistant_link.py WORK_ACTIONS), and the one that opens a task.
WORK_ACTIONS = ("task_create", "task_input", "task_revise", "task_resume", "task_finish_continuation")
REPEAT_WINDOW, REPEAT_COUNT = timedelta(minutes=10), 3
# Replies the user asked for; everything else the assistant starts on its own (a result, progress, a notice).
ASKED_KINDS = frozenset({"model", "followup", "keepalive", ""})
_SENTENCE = re.compile(r"[^。！？!?；;\n]+")
_CLAIM = re.compile(
    r"(已经?|刚刚?|都)\s*(帮你|给你|为你)?\s*(交给|转交|转给|交办|提交|递交|安排|派给|建好|开好|创建好?|下单)"
    r"|正在\s*(做|处理|制作|生成|剪|跑)|在(做|处理|制作|生成)了"
    r"|帮手\s*(已经|正)?\s*(在|开始)\s*(做|处理|制作)?"
    r"|(handed (it |this )?over|already submitted|is (being|now) (made|processed|worked on))", re.IGNORECASE)
_NEGATED = re.compile(r"还没|尚未|没有|并未|未能|不能说|not yet|has not|hasn't", re.IGNORECASE)


def claims(text: str) -> list[str]:
    """The sentences of one assistant line that say work was handed over or is under way."""
    return [sentence.strip() for sentence in _SENTENCE.findall(text or "")
            if _CLAIM.search(sentence) and not _NEGATED.search(sentence)]


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive values for timezone-aware columns; they were written in UTC.
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))]


def _spread(values: list[float]) -> dict:
    return {"count": len(values), "p50": median(values) if values else None, "p90": _percentile(values, 0.9)}


async def measure(since: datetime, until: datetime, *, user_id: str | None = None) -> dict:
    """The six metrics for calls started in ``[since, until)``; one user's when ``user_id`` is given."""
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from db.models.assistant import AssistantCommand
    from db.models.voice import VoiceCall, VoiceLine, VoiceTurn

    async with get_db_session() as db:
        scope = [VoiceCall.started_at >= since, VoiceCall.started_at < until]
        if user_id:
            scope.append(VoiceCall.user_id == user_id)
        calls = list((await db.scalars(select(VoiceCall).where(*scope))).all())
        call_ids = [call.id for call in calls]
        if not call_ids:
            return _report(since, until, [], [], {}, [], [])
        turns = list((await db.scalars(select(VoiceTurn).where(VoiceTurn.call_id.in_(call_ids)))).all())
        inbox_ids = [turn.inbox_id for turn in turns if turn.inbox_id]
        runs = dict((await db.execute(select(AgentInboxItem.id, AgentInboxItem.run_id).where(
            AgentInboxItem.id.in_(inbox_ids)))).all()) if inbox_ids else {}
        sessions = {call.main_session_id for call in calls}
        # A call can outlive the window's end; its commands are still its own.
        commands = list((await db.scalars(select(AssistantCommand).where(
            AssistantCommand.assistant_session_id.in_(sessions), AssistantCommand.target_type == "task",
            AssistantCommand.action.in_(WORK_ACTIONS), AssistantCommand.created_at >= since,
            AssistantCommand.created_at < until + timedelta(days=1)))).all())
        lines = list((await db.scalars(select(VoiceLine).where(VoiceLine.call_id.in_(call_ids))
                                       .order_by(VoiceLine.call_id, VoiceLine.seq))).all())
    return _report(since, until, calls, turns, runs, commands, lines)


def _report(since, until, calls, turns, runs, commands, lines) -> dict:
    by_run: dict[str, list] = {}
    for command in commands:
        run_id = (command.source_ref or {}).get("run_id")
        if run_id:
            by_run.setdefault(run_id, []).append(command)
    turn_runs = {turn.id: runs.get(turn.inbox_id) for turn in turns if turn.inbox_id}

    # 2. From the user's words being handed over to the task existing.
    waits = []
    for turn in turns:
        created = [_aware(command.created_at) for command in by_run.get(turn_runs.get(turn.id) or "", [])
                   if command.action == "task_create"]
        if created:
            waits.append(max((min(created) - _aware(turn.requested_at)).total_seconds(), 0.0))

    # 3. A result that came back and was never told.
    settled = [turn for turn in turns if turn.settled_at is not None and turn.outcome in ("delivered", "cancelled")]
    untold = sum(turn.outcome == "cancelled" for turn in settled)

    # 4. The same task given work three times within ten minutes.
    per_task: dict[str, list[datetime]] = {}
    for command in commands:
        if command.action == "task_input" and command.target_id:
            per_task.setdefault(command.target_id, []).append(_aware(command.created_at))
    repeated = sorted(task_id for task_id, times in per_task.items() if _bursts(sorted(times)))

    # 1, 5, 6 need the call as it was said.
    logged = {line.call_id for line in lines}
    handed: dict[str, list[datetime]] = {}  # call -> when work commands from its own turns were recorded
    for turn in turns:
        for command in by_run.get(turn_runs.get(turn.id) or "", []):
            handed.setdefault(turn.call_id, []).append(_aware(command.created_at))
    suspects = []
    for line in lines:
        if line.role != "assistant" or line.kind not in ("model", "followup"):
            continue  # a delivery tells what came back; only the assistant's own replies can promise unbacked work
        found = claims(line.text)
        if found and not any(moment <= _aware(line.at) for moment in handed.get(line.call_id, [])):
            suspects.append({"call_id": line.call_id, "seq": line.seq, "sentences": len(found)})
    tellings = [len(line.text) for line in lines if line.role == "assistant" and line.kind == "delivery"]
    proactive = sum(line.role == "assistant" and line.kind not in ASKED_KINDS for line in lines)
    cut = sum(line.role == "event" and line.kind.startswith("interrupt:")
              and line.kind.split(":", 1)[1] not in ASKED_KINDS for line in lines)

    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "calls": len(calls), "turns": len(turns), "calls_with_lines": len(logged),
        "unbacked_claims": {"measured": bool(logged), "heuristic": True, "count": len(suspects),
                            "calls": len({item["call_id"] for item in suspects}), "where": suspects[:50]},
        "dispatch_seconds": _spread(waits),
        "results_untold": {"settled": len(settled), "untold": untold,
                           "share": untold / len(settled) if settled else None},
        "repeated_delegation": {"tasks": len(repeated), "task_ids": repeated[:50]},
        "telling_chars": {"measured": bool(logged), **_spread([float(value) for value in tellings])},
        # An interrupted telling may leave no line of its own, so the larger of the two is the denominator.
        "tellings_interrupted": {"measured": bool(logged), "proactive": proactive, "interrupted": cut,
                                 "share": cut / max(proactive, cut) if max(proactive, cut) else None},
    }


def _bursts(times: list[datetime]) -> bool:
    return any(times[index + REPEAT_COUNT - 1] - times[index] <= REPEAT_WINDOW
               for index in range(len(times) - REPEAT_COUNT + 1))


def render(report: dict, lang: str = "zh") -> str:
    """The report as the plan's table, for a terminal or a message."""
    def seconds(value):
        return "—" if value is None else f"{value:.0f}s"

    def share(value):
        return "—" if value is None else f"{value * 100:.1f}%"

    def unmeasured(item, text):
        return text if item["measured"] else "未采集（voice.line_log 未开）"

    claims_, waits, untold = report["unbacked_claims"], report["dispatch_seconds"], report["results_untold"]
    repeat, chars, cut = report["repeated_delegation"], report["telling_chars"], report["tellings_interrupted"]
    rows = [
        ("空口交办（疑似，需复核）", unmeasured(claims_, f"{claims_['count']} 句 / {claims_['calls']} 通"), "0"),
        ("交办到任务单", f"p50 {seconds(waits['p50'])} · p90 {seconds(waits['p90'])}（{waits['count']} 次）", "p90 ≤ 60s"),
        ("结果没播出", f"{share(untold['share'])}（{untold['untold']}/{untold['settled']}）", "≤ 5%"),
        ("同任务重复交办", f"{repeat['tasks']} 个任务", "0"),
        ("结果播报长度", unmeasured(chars, f"中位 {seconds(chars['p50']).rstrip('s')} 字 · p90 "
                                          f"{seconds(chars['p90']).rstrip('s')} 字（{chars['count']} 次）"), "看趋势"),
        ("主动播报被打断", unmeasured(cut, f"{share(cut['share'])}（{cut['interrupted']}/{max(cut['proactive'], cut['interrupted'])}）"),
         "基线减半"),
    ]
    head = (f"语音交办指标 {report['window']['since'][:10]} → {report['window']['until'][:10]}："
            f"{report['calls']} 通电话，{report['turns']} 次转交，{report['calls_with_lines']} 通有逐句记录")
    return "\n".join([head, *(f"- {name}：{value}（目标 {goal}）" for name, value, goal in rows)])
