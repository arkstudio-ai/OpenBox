"""The personal-assistant side of ``assistant_ask``: each call is one main-session turn.

A voice turn is accepted exactly like typed input (``inputs.accept_turn``),
marked ``entrypoint=assistant_voice`` so the projection asks for a reply that
works on the phone. Its text is the front desk's restatement of the request
(the assistant never heard the call); ``voice_context`` carries the user's own
words and the call's last lines, which the projection adds to that turn
(assistant/projection.py). The reply is the settled Inbox item's result message.

A turn that stops at a confirmation card settles too (the run waits for the
answer): its result then carries the cards waiting in the main session, for
the front desk to read out (voice/cards.py). A card answered in the call
resumes the turn through a new Inbox item (question/continuation.py), which
``follow`` waits for like a turn.

A turn that handed the work to a task ends before the work does: the task
reports back on its own later (assistant/results.py, voice/reports.py). Its
result then names the tasks still at it, read from the commands the run
issued, not from its wording, so the call tells it as passed on, never as a
result. Measured 2026-10-08: "帮我联网查一下……" twice ended with "已经安排去
查了，正在后台检索"; the front desk, told the result had come, said "查好了"
and made one up, a minute before the real one arrived.
"""
import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from core.log import create_logger
from voice import calls, phrases
from voice.prompt import VOICE_ENTRYPOINT
from voice.speech_text import clean

log = create_logger("voice.assistant_link")

WAIT_STEP_SECONDS = 0.5  # how soon a hung-up call stops observing, and a claimed message is noticed
RESUME_ENTRYPOINT = "question_answer"  # the Inbox item an answered main-session card resumes the turn with
# Commands that give a task work it reports back on by itself, and the task states that mean it is still at it.
WORK_ACTIONS = ("task_create", "task_input", "task_resume", "task_finish_continuation")
WORKING = ("queued", "running", "resuming")


@dataclass(eq=False)
class VoiceTurnRef:
    """One ``assistant_ask`` while the call lasts; ``id`` is the VoiceTurn row and the event's turn_id."""
    id: str
    provider_call_id: str
    text: str
    transcript: str
    requested: float              # bridge clock
    inbox_id: str | None = None
    message_id: str | None = None
    status: str = "pending"       # pending, then ok / failed / timeout
    reason: str = ""              # a result's note: ok / timeout / failed / unavailable
    speech: str = ""
    late: bool = False
    progress: int = 0             # progress replies said while it was pending
    delivery: str = ""            # queued / creating / delivering / covered / done
    attempts: int = 0
    note: str = ""                # the background note this result became
    note_pending: bool = False    # created, its item id not seen yet
    note_item: str | None = None  # the provider's item id, while the note is in the conversation
    note_at: float | None = None  # bridge clock when the provider confirmed the note
    note_seq: int = 0             # ...and how many provider events had arrived by then
    covered_by: str | None = None  # a reply that saw the note before ours could start
    cards: list | None = None     # main-session cards waiting when the result came (voice/cards.py)
    report: str | None = None     # a task report nobody asked for in this call: the task's title ("" unknown)
    asked: str = ""               # a report answering a request of this call that was passed on: the user's words
    running: list | None = None   # the tasks the turn passed the work to, still at it when it ended
    riders: list | None = None    # other results told in the same note and reply as this one
    context: dict | None = None   # the user's own words and the call's last lines, for the assistant (start)
    lane: str = "assistant"       # assistant: a main-session turn; local / ask: settled by the handover plan
    recall: int | None = None     # what the user's records add to a reply: the utterance's number, else None
    settled: float | None = None  # bridge clock, for the turn's log line
    finished: float | None = None

    @property
    def recorded(self) -> bool:
        """A request of this call (a VoiceTurn row), unlike a report or a recall nobody handed over."""
        return self.report is None and self.recall is None


def turn_outcome(ref: VoiceTurnRef, *, heard: bool = False, pending: bool = False) -> str:
    """The VoiceTurn outcome once the call is done with a turn."""
    if pending or ref.status == "timeout":
        return "late"  # the assistant still answers, in the conversation
    if ref.status != "ok":
        return "failed"
    return "delivered" if heard else "cancelled"


class AssistantLink:
    def __init__(self, *, call_id: str, user_id: str, workspace_id: str, main_session_id: str,
                 lang: str, turn_timeout: float, model: str | None = None, variant: str | None = None):
        self.call_id, self.user_id, self.workspace_id = call_id, user_id, workspace_id
        self.main_session_id, self.lang, self.turn_timeout = main_session_id, lang, turn_timeout
        # A faster model/variant for voice turns (VoiceConfig.turn_model/turn_variant); None keeps the main one's.
        self.model, self.variant = model or None, variant or None
        self.count = 0
        self.closed = False  # set when the call ends; observers stop at their next step

    async def start(self, ref: VoiceTurnRef) -> None:
        """Accept the request as a main-session turn and record the voice turn."""
        from agent.inbox import schedule_inbox_wake
        from assistant import inputs
        self.count += 1

        async def accept(context: dict | None):
            return await inputs.accept_turn(
                user_id=self.user_id, workspace_id=self.workspace_id, main_id=self.main_session_id,
                client_id=f"voice:{self.call_id}:{self.count}", text=ref.text, model=self.model,
                variant=self.variant, entrypoint=VOICE_ENTRYPOINT,
                extra_ref={"voice_call_id": self.call_id, **({"voice_context": context} if context else {})})
        try:
            try:
                receipt = await accept(ref.context)
            except ValueError:
                if not ref.context:
                    raise
                # The input's origin reference has a size bound shared with other context: the request alone.
                log.info("voice turn context dropped turn=%s call=%s", ref.id, self.call_id)
                receipt = await accept(None)
        except Exception:
            await self._add(ref, outcome="failed")
            raise
        if receipt["state"] == "accepted":
            schedule_inbox_wake(self.main_session_id, self.user_id)
        ref.inbox_id, ref.message_id = receipt["inbox_id"], receipt["message_id"]
        await self._add(ref, inbox_id=ref.inbox_id, message_id=ref.message_id)

    async def wait(self, ref: VoiceTurnRef, *, elapsed: float = 0.0, on_message=None) -> dict | None:
        """The turn's function_call_output, ``{"status": ok|failed|timeout, "speech": ...}``; None after hang-up.

        Waits in short steps, so a finished call stops observing between reads
        instead of cancelling one, and so the user message is noticed once the
        run claims the input (the client scrolls to it).
        """
        from agent.inbox import get_inbox_item, wait_for_inbox_terminal
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.turn_timeout - elapsed, 0.1)
        while not self.closed:
            try:
                step = max(min(WAIT_STEP_SECONDS, deadline - loop.time()), 0.01)
                receipt = await wait_for_inbox_terminal(ref.inbox_id, user_id=self.user_id, timeout=step)
                break
            except TimeoutError:
                if loop.time() >= deadline:
                    # The turn goes on; its result reaches the conversation, not this call.
                    await self.record(ref, outcome="late")
                    return {"status": "timeout", "speech": phrases.speech_text("timeout", self.lang)}
            if on_message is not None and not ref.message_id:
                current = await get_inbox_item(ref.inbox_id, user_id=self.user_id)
                if current is not None and current.message_id:
                    ref.message_id = current.message_id
                    await self.record(ref, message_id=current.message_id)
                    on_message(ref)
        else:
            return None
        if receipt.state == "settled" and receipt.outcome == "succeeded":
            text = (await reply_text(self.main_session_id, receipt.result_message_id, self.user_id)
                    if receipt.result_message_id else "")
            await self.record(ref, settled=True, result_message_id=receipt.result_message_id,
                              message_id=receipt.message_id or ref.message_id)
            cards = await self.cards()  # the turn may be waiting for the user's confirmation
            running = await self.running(ref)  # or it passed the work on: the task's result comes later
            speech = clean(text, self.lang) or ("" if cards else phrases.phrase_text("result_in_text", self.lang))
            return {"status": "ok", "speech": speech, **({"cards": cards} if cards else {}),
                    **({"running": running} if running else {})}
        await self.record(ref, settled=True, outcome="failed", result_message_id=receipt.result_message_id)
        return {"status": "failed", "speech": phrases.speech_text("failed", self.lang)}

    async def follow(self, ref: VoiceTurnRef, *, after: str, on_message=None) -> dict | None:
        """A card answered in the call: wait for the turn it resumes, then its result like ``wait``.

        ``after`` is the newest main-session Inbox item before the answer; the
        continuation adds the resuming item within a couple of seconds.
        """
        await self._add(ref)
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not self.closed:
            ref.inbox_id = await resumed_item(self.main_session_id, self.user_id, after)
            if ref.inbox_id:
                await self.record(ref, inbox_id=ref.inbox_id)
                return await self.wait(ref, elapsed=loop.time() - started, on_message=on_message)
            if loop.time() - started >= self.turn_timeout:
                await self.record(ref, outcome="late")
                return {"status": "timeout", "speech": phrases.speech_text("timeout", self.lang)}
            await asyncio.sleep(WAIT_STEP_SECONDS)
        return None

    async def cards(self) -> list[dict]:
        """The main session's cards waiting for the user; none if they cannot be read."""
        from assistant.confirmations import pending_cards
        try:
            return await pending_cards(self.user_id, self.workspace_id, self.main_session_id)
        except Exception as exc:  # the result is still told; the card stays on screen
            log.warning("voice cards unread call=%s error=%s", self.call_id, type(exc).__name__)
            return []

    async def running(self, ref: VoiceTurnRef) -> list[dict]:
        """The tasks the turn passed the work to that are still at it; none if they cannot be read."""
        try:
            return await running_tasks(self.main_session_id, self.user_id, ref.inbox_id)
        except Exception as exc:  # told as the assistant put it; the delivery check still applies (voice/turns.py)
            log.warning("voice running tasks unread call=%s error=%s", self.call_id, type(exc).__name__)
            return []

    async def answered(self, ref: VoiceTurnRef) -> None:
        """A request the handover plan settled itself (an answer, or a question back): recorded, no main turn."""
        await self._add(ref)

    async def done(self, ref: VoiceTurnRef, outcome: str, *, delivered: bool = False) -> None:
        """A turn's last word in this call: its outcome and one log line (times, never words)."""
        await self.record(ref, outcome=outcome, delivered=delivered)
        settle = f"{ref.settled - ref.requested:.1f}" if ref.settled is not None else "-"
        deliver = (f"{ref.finished - ref.settled:.1f}" if delivered and None not in (ref.settled, ref.finished)
                   else "-")
        log.info("voice turn=%s call=%s inbox=%s lane=%s settle_s=%s deliver_s=%s outcome=%s",
                 ref.id, self.call_id, ref.inbox_id, ref.lane, settle, deliver, outcome)

    async def record(self, ref: VoiceTurnRef, **fields) -> None:
        try:
            await calls.update_turn(ref.id, **fields)
        except Exception as exc:  # bookkeeping only; the call goes on
            log.warning("voice turn not recorded turn=%s error=%s", ref.id, type(exc).__name__)

    async def _add(self, ref: VoiceTurnRef, **fields) -> None:
        try:
            await calls.add_turn(turn_id=ref.id, call_id=self.call_id, user_id=self.user_id,
                                 provider_call_id=ref.provider_call_id, transcript=ref.transcript, **fields)
        except Exception as exc:
            log.warning("voice turn not recorded turn=%s error=%s", ref.id, type(exc).__name__)


async def latest_inbox_id(session_id: str, user_id: str) -> str:
    """The newest Inbox item of a session (ids sort by time), or ""."""
    from sqlalchemy import func
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    async with get_db_session() as db:
        return await db.scalar(select(func.max(AgentInboxItem.id)).where(
            AgentInboxItem.session_id == session_id, AgentInboxItem.user_id == user_id)) or ""


async def resumed_item(session_id: str, user_id: str, after: str) -> str | None:
    """The Inbox item that resumed the session after an answered card, added after ``after``."""
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    async with get_db_session() as db:
        rows = (await db.execute(select(AgentInboxItem.id, AgentInboxItem.origin_ref).where(
            AgentInboxItem.session_id == session_id, AgentInboxItem.user_id == user_id,
            AgentInboxItem.id > after, AgentInboxItem.origin == "system_recovery")
            .order_by(AgentInboxItem.id))).all()
    return next((item_id for item_id, origin_ref in rows
                 if (origin_ref or {}).get("entrypoint") == RESUME_ENTRYPOINT), None)


async def running_tasks(session_id: str, user_id: str, inbox_id: str | None) -> list[dict]:
    """The tasks the run behind ``inbox_id`` created or gave work to that are still at it: id and title each."""
    from sqlalchemy import JSON, type_coerce
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from db.models.assistant import AssistantCommand, AssistantTask
    if not inbox_id:
        return []
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, inbox_id)
        if item is None or item.user_id != user_id or item.session_id != session_id or not item.run_id:
            return []
        rows = (await db.execute(select(AssistantTask.id, AssistantTask.title).join(
            AssistantCommand, AssistantCommand.target_id == AssistantTask.id).where(
            AssistantCommand.assistant_session_id == session_id, AssistantCommand.actor_user_id == user_id,
            AssistantCommand.target_type == "task", AssistantCommand.action.in_(WORK_ACTIONS),
            type_coerce(AssistantCommand.source_ref, JSON)["run_id"].as_string() == item.run_id,
            AssistantTask.user_id == user_id, AssistantTask.observed_state.in_(WORKING))
            .order_by(AssistantCommand.created_at))).all()
    return list({task_id: {"task_id": task_id, "title": title} for task_id, title in rows}.values())


async def reply_text(session_id: str, message_id: str, user_id: str) -> str:
    """The answer's own text parts: final ones when the reply marked them, never tool narration."""
    from db.base import get_db_session
    from db.models.message import Message
    from db.models.part import Part
    from memory.redaction import redact_credentials
    async with get_db_session() as db:
        parts = list((await db.scalars(select(Part).join(Message, Message.id == Part.message_id).where(
            Part.message_id == message_id, Part.session_id == session_id, Part.user_id == user_id,
            Part.type == "text", Message.role == "assistant").order_by(Part.created_at, Part.id))).all())
    texts = [(part.data.get("channel"), str(part.data.get("text") or "")) for part in parts
             if not part.data.get("ignored") and not part.data.get("synthetic")]
    chosen = ([text for channel, text in texts if channel == "final"]
              or [text for channel, text in texts if channel != "commentary"])
    return redact_credentials("\n".join(text for text in chosen if text.strip()))


async def main_session(user_id: str, workspace_id: str | None) -> tuple[str | None, str | None]:
    """(workspace, main session ID): no workspace without access, no session before it is created.

    Single-user mode has no ticket workspace; it uses the account's default one like the HTTP routes.
    """
    from assistant.policy import AssistantError
    from assistant.service import get_main_session
    from db.repository.user_repo import PgUserRepo
    workspace_id = workspace_id or ((await PgUserRepo().get(user_id)) or {}).get("default_workspace_id")
    if not workspace_id:
        return None, None
    try:
        main = await get_main_session(user_id=user_id, workspace_id=workspace_id)
    except AssistantError:
        return None, None
    return workspace_id, (main.id if main is not None else None)
