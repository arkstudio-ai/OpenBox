"""The one line a telling's status comes from (docs/VOICE_DISPATCH_PLAN_20261112.md §4.2).

voice.dispatch_mode=server only. The server writes what it verified: which task, and one of the status
words the task card already uses (在做 / 等你 / 停了 / 做完了 / 出错了) or one of the two handover words
(收到了 / 已交给帮手). The front desk words its own sentence from it; the text assistant's prose is content
for a plain answer and reference otherwise, never the ground for "handed over" or "being done".

2026-11-07: with the prose as the ground, "我来安排" in a reply that submitted nothing was told as arranged.
"""
from voice.phrases import english

_WORKING = {"running": ("在做", "in progress"), "queued": ("已交给帮手，排着队还没开始", "handed over, queued, not started"),
            "resuming": ("已交给帮手，正在接着做", "handed over, resuming")}


def _submission(row: dict, en: bool) -> str:
    state, outcome = row.get("state"), row.get("outcome")
    if state == "canceled":
        word = ("停了", "stopped")
    elif state == "accepted" and row.get("blocked_by"):
        word = ("等你：任务里有一张卡要你先定，这条要求收到了，还没开始做",
                "waiting for you: a card in the task needs your decision first; this request is received, not started")
    elif state == "accepted":
        word = ("已交给帮手，排着队还没开始", "handed over, queued, not started")
    elif state == "claimed":
        word = ("在做", "in progress")
    elif outcome == "succeeded":
        word = ("帮手这一轮处理完了，成品出没出以任务单为准", "this round is done; whether the output exists is on the task card")
    elif outcome == "error":
        word = ("出错了", "failed")
    else:
        word = ("收到了，执行情况还没核实", "received; its execution is not verified")
    return f"{row.get('title') or ''}: {word[1]}" if en else f"「{row.get('title') or ''}」{word[0]}"


def line(*, lang: str, status: str, reason: str, recorded: bool, submissions, running, light_redirect: bool = False,
         report: str | None = None, speech: str = "") -> tuple[str, bool]:
    """(the fact line, whether it states task facts: the prose is then reference, not the answer)."""
    en = english(lang)
    if reason == "control":
        return speech, False  # written from the task's own row (voice/control_notes.py)
    if status != "ok":
        return (("No reply came back this time; nothing about the work is confirmed."
                 if en else "这一次没有拿到回复，交办情况没有确认。"), False)
    if light_redirect:
        return (("Nothing was handed over: the assistant only pointed at a home-screen entry the user cannot open "
                 "on the phone. Ask whether to do it in the background; hand the words over again on a yes."
                 if en else "没有交办：助理只给了首页入口，电话里点不了。问用户要不要直接在后台做，用户说要再转交。"), True)
    tasks = [_submission(row, en) for row in submissions or []]
    known = {row.get("task_id") for row in submissions or []}
    for task in running or []:
        if task.get("task_id") in known:
            continue
        word = _WORKING.get(task.get("state"), _WORKING["running"])
        tasks.append(f"{task.get('title') or ''}: {word[1]}" if en else f"「{task.get('title') or ''}」{word[0]}")
    if report is not None and not recorded:
        if tasks:
            return ("The helper reported. " if en else "帮手回报了。") + ("; ".join(tasks) if en else "；".join(tasks)) + \
                ("." if en else "。"), False
        return ((f"The helper on {report} reported; it is not working on it right now."
                 if en else f"「{report}」帮手回报了，现在没有在做。"), False)
    if tasks:
        return ("Tasks: " if en else "任务单：") + ("; ".join(tasks) if en else "；".join(tasks)) + ("." if en else "。"), True
    if recorded and submissions is None:
        return (("Whether any work was handed over could not be verified: say neither that it started nor that "
                 "it did not arrive." if en else "交办情况没能核实：不能说已经开始，也不能说没送到。"), True)
    return (("No work was handed over in this round; this is the assistant's answer."
             if en else "这一轮没有交办任何任务，下面是助理的回答。"), False)
