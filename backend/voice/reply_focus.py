"""Which pending decision a reply is about, by meaning instead of wording.

Exact names and a notice's own phrasing (voice/spoken_cards.py) miss a short
paraphrase, a translated task name or a reply that never names its task
(reported 2026-10-27: "Would you like the gulls at the end or in the
middle?" kept playing after its card was answered on screen). The reply is
compared by meaning with each decision it could be asking for. An embedding
comparison answers in about a second, while the reply is still streaming, so
it is usually ready before the user acts; the small model's reading of the
finished reply follows a few seconds later and replaces it. Either one
failing leaves the exact matches only. This only chooses which speech an
answered card stops; it never decides anything for the user.
"""
import asyncio
import json
import math
import re
import unicodedata
from collections import OrderedDict

from core.log import create_logger

log = create_logger("voice.focus")

# The closest decision by meaning, when close enough and clearly closer than
# the next (measured 2026-10-27, qwen3.7-text-embedding, short English and
# Chinese replies about Chinese cards: replies asking a decision 0.51-0.80,
# 0.09-0.33 ahead of the next; small talk and replies about other tasks at
# most 0.46, under 0.03 ahead).
MEANING_FLOOR, MEANING_LEAD = 0.5, 0.08
# Tasks too close by meaning to tell apart are each kept, this many at most
# (measured 2026-10-27: "the sunset waves video" scored 0.660 against
# 日落海浪 and 0.642 against 日出海浪): a reply possibly about a task stops
# when it changes, rather than telling an outdated state.
CLOSE_TASKS = 3
# The user's words name a task when they share at least this many pieces
# (adjacent Chinese character pairs, Latin words of three letters or more)
# with its title and project, more than with any other task.
NAMED_PIECES = 2
# The reply alone is embedded while it plays (measured 2026-10-27: 0.7-1.0 s
# for one text, 2-4 s for a batch of descriptions, which is why descriptions
# are embedded beforehand, as cards and tasks appear).
MEANING_SECONDS, PREPARE_SECONDS, READING_SECONDS = 3.0, 12.0, 8.0
DESCRIPTION_CHARS, REPLY_CHARS, WORDS_CHARS = 360, 1200, 400
VECTORS = 256

READING = ("You label which pending decisions a voice assistant's reply asks the user to make, or explains or "
           "presents the choices of. Reply with JSON only: {\"asks\": [labels]}. Include a decision only when the "
           "reply itself is about it; a status update, small talk, reporting a choice the user already made, or a "
           "different decision does not count. Task names may be translated, shortened or left out.")


def describe(decision: dict) -> str:
    """What a card or task question asks, in its own words: its task, its question and its options."""
    if "questions" in decision:  # a task question (assistant/request_answers.py)
        task = decision.get("session_title") or decision.get("project_name") or ""
        asked = "；".join(" ".join(filter(None, (item.get("header"), item.get("question"))))
                         for item in decision.get("questions") or [])
        options = "；".join(option for item in decision.get("questions") or [] for option in item.get("options") or [])
    else:  # a main-session card (assistant/confirmations.py)
        task = decision.get("task_title") or ""
        asked = "：".join(filter(None, (decision.get("header"), (decision.get("prompt") or "").split("\n")[0])))
        options = "；".join(decision.get("options") or [])
    text = (f"任务「{task}」" if task else "") + f"等待决定：{asked}" + (f"。选项：{options}" if options else "")
    return text[:DESCRIPTION_CHARS]


def named(words: str, tasks: dict[str, str]) -> str | None:
    """The one task the user's own words name: {key: its title and project} → its key, if one is clear.

    Meaning cannot tell near-duplicates apart (measured 2026-10-27: the
    question "日落海浪那个视频现在进展到哪了？" scored higher against
    日出海浪 than against 日落海浪), while the words carry the task's own
    name: "日落海浪" shares four pieces with its title, two with the other.
    A word shared by every task ("视频") never decides alone.
    """
    said = _pieces(words)
    if not said:
        return None
    scored = sorted(((len(said & _pieces(text)), key) for key, text in tasks.items()), reverse=True)
    if not scored or scored[0][0] < NAMED_PIECES or len(scored) > 1 and scored[1][0] >= scored[0][0]:
        return None
    return scored[0][1]


def _pieces(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    pieces = set(re.findall(r"[a-z0-9]{3,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        pieces.update(run[index:index + 2] for index in range(len(run) - 1))
    return pieces


def describe_task(row: dict) -> str:
    """Which task a work row is: its title and project, never its changing state."""
    title, project = row.get("title") or "", row.get("project") or ""
    return (f"任务「{title}」" + (f"（项目：{project}）" if project and project != title else ""))[:DESCRIPTION_CHARS]


class ReplyFocus:
    """One per call: each decision's description is embedded once, replies are compared as they stream."""

    def __init__(self, *, embed=None, read=None):
        self._embed = embed or _embed
        self._read = read or _read
        self._vectors: OrderedDict[str, list[float]] = OrderedDict()
        self._preparing: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def missing(self, decisions: dict) -> list[str]:
        return [text for _, text in decisions.values() if text not in self._vectors]

    def soon(self, *groups: dict) -> None:
        """Embed the descriptions not embedded yet in the background, for the replies that will need them."""
        texts = [text for group in groups for text in self.missing(group) if text not in self._preparing]
        if texts:
            self._preparing.update(texts)
            task = asyncio.get_running_loop().create_task(self._prepare(list(dict.fromkeys(texts))))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def prepare(self, *groups: dict) -> None:
        """Embed new descriptions now (tests and warm-up); a reply never waits on this."""
        if texts := list(dict.fromkeys(text for group in groups for text in self.missing(group))):
            await self._prepare(texts)

    async def _prepare(self, texts: list[str]) -> None:
        try:
            async with asyncio.timeout(PREPARE_SECONDS):
                self._keep(texts, await self._embed(texts))
        except Exception as exc:
            log.info("voice reply focus unavailable by=meaning stage=prepare error=%s", type(exc).__name__)
        finally:
            self._preparing.difference_update(texts)

    async def by_meaning(self, reply: str, decisions: dict) -> dict[str, str]:
        """{identity: version} of the one decision the reply is closest to; {} when none clearly is."""
        return (await self.place(reply, decisions))[0]

    async def place(self, reply: str, *groups: dict, close: tuple[int, ...] = ()) -> list[dict[str, str]]:
        """For each group of {identity: (version, description)}: the one the reply is clearly closest to, if any.

        Decisions and tasks are separate groups: a reply about a task's card is
        also about that task, and neither may crowd the other out. In the
        groups numbered in ``close``, candidates too close to tell apart are
        each kept (CLOSE_TASKS).
        """
        placed = [{} for _ in groups]
        if not any(groups) or not reply.strip():
            return placed
        self.soon(*groups)  # a description not embedded yet joins the next comparison, never this one
        if not any(text in self._vectors for group in groups for _, text in group.values()):
            return placed
        try:
            async with asyncio.timeout(MEANING_SECONDS):
                [vector] = await self._embed([reply[:REPLY_CHARS]])
        except Exception as exc:
            log.info("voice reply focus unavailable by=meaning error=%s", type(exc).__name__)
            return placed
        for index, group in enumerate(groups):
            ranked = sorted(((_cosine(vector, self._vectors[text]), identity, version)
                             for identity, (version, text) in group.items() if text in self._vectors), reverse=True)
            if ranked:
                best, identity, version = ranked[0]
                runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
                if best >= MEANING_FLOOR and best - runner_up >= MEANING_LEAD:
                    placed[index] = {identity: version}
                elif best >= MEANING_FLOOR and index in close:
                    placed[index] = {identity: version for score, identity, version in ranked[:CLOSE_TASKS]
                                     if best - score < MEANING_LEAD}
        return placed

    async def by_reading(self, reply: str, words: str, decisions: list[tuple[str, str, str]]) -> dict[str, str] | None:
        """The small model's reading: {identity: version} of each decision the reply asks; None when unavailable.

        ``decisions`` are (identity, version, description); a decision answered
        moments ago may be listed too, since a reply begun before the answer
        reached it can still ask it.
        """
        if not decisions or not reply.strip():
            return {}
        labels = {str(index): (identity, version) for index, (identity, version, _) in enumerate(decisions, 1)}
        body = json.dumps({"user_said": words[:WORDS_CHARS], "pending_decisions": [
            {"label": str(index), "about": text} for index, (_, _, text) in enumerate(decisions, 1)],
            "assistant_reply": reply[:REPLY_CHARS]}, ensure_ascii=False)
        try:
            answer = await asyncio.wait_for(self._read(READING, body), READING_SECONDS)
            chosen = json.loads(_unfenced(answer)).get("asks")
        except Exception as exc:
            log.info("voice reply focus unavailable by=reading error=%s", type(exc).__name__)
            return None
        if not isinstance(chosen, list):
            return None
        return dict(labels[str(label)] for label in chosen if str(label) in labels)

    def _keep(self, texts, vectors) -> None:
        for text, vector in zip(texts, vectors):
            self._vectors[text] = vector
            self._vectors.move_to_end(text)
        while len(self._vectors) > VECTORS:
            self._vectors.popitem(last=False)


def _cosine(first, second) -> float:
    dot = sum(a * b for a, b in zip(first, second))
    norms = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return dot / norms if norms else 0.0


def _unfenced(answer: str) -> str:
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _embed(texts: list[str]) -> list[list[float]]:
    from core.config import get_config
    from memory.index.embedding import BailianEmbedding
    vectors, _ = await BailianEmbedding(get_config().memory).embed(texts)
    return vectors


async def _read(system: str, text: str) -> str:
    from agent.model_resolve import resolve
    from core.config import get_config
    from voice.summary import complete
    model, _ = resolve(get_config().voice.summary_model, get_config(), context="voice reply focus")
    return await complete(model, system, text, READING_SECONDS)
