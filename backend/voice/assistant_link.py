"""The personal-assistant side of ``assistant_ask``: each call is one main-session turn.

A voice turn is accepted exactly like typed input (``inputs.accept_turn``),
marked ``entrypoint=assistant_voice`` so the projection asks for a reply that
works on the phone. The reply is the settled Inbox item's result message.
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
    speech: str = ""
    late: bool = False
    delivery: str = ""            # queued / creating / delivering / done
    attempts: int = 0
    settled: float | None = None  # bridge clock, for the turn's log line
    finished: float | None = None


def turn_outcome(ref: VoiceTurnRef, *, heard: bool = False, pending: bool = False) -> str:
    """The VoiceTurn outcome once the call is done with a turn."""
    if pending or ref.status == "timeout":
        return "late"  # the assistant still answers, in the conversation
    if ref.status != "ok":
        return "failed"
    return "delivered" if heard else "cancelled"


class AssistantLink:
    def __init__(self, *, call_id: str, user_id: str, workspace_id: str, main_session_id: str,
                 lang: str, turn_timeout: float):
        self.call_id, self.user_id, self.workspace_id = call_id, user_id, workspace_id
        self.main_session_id, self.lang, self.turn_timeout = main_session_id, lang, turn_timeout
        self.count = 0
        self.closed = False  # set when the call ends; observers stop at their next step

    async def start(self, ref: VoiceTurnRef) -> None:
        """Accept the user's words as a main-session turn and record the voice turn."""
        from agent.inbox import schedule_inbox_wake
        from assistant import inputs
        self.count += 1
        try:
            receipt = await inputs.accept_turn(
                user_id=self.user_id, workspace_id=self.workspace_id, main_id=self.main_session_id,
                client_id=f"voice:{self.call_id}:{self.count}", text=ref.text,
                entrypoint=VOICE_ENTRYPOINT, extra_ref={"voice_call_id": self.call_id})
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
            speech = clean(text, self.lang) or phrases.phrase_text("result_in_text", self.lang)
            return {"status": "ok", "speech": speech}
        await self.record(ref, settled=True, outcome="failed", result_message_id=receipt.result_message_id)
        return {"status": "failed", "speech": phrases.speech_text("failed", self.lang)}

    async def done(self, ref: VoiceTurnRef, outcome: str, *, delivered: bool = False) -> None:
        """A turn's last word in this call: its outcome and one log line (times, never words)."""
        await self.record(ref, outcome=outcome, delivered=delivered)
        settle = f"{ref.settled - ref.requested:.1f}" if ref.settled is not None else "-"
        deliver = (f"{ref.finished - ref.settled:.1f}" if delivered and None not in (ref.settled, ref.finished)
                   else "-")
        log.info("voice turn=%s call=%s inbox=%s settle_s=%s deliver_s=%s outcome=%s",
                 ref.id, self.call_id, ref.inbox_id, settle, deliver, outcome)

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
