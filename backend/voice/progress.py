"""What the personal assistant is doing right now, from the main session's live events.

The agent loop publishes ``tool.running`` (``data.tool`` is the assistant tool
id, e.g. ``tasks.list``) and ``message.text_delta`` for every run, in this
worker or, through the Redis bus, another one. While a voice turn is pending
the latest step becomes a short phrase ("在翻你的任务列表") that goes into the
session prompt's progress section and into progress replies, instead of a
fixed "still working" sentence.
"""
import time

from core.log import create_logger

log = create_logger("voice.progress")

TEXT_STEP = "<text>"
STEPS = {
    "tasks.list": ("在翻你的任务列表", "looking through your tasks"),
    "tasks.get": ("在看那个任务现在的状态", "checking that task"),
    "history.read": ("在看那个对话的记录", "reading that conversation"),
    "results.read": ("在看任务的结果", "reading the task's result"),
    "tasks.submit": ("在把活交给项目", "handing the work to the project"),
    "tasks.followup": ("在给任务补充说明", "adding to the task"),
    "assets.attach": ("在把文件交给任务", "passing the files to the task"),
    "assets.list": ("在找你的文件", "looking for your files"),
    "tasks.pause": ("在暂停任务", "pausing the task"),
    "tasks.resume": ("在恢复任务", "resuming the task"),
    "tasks.cancel": ("在取消任务", "cancelling the task"),
    "tasks.delete": ("在停掉任务", "stopping the task"),
    "tasks.archive": ("在整理关注的任务", "tidying the watch list"),
    "tasks.link_existing": ("在关注那个对话", "watching that conversation"),
    "tasks.next_step": ("在安排下一步", "planning the next step"),
    "memory.search": ("在翻记忆", "searching your memories"),
    "memory.read": ("在翻记忆", "reading a memory"),
    "memory.remember": ("在记下来", "saving it to memory"),
    "memory.update": ("在改记忆", "correcting a memory"),
    "memory.forget": ("在删掉那条记忆", "forgetting that memory"),
    "projects.list": ("在看你的项目", "looking at your projects"),
    "projects.create": ("在建项目", "creating the project"),
    "projects.delete": ("在删项目", "deleting the project"),
    "projects.brief.read": ("在看项目简介", "reading the project brief"),
    "projects.brief.update": ("在更新项目简介", "updating the project brief"),
    "sessions.list": ("在找对应的对话", "finding the conversation"),
    "sessions.rename": ("在改对话的名字", "renaming the conversation"),
    "sessions.delete": ("在删会话", "deleting the conversation"),
    "decisions.propose": ("在记下你的决定", "noting your decision"),
    "briefing.configure": ("在设置每日简报", "setting up the daily briefing"),
    "batch": ("在同时查几样东西", "checking a few things at once"),
    "status.credits": ("在看积分还剩多少", "checking your credits"),
    "status.resources": ("在看云电脑能不能用", "checking whether the cloud desktop is ready"),
    "status.publishing": ("在看发布渠道通不通", "checking the publishing route"),
    "status.skills": ("在看能用哪些技能", "checking the available skills"),
    "status.briefing": ("在看每日简报的设置", "checking the daily briefing"),
    TEXT_STEP: ("在整理回复", "writing up the answer"),
}
PREFIX_STEPS = {
    "requests.": ("在看那张卡片", "looking at the request card"),
    "schedules.": ("在看定时任务", "checking your schedules"),
    "status.": ("在看账号这边的情况", "checking your account"),
    "knowledge.": ("在查知识库", "searching your knowledge"),
}
OTHER_STEP = ("在处理", "working on it")
TEXT_CHARS = 40


def step_phrase(tool: str, lang: str = "zh") -> str:
    """A tool id as a short spoken phrase; unknown tools are just "working"."""
    pair = STEPS.get(tool) or next((value for prefix, value in PREFIX_STEPS.items() if tool.startswith(prefix)),
                                   OTHER_STEP)
    return pair[1] if lang == "en" else pair[0]


class Progress:
    """The main session's latest step; ``version`` changes whenever the step does."""

    def __init__(self, *, user_id: str, main_session_id: str, lang: str = "zh", clock=time.monotonic):
        self.user_id, self.main_session_id, self.lang, self.clock = user_id, main_session_id, lang, clock
        self.step, self.at, self.version = "", None, 0
        self._unsubscribe = []

    def start(self) -> None:
        from bus import bus
        from bus.events import MESSAGE_TEXT_DELTA, TOOL_RUNNING
        self._unsubscribe = [bus.subscribe(TOOL_RUNNING, self.on_event),
                             bus.subscribe(MESSAGE_TEXT_DELTA, self.on_event)]

    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    def on_event(self, event: dict) -> None:
        """Synchronous and cheap: called for every run's events in this process."""
        data = event.get("data") or {}
        if data.get("sessionId") != self.main_session_id or data.get("userId") != self.user_id:
            return
        tool = TEXT_STEP if event.get("type") == "message.text_delta" else str(data.get("tool") or "")
        if tool:
            self.set(step_phrase(tool, self.lang))

    def set(self, step: str) -> None:
        if step != self.step:
            self.step, self.at, self.version = step, self.clock(), self.version + 1

    def reset(self) -> None:
        """Nothing pending any more: the next turn starts without a stale step."""
        self.set("")

    def section(self, pending) -> str:
        """The prompt's progress section for the pending voice turns (oldest first); empty when none."""
        if not pending:
            return ""
        running = [ref for ref in pending if ref.message_id] or pending[:1]
        queued = [ref for ref in pending if ref not in running]
        quote = (lambda ref: f"“{_short(ref.text)}”") if self.lang != "en" else (lambda ref: f"\"{_short(ref.text)}\"")
        if self.lang == "en":
            text = f"the assistant is on {', '.join(quote(ref) for ref in running)}"
            text += f" ({self.step})" if self.step else ""
            return text + (f"; {len(queued)} queued: {', '.join(quote(ref) for ref in queued)}." if queued else ".")
        text = f"个人助理正在办{'、'.join(quote(ref) for ref in running)}" + (f"，{self.step}" if self.step else "")
        return text + (f"；排队 {len(queued)} 件：{'、'.join(quote(ref) for ref in queued)}。" if queued else "。")


def _short(text: str) -> str:
    text = " ".join(text.split()).rstrip("。.！!？?")
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS - 1] + "…"
