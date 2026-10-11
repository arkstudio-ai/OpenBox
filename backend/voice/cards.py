"""Native semantic card decisions with scoped, versioned, idempotent writes.

The model resolves references and choices from the conversation. This module
checks that the exact card was shown, the human spoke after it, the version and
option are valid, and the owned checkpoint still accepts an answer. No sentence
is classified here and no transcript alone can answer or cancel a card.
"""
import asyncio
import json
import hashlib
import time

from core.log import create_logger

log = create_logger("voice.cards")

WORDS_WAIT_SECONDS, WORDS_STEP_SECONDS = 2.5, 0.1  # the user's words may land just after the model's call
MAX_QUESTIONS = 5
WATCH_LIMIT = 30
POLL_SECONDS, POLL_TIMEOUT_SECONDS = 2, 4
WHAT_CHARS, IMPACT_CHARS = 300, 200
KINDS = {"assistant_confirm": "确认", "assistant_send": "确认发送", "memory_proposal": "记忆",
         "assistant_placement": "安排位置"}

def version(card: dict) -> str:
    """Content identity, independent of wording interpretation or list position."""
    fields = {key: card.get(key) for key in (
        "card_id", "kind", "action", "prompt", "impact", "options", "expires_at", "generation")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def option_id(index: int) -> str:
    return f"option_{index + 1}"


class CardDesk:
    """The cards this call has given the model, by short handle; a handle is all it answers with.

    ``heard`` returns (user utterances transcribed so far, the latest one's
    words); it is how the desk knows the user spoke after a card was shown.
    """

    def __init__(self, heard, clock=time.monotonic):
        self.heard, self.clock = heard, clock
        self._versions: dict[str, str] = {}
        self.answer_lock = asyncio.Lock()
        self._handles: dict[str, str] = {}   # handle → card id
        self._shown: dict[str, int] = {}     # card id → utterances heard when the model first got its content
        self.answered: dict[str, str] = {}   # card id → choice
        self._inactive: set[str] = set()    # absent from the latest snapshot; not necessarily answered
        self.answers: list[dict] = []        # answered in this call, waiting for the bridge to follow
        self.questions: dict[str, dict] = {}  # task questions actually offered in this call
        self._question_shown: dict[str, int] = {}

    def show(self, card: dict) -> str:
        card_id = card["card_id"]
        changed = card_id in self._versions and self._versions[card_id] != version(card)
        self._versions[card_id] = version(card)
        self._inactive.discard(card_id)
        handle = self.handle(card_id)
        if handle is None:
            handle = str(len(self._handles) + 1)
            self._handles[handle] = card_id
        count = self.heard()[0]
        if changed:
            self._shown[card_id] = count
        else:
            self._shown.setdefault(card_id, count)
        return handle

    def handle(self, card_id: str) -> str | None:
        return next((handle for handle, saved in self._handles.items() if saved == card_id), None)

    def card_id(self, handle: str) -> str | None:
        handle = str(handle or "").strip().lstrip("#")
        return self._handles.get(handle) or (handle if handle in self._shown else None)

    def open(self) -> list[str]:
        """Shown and not answered in this call (it may have been answered on screen since)."""
        return [card_id for card_id in self._shown if card_id not in self.answered and card_id not in self._inactive]

    async def words_after(self, card_id: str) -> str | None:
        """What the user said after the card was shown, waiting briefly for the transcript; None if nothing."""
        deadline = self.clock() + WORDS_WAIT_SECONDS
        while True:
            count, words = self.heard()
            if count > self._shown.get(card_id, count) and words.strip():
                return words
            if self.clock() >= deadline:
                return None
            await asyncio.sleep(WORDS_STEP_SECONDS)

    def show_question(self, item: dict, lang: str) -> dict:
        value = spoken_question(item, lang)
        if identity := value.get("request_id"):
            self.questions[identity] = value
            self._question_shown.setdefault(identity, self.heard()[0])
        return value

    def reconcile(self, main_cards: list[dict], questions: list[dict]) -> None:
        """A choice made on another client or an expired generation is no longer answerable here."""
        current = {card["card_id"] for card in main_cards}
        self._inactive = set(self._shown) - current
        current = {item["id"] for item in questions}
        self.questions = {key: value for key, value in self.questions.items() if key in current}
        self._question_shown = {key: count for key, count in self._question_shown.items() if key in current}

    def question_context(self, identity: str = "") -> list[dict]:
        """Stable targets survive a short answer like '第二个'. Long forms are re-read by ID.

        The inbox origin reference is limited to 8192 ASCII JSON characters;
        leave room for the user's words, the brief and the last spoken lines.
        """
        candidates = [value for key, value in self.questions.items()
                      if key == identity]
        candidates = candidates[-MAX_QUESTIONS:]
        if len(json.dumps(candidates, ensure_ascii=True)) <= 3500:
            return candidates
        return [{"request_id": value["request_id"], "session_id": value["session_id"],
                 "questions_omitted": True} for value in candidates]



def _bounded(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _spoken(text: str, lang: str, limit: int) -> str:
    from voice.speech_text import clean
    return clean(text or "", lang, limit=limit)


def spoken_card(card: dict, handle: str, lang: str = "zh") -> dict:
    """One main-session card as the model reads it: what will be done, its impact, the exact options."""
    what = str(card.get("prompt") or "").split("\n影响：", 1)[0]
    return {"card": handle, "kind": KINDS.get(card.get("kind"), "确认"), "title": card.get("header") or "",
            "what": _spoken(what, lang, WHAT_CHARS),
            **({"impact": _spoken(card["impact"], lang, IMPACT_CHARS)} if card.get("impact") else {}),
            "version": version(card),
            "options": [{"id": option_id(index), "label": label,
                         **({"fields": ["project_name"]} if card.get("kind") == "assistant_placement" and index == 1 else {})}
                        for index, label in enumerate(card.get("options") or [])],
            "high_risk": bool(card.get("high_risk"))}


def spoken_question(item: dict, lang: str = "zh") -> dict:
    # Keep every question and exact option label. Truncating a label can change its meaning or make
    # the assistant unable to submit it; these are data, the front desk supplies the spoken summary.
    value = {"request_id": item.get("id") or "", "session_id": item.get("session_id") or "",
             "conversation": item.get("session_title") or "", "project": item.get("project_name") or "",
             "questions": [{"number": index, "header": question.get("header") or "",
                            "question": question.get("question") or "",
                            "options": list(question.get("options") or []),
                            "multiple": bool(question.get("multiple")), "custom": question.get("custom", True),
                            "allow_attachments": bool(question.get("allow_attachments"))}
                           for index, question in enumerate(item.get("questions") or [], start=1)],
             "assistant_may_answer": bool(item.get("assistant_may_answer")),
             "high_risk": bool(item.get("high_risk")),
             # What kind of card and where the user acts on it (assistant/request_answers.py list_waiting).
             "kind": item.get("card_kind") or "question", "link": item.get("link") or "",
             **({"host": item["host"]} if item.get("host") else {})}
    if value["kind"] == "desktop_takeover":
        value["answer_how"] = takeover_how(item)
    elif item.get("assistant_may_answer"):
        value["answer_how"] = "用户明确回答后，用 assistant_ask 结合任务和问题转交选择；保留原话核对，不替用户补选"
    else:
        value["answer_how"] = "要用户自己在屏幕上处理"
    return value


def takeover_how(item: dict) -> str:
    """Where the user finishes a desktop takeover, said so they can do it: the conversation, the
    cloud desktop (or their own browser) and the button to press after."""
    title = item.get("session_title") or "那个对话"
    done = item.get("done_option") or "我已完成，继续"
    site = f"（{item['host']}）" if item.get("host") else ""
    if item.get("browser") == "extension":
        where = f"在你自己的浏览器里完成验证{site}，再回到『{title}』点『{done}』"
    else:
        where = f"在『{title}』里打开云桌面完成验证{site}，再点『{done}』"
    return (f"这是网页上的人机验证，只能用户自己做：告诉用户{where}。用户在对话中表示已完成时，"
            f"用 assistant_ask 把这句原话转交，个人助理会替他选『{done}』；没说完成就不要转交")


async def waiting(scope, *, limit: int = WATCH_LIMIT) -> tuple[list[dict], list[dict]]:
    """Same owner/workspace/main-session checks as the assistant; also used by the call's watcher."""
    from assistant.confirmations import pending_cards
    from assistant.request_answers import list_waiting
    # A call can hang up during either read. TaskGroup waits for BOTH sessions to close on cancellation;
    # gather can return the first CancelledError while its sibling still holds a DB transaction open.
    async with asyncio.TaskGroup() as group:
        main_cards = group.create_task(pending_cards(scope.user_id, scope.workspace_id, scope.main_session_id))
        questions = group.create_task(list_waiting(user_id=scope.user_id, workspace_id=scope.workspace_id,
                                                  main_id=scope.main_session_id, limit=limit))
    return main_cards.result(), questions.result()


async def pending(scope, arguments: dict) -> dict:
    """cards_pending: the main session's cards (answerable here) and questions waiting in task conversations."""
    cards, questions = await waiting(scope)
    scope.desk.reconcile(cards, questions)
    shown = [spoken_card(card, scope.desk.show(card), scope.lang) for card in cards]
    task_questions = [scope.desk.show_question(item, scope.lang) for item in questions[:MAX_QUESTIONS]]
    if not shown and not task_questions:
        return {"status": "none"}
    return {"status": "ok", "cards": shown, "task_questions": task_questions,
            "waiting_count": len(questions),
            "how": "结合对话说明待决定事项和影响；由用户决定，使用返回的卡片标识和选项 id 提交。"}



async def answer(scope, arguments: dict) -> dict:
    """The native model interprets the utterance; the server validates scope and state."""
    async with scope.desk.answer_lock:
        return await _answer(scope, arguments)


async def _answer(scope, arguments: dict) -> dict:
    from assistant.confirmations import pending_cards, card_status
    from question import question as questions
    from voice.tools import CardAnswerArguments
    try:
        args = CardAnswerArguments.model_validate(arguments)
    except ValueError:
        return {"status": "invalid_arguments", "this_call_applied": False}
    desk = scope.desk
    card_id = desk.card_id(args.card)
    if card_id is None:
        return {"status": "unknown_card", "this_call_applied": False, "next": "cards_pending"}
    if card_id in desk.answered:
        return {"status": "already_answered", "this_call_applied": False,
                "execution_confirmed": False, "note": "本次未执行操作；回答记录不代表业务完成。"}
    card = next((item for item in await pending_cards(scope.user_id, scope.workspace_id, scope.main_session_id)
                 if item["card_id"] == card_id), None)
    if card is None:
        state = await card_status(scope.user_id, scope.workspace_id, scope.main_session_id, card_id)
        return {"status": state, "this_call_applied": False, "execution_confirmed": False,
                "note": "本次未应用回答，不代表任务已完成。不能换一张卡片代答。"}
    if args.version != desk._versions.get(card_id) or args.version != version(card):
        return {"status": "conflict", "this_call_applied": False, "next": "cards_pending"}
    chosen = next((i for i in range(len(card["options"])) if option_id(i) == args.option_id), None)
    if chosen is None:
        return {"status": "invalid_choice", "this_call_applied": False, "next": "cards_pending"}
    if args.project_name is not None and (card["kind"] != "assistant_placement" or chosen != 1):
        return {"status": "invalid_arguments", "this_call_applied": False,
                "note": "project_name 仅适用于新建项目选项。"}
    words = (scope.transcript if scope.utterance_index > desk._shown.get(card_id, scope.utterance_index)
             and scope.transcript.strip() else None) if scope.utterance_index is not None else await desk.words_after(card_id)
    if words is None:
        return {"status": "need_user_answer", "this_call_applied": False,
                "note": "卡片展示后还没有用户回答，不能把模型或后台消息作为用户的决定。"}
    from voice.assistant_link import latest_inbox_id
    before = await latest_inbox_id(scope.main_session_id, scope.user_id)
    choice = card["options"][chosen]
    try:
        receipt = await questions.reply(card_id, [[choice]], user_id=scope.user_id,
            **({"placement_project_name": args.project_name} if args.project_name is not None else {}))
    except questions.QuestionGone as exc:
        return {"status": "already_answered" if exc.status == "answered" else exc.status,
                "this_call_applied": False, "execution_confirmed": False}
    except questions.QuestionConflict:
        return {"status": "conflict", "this_call_applied": False, "execution_confirmed": False}
    if receipt.get("applied") is False:
        return {"status": "already_answered", "this_call_applied": False, "execution_confirmed": False}
    # These card types publish their cancellation option last. This is card structure, not speech parsing.
    declined = chosen == len(card["options"]) - 1
    desk.answered[card_id] = choice
    desk.answers.append({"card_id": card_id, "choice": choice, "words": words, "kind": card["kind"],
                         "declined": declined, "what": spoken_card(card, "", scope.lang)["what"], "after": before})
    log.info("voice card answered call=%s card=%s kind=%s declined=%s", scope.call_id, card_id, card["kind"], declined)
    return {"status": "decision_recorded", "request_id": card_id, "option_id": args.option_id,
            "this_call_applied": True, "decision": "declined" if declined else "selected",
            "operation_state": "not_started" if declined else "continuation_pending", "execution_confirmed": False,
            "execution_result": None,
            "note": ("已记录取消决定，不会据此启动操作。" if declined else
                     "本工具仅记录选择。发送、创建等业务尚未返回执行结果；告知用户决定已收到，等待后台结果。")}
