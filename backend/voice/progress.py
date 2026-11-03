"""What the personal assistant is doing right now, from the main session's live events.

The agent loop publishes ``tool.running`` (``data.tool`` is the assistant tool
id, e.g. ``tasks.list``) and ``message.text_delta`` for every run, in this
worker or, through the Redis bus, another one. While a voice turn is pending
the latest step becomes a short phrase ("在翻任务列表") that goes into the
progress reply, instead of a fixed "still working" sentence. The session prompt
only says what is still being done ("还在办：…"): with the step and a queue in
it, answers to new requests wandered into the old ones (2026-10-09).
"""
import time

from core.log import create_logger

log = create_logger("voice.progress")

TEXT_STEP = "<text>"
STEPS = {
    "tasks.list": ("在翻任务列表", "looking through your tasks"),
    "tasks.get": ("在看那个任务现在的状态", "checking that task"),
    "history.read": ("在看那个对话的记录", "reading that conversation"),
    "results.read": ("在看任务的结果", "reading the task's result"),
    "tasks.submit": ("在把活交给项目", "handing the work to the project"),
    "tasks.followup": ("在给任务补充说明", "adding to the task"),
    "assets.attach": ("在把文件交给任务", "passing the files to the task"),
    "assets.list": ("在找文件", "looking for your files"),
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
    "projects.list": ("在看项目", "looking at your projects"),
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
        """The prompt's line on the user's requests still being done (oldest first); empty when none.

        Only the requests: a quiet "remember this" (lane ``remember``) is nothing the user waits for.
        """
        asked = [ref for ref in pending if ref.lane != "remember"]
        if not asked:
            return ""
        if self.lang == "en":
            return "Handed to the personal assistant; awaiting results (not proof of execution): " + ", ".join(f"\"{_short(ref.text)}\"" for ref in asked) + "."
        return "已交给个人助理等待结果（不代表任务已开始执行）：" + "、".join(f"“{_short(ref.text)}”" for ref in asked) + "。"


def _short(text: str) -> str:
    text = " ".join(text.split()).rstrip("。.！!？?")
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS - 1] + "…"


# What a task's own conversation (the build agent) is doing, said without tool names (VOICE_SEAM_PLAN §9.4 step 2).
ROOM_STEPS = {
    "image_gen": ("在生成图片", "generating the image"),
    "image_runninghub": ("在生成图片", "generating the image"),
    "video_production": ("在制作视频", "producing the video"),
    "video_dashscope": ("在生成视频", "generating the video"),
    "video_runninghub": ("在生成视频", "generating the video"),
    "video_compose": ("在剪视频", "editing the video"),
    "video_analyze": ("在看视频内容", "reviewing the video"),
    "desktop_publish": ("在发布", "publishing"),
    "douyin_publish": ("在发布到抖音", "publishing to Douyin"),
    "desktop_login": ("在登录平台", "signing in to the platform"),
    "desktop_takeover": ("在等你接管云桌面", "waiting for you on the cloud desktop"),
    "browser_action": ("在操作网页", "working in the browser"),
    "computer": ("在操作云桌面", "working on the cloud desktop"),
    "web_search": ("在查资料", "searching the web"),
    "web_fetch": ("在看网页", "reading a page"),
    "hot_trends": ("在看热点", "checking what is trending"),
    "share_file": ("在整理文件给你", "preparing the file for you"),
    "asset_delivery": ("在整理文件给你", "preparing the file for you"),
    "question": ("在等你回答", "waiting for your answer"),
    "plan": ("在列计划", "planning"),
    "todo": ("在列计划", "planning"),
    TEXT_STEP: ("在写说明", "writing up"),
}
ROOM_PREFIX_STEPS = {"video": ("在做视频", "working on the video"), "image": ("在做图", "working on the image"),
                     "desktop": ("在操作云桌面", "working on the cloud desktop"),
                     "browser": ("在操作网页", "working in the browser"),
                     "skill": ("在用技能", "using a skill"), "memory": ("在翻记忆", "searching memories")}
ROOM_OTHER_STEP = ("在处理", "working on it")
ROOM_STEP_SECONDS = 180  # a step older than this is not "what it is doing now"


def room_step_phrase(tool: str, lang: str = "zh") -> str:
    pair = ROOM_STEPS.get(tool) or next((value for prefix, value in ROOM_PREFIX_STEPS.items()
                                         if tool.startswith(prefix)), ROOM_OTHER_STEP)
    return pair[1] if lang == "en" else pair[0]


class RoomProgress:
    """The latest step of each task room (execution session) of this call's user, from the live bus.

    ``rooms`` says which sessions are task rooms (voice/shared_context.py knows them after each read);
    everything else of the user's is ignored. ``version`` changes whenever any watched step does."""

    def __init__(self, *, user_id: str, rooms, lang: str = "zh", clock=time.monotonic):
        self.user_id, self.rooms, self.lang, self.clock = user_id, rooms, lang, clock
        self.steps: dict[str, tuple[str, float]] = {}
        self.version = 0
        self._unsubscribe = []

    def start(self) -> None:
        from bus import bus
        from bus.events import MESSAGE_TEXT_DELTA, SESSION_STATUS, TOOL_RUNNING
        self._unsubscribe = [bus.subscribe(kind, self.on_event) for kind in (TOOL_RUNNING, MESSAGE_TEXT_DELTA, SESSION_STATUS)]

    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    def on_event(self, event: dict) -> None:
        data = event.get("data") or {}
        session_id = data.get("sessionId")
        if data.get("userId") != self.user_id or not session_id or session_id not in self.rooms():
            return
        kind = event.get("type")
        if kind == "session.status":
            if data.get("status") in {"idle", "waiting_input", "error"} and session_id in self.steps:
                del self.steps[session_id]
                self.version += 1
            return
        tool = TEXT_STEP if kind == "message.text_delta" else str(data.get("tool") or "")
        if tool:
            phrase = room_step_phrase(tool, self.lang)
            if self.steps.get(session_id, ("", 0))[0] != phrase:
                self.version += 1
            self.steps[session_id] = (phrase, self.clock())

    def step(self, session_id: str | None) -> str:
        """What that room is doing now, or "" when nothing recent."""
        if not session_id or session_id not in self.steps:
            return ""
        phrase, at = self.steps[session_id]
        return phrase if self.clock() - at <= ROOM_STEP_SECONDS else ""
