"""Cards waiting for the user, read out and answered inside a call (docs/ASSISTANT_VOICE_FIX_PLAN.md §1.3).

Two kinds. The main session's own cards (assistant/confirmations.py): a
confirmation before a high-risk action, a send into a shared conversation, a
memory to keep. The front desk reads one aloud and answers it for the user
with ``cards_answer``, which makes the same call as the card's button
(question.question.reply); the suspended tool call then goes on and its
result comes back to the call as a note (voice/turns.py). Questions waiting in
the user's task conversations are listed too, read only: an answer is the
user's words handed to the assistant (``assistant_ask`` → requests.answer,
which shows a confirmation card itself when the answer is high-risk).

Both sources are also polled while the call is connected (api/voice.py), so
waiting for user input is announced without first asking for cards_pending.
Task forms keep all fields and one stable ID in the assistant's voice context.

Whatever the model does, a card is only answered when
- its handle ("1", "2", …) was issued in this call together with the card's
  content (a cards_pending output or a result note): the model had the words;
- the user spoke after that, and for any choice but declining, those words are
  a clear yes (确认、可以、删吧…) with no refusal, hesitation or question in
  them: silence, "等等" or "什么意思" are no consent;
- the yes came within two utterances of the card being (re)read: a "好的"
  about something else later on is not taken for it;
- the choice is one of the card's option labels, exactly.
A card is answered once; an answered or expired card says so. A clear "no"
right after a card was read declines it on the server (``decline``): the
front desk tends to just say "不删了", and a card left open would wait on
screen for an answer the user already gave.
"""
import asyncio
import json
import re
import time

from core.log import create_logger

log = create_logger("voice.cards")

#: Choices that decline: nothing happens, so no consent is needed.
DECLINE = frozenset({"取消", "不用记"})
WORDS_WAIT_SECONDS, WORDS_STEP_SECONDS = 2.5, 0.1  # the user's words may land just after the model's call
MAX_QUESTIONS = 5
WATCH_LIMIT = 30
POLL_SECONDS, POLL_TIMEOUT_SECONDS = 2, 4
FRESH_UTTERANCES = 2  # a yes counts within this many user utterances after the card was last read
WHAT_CHARS, IMPACT_CHARS = 300, 200
KINDS = {"assistant_confirm": "确认", "assistant_send": "确认发送", "memory_proposal": "记忆"}

_YES = re.compile(r"确认|确定|同意|没问题|就这样|可以|好的|行吧|删吧|删掉吧|删了吧|停掉吧|停了吧|执行吧|去做吧|发吧|发送吧"
                  r"|答吧|选吧|是的|对的|记住|记下|go ahead|do it|\bconfirm|\byes\b|\byeah\b|\byep\b|\bsure\b"
                  r"|\bok(?:ay)?\b", re.IGNORECASE)
_YES_ALONE = {"好", "行", "对", "是", "嗯", "嗯嗯", "删", "要", "记"}
_NO = re.compile(r"不要|不用|不行|不对|不是|不可以|不同意|不确定|不好|不了|别|算了|取消|等等|等一下|等下|稍等|再想想|先不"
                 r"|\bno\b|\bnot\b|don'?t|\bwait\b|\bcancel", re.IGNORECASE)
_DECLINES = re.compile(r"算了|不删|不要删|别删|不用删|取消|先不|不用了|不要了|不做了|不发了|不答了|不用记|不要记|不记"
                       r"|\bno\b|\bcancel|never ?mind|don'?t", re.IGNORECASE)
_QUESTION = re.compile(r"[?？]|吗|什么|为什么|怎么|哪|多少|啥|\bwhat\b|\bwhy\b|\bhow\b|\bwhich\b", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[\s，。！？、；：,.!?;:…~～\-—\"'“”‘’（）()]+")
_PARTICLES = "啊吧呀呢哦嘛的了"


def consents(words: str | None) -> bool:
    """A clear yes: a yes word, and no refusal, hesitation or question anywhere in the words."""
    text = (words or "").strip()
    if not text or _NO.search(text) or _QUESTION.search(text):
        return False
    core = _PUNCTUATION.sub("", text).lower().rstrip(_PARTICLES)
    return bool(_YES.search(text)) or core in _YES_ALONE or core.strip("嗯") in _YES_ALONE


def refuses(words: str | None) -> bool:
    """A clear no to a card ("算了", "先不删了", "取消吧"); a yes anywhere in it makes it no refusal."""
    return bool(_DECLINES.search(words or "")) and not consents(words) and not _YES.search(words or "")


def answers_card(words: str | None) -> bool:
    """Short words that answer a yes/no card ("确认", "好的", "算了") rather than ask for something new."""
    plain = _PUNCTUATION.sub("", words or "").lower()
    return 0 < len(plain) <= 8 and (consents(words) or bool(_NO.search(plain)))


class CardDesk:
    """The cards this call has given the model, by short handle; a handle is all it answers with.

    ``heard`` returns (user utterances transcribed so far, the latest one's
    words); it is how the desk knows the user spoke after a card was shown.
    """

    def __init__(self, heard, clock=time.monotonic):
        self.heard, self.clock = heard, clock
        self._handles: dict[str, str] = {}   # handle → card id
        self._shown: dict[str, int] = {}     # card id → utterances heard when the model first got its content
        self._read: dict[str, int] = {}      # card id → utterances heard when it was last given to the model
        self.answered: dict[str, str] = {}   # card id → choice
        self.answers: list[dict] = []        # answered in this call, waiting for the bridge to follow
        self.questions: dict[str, dict] = {}  # task questions actually offered in this call
        self._question_shown: dict[str, int] = {}
        self._question_read: dict[str, int] = {}

    def show(self, card_id: str) -> str:
        handle = self.handle(card_id)
        if handle is None:
            handle = str(len(self._handles) + 1)
            self._handles[handle] = card_id
        count = self.heard()[0]
        self._shown.setdefault(card_id, count)
        self._read[card_id] = count
        return handle

    def handle(self, card_id: str) -> str | None:
        return next((handle for handle, saved in self._handles.items() if saved == card_id), None)

    def card_id(self, handle: str) -> str | None:
        handle = str(handle or "").strip().lstrip("#")
        return self._handles.get(handle) or (handle if handle in self._shown else None)

    def open(self) -> list[str]:
        """Shown and not answered in this call (it may have been answered on screen since)."""
        return [card_id for card_id in self._shown if card_id not in self.answered]

    def since_read(self, card_id: str) -> int:
        """User utterances since the card was last given to the model."""
        return self.heard()[0] - self._read.get(card_id, self.heard()[0])

    def fresh(self) -> list[str]:
        """Open cards read so recently that the user's words may answer them."""
        return [card_id for card_id in self.open() if self.since_read(card_id) <= FRESH_UTTERANCES]

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
            self._question_read[identity] = self.heard()[0]
        return value

    def reconcile(self, main_cards: list[dict], questions: list[dict]) -> None:
        """A choice made on another client or an expired generation is no longer answerable here."""
        current = {card["card_id"] for card in main_cards}
        for identity in self.open():
            if identity not in current:
                self.answered.setdefault(identity, "")
        current = {item["id"] for item in questions}
        self.questions = {key: value for key, value in self.questions.items() if key in current}
        self._question_shown = {key: count for key, count in self._question_shown.items() if key in current}
        self._question_read = {key: count for key, count in self._question_read.items() if key in current}

    def question_context(self, identity: str = "") -> list[dict]:
        """Stable targets survive a short answer like '第二个'. Long forms are re-read by ID.

        The inbox origin reference is limited to 8192 ASCII JSON characters;
        leave room for the user's words, the brief and the last spoken lines.
        """
        candidates = [value for key, value in self.questions.items()
                      if key == identity or (not identity and self.heard()[0] - self._question_read[key]
                                             <= FRESH_UTTERANCES)]
        candidates = candidates[-MAX_QUESTIONS:]
        if len(json.dumps(candidates, ensure_ascii=True)) <= 3500:
            return candidates
        return [{"request_id": value["request_id"], "session_id": value["session_id"],
                 "questions_omitted": True} for value in candidates]

    def asked_again(self, context: dict | None) -> None:
        """The assistant asks for the next field: keep the same form attached to that answer."""
        for item in (context or {}).get("task_questions") or []:
            if item["request_id"] in self.questions:
                self._question_read[item["request_id"]] = self.heard()[0]

    def question_words(self, identities: list[str]) -> str | None:
        count, words = self.heard()
        if words.strip() and any(key in self.questions and count > self._question_shown[key] for key in identities):
            return words
        return None

    async def question_words_after(self, identities: list[str]) -> str | None:
        """Only an actual new transcript can authorize a task answer, never a model-written request."""
        deadline = self.clock() + WORDS_WAIT_SECONDS
        while True:
            if words := self.question_words(identities):
                return words
            if self.clock() >= deadline:
                return None
            await asyncio.sleep(WORDS_STEP_SECONDS)


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
            "options": list(card.get("options") or []), "high_risk": bool(card.get("high_risk"))}


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
             "high_risk": bool(item.get("high_risk"))}
    if item.get("assistant_may_answer"):
        value["answer_how"] = "用户明确回答后，用 assistant_ask 结合任务和问题转交选择；保留原话核对，不替用户补选"
    else:
        value["answer_how"] = "要用户自己在屏幕上处理"
    return value


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
    shown = [spoken_card(card, scope.desk.show(card["card_id"]), scope.lang) for card in cards]
    task_questions = [scope.desk.show_question(item, scope.lang) for item in questions[:MAX_QUESTIONS]]
    if not shown and not task_questions:
        return {"status": "none"}
    return {"status": "ok", "cards": shown, "task_questions": task_questions,
            "waiting_count": len(questions),
            **({"how": "先念清楚要做什么和影响，再问确认吗；用户明确表态后用 cards_answer 选卡片上的选项"} if shown else {})}


async def answer(scope, arguments: dict) -> dict:
    """cards_answer: the user's clear answer to a card this call has read out."""
    from assistant.confirmations import pending_cards
    from question import question as questions
    desk = scope.desk
    card_id = desk.card_id(arguments.get("card") or arguments.get("card_id"))
    choice = str(arguments.get("choice") or "").strip()
    if card_id is None:
        return {"status": "unknown_card", "hint": "先用 cards_pending 看现在有哪些卡片，念给用户听"}
    card = next((item for item in await pending_cards(scope.user_id, scope.workspace_id, scope.main_session_id)
                 if item["card_id"] == card_id), None)
    if card is None:
        desk.answered.setdefault(card_id, "")
        return {"status": "gone", "hint": "这张卡片已经处理过或过期了，不用再答"}
    if choice not in card["options"]:
        return {"status": "invalid_choice", "options": card["options"], "hint": "choice 要用卡片上的选项原文"}
    words = await desk.words_after(card_id)
    if words is None:
        return {"status": "need_user_answer",
                "hint": "用户还没回答这张卡片：说清楚要做什么、影响是什么，问确认吗，等用户明确表态"}
    if choice not in DECLINE and desk.since_read(card_id) > FRESH_UTTERANCES:
        return {"status": "read_again", "hint": "这张卡片是前面念的：先用 cards_pending 重新念给用户听，再问确认吗"}
    if choice not in DECLINE and not consents(words):
        return {"status": "not_confirmed", "heard": _bounded(words, 60),
                "hint": "用户没有明确同意：再问一次确认吗；用户拒绝就选取消"}
    from voice.assistant_link import latest_inbox_id
    # Read before answering: the turn resumes through a newer Inbox item (voice/assistant_link.py follow).
    before = await latest_inbox_id(scope.main_session_id, scope.user_id)
    try:
        await questions.reply(card_id, [[choice]], user_id=scope.user_id)
    except questions.QuestionGone:
        desk.answered.setdefault(card_id, "")
        return {"status": "gone", "hint": "这张卡片已经处理过或过期了，不用再答"}
    except questions.QuestionConflict:
        desk.answered.setdefault(card_id, "")
        return {"status": "already_answered"}
    desk.answered[card_id] = choice
    desk.answers.append({"card_id": card_id, "choice": choice, "words": words, "kind": card["kind"],
                         "what": spoken_card(card, "", scope.lang)["what"], "after": before})
    log.info("voice card answered call=%s card=%s kind=%s declined=%s", scope.call_id, card_id, card["kind"],
             choice in DECLINE)
    return {"status": "answered", "choice": choice,
            "next": "已取消，不会执行" if choice in DECLINE else "个人助理接着办，办完会以后台备注送来"}


async def decline(scope, card_id: str) -> str | None:
    """The user's clear no right after a card was read: the card's declining option, chosen here."""
    from assistant.confirmations import pending_cards
    from question import question as questions
    desk = scope.desk
    card = next((item for item in await pending_cards(scope.user_id, scope.workspace_id, scope.main_session_id)
                 if item["card_id"] == card_id), None)
    choice = next((option for option in (card or {}).get("options") or [] if option in DECLINE), None)
    if card is None or choice is None or card_id in desk.answered:
        return None
    try:
        await questions.reply(card_id, [[choice]], user_id=scope.user_id)
    except (questions.QuestionGone, questions.QuestionConflict):
        desk.answered.setdefault(card_id, "")
        return None
    desk.answered[card_id] = choice
    log.info("voice card declined call=%s card=%s kind=%s", scope.call_id, card_id, card["kind"])
    return choice
