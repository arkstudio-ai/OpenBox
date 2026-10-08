"""What the front desk remembers, read the way the personal assistant's own turn reads it.

Every assistant turn recalls across the user's personal background and their
own live projects (agent/loop.py: ``run_memory_context`` with
``include_all_projects``): the core memories, plus the memories, documents and
knowledge pages that match the question, reranked. The front desk reads the
same with the same authority (user, workspace, main session):

- ``core_memories``: the core memories, in the session prompt from connect
  time on. Measured 2026-10-08: "云杉项目负责人是小李" and "松鼠青柠" were core
  memories the typed assistant answered from, while the front desk only had
  the profile and preference ones and said it knew nothing.
- ``search``: one question across memories, documents and knowledge pages,
  for ``memory_search`` and the recall each utterance starts (voice/router.py).
  The personal-scope keyword search used before missed the project memory.

What comes back is data, never instructions; texts are bounded and stripped of
markdown so the realtime model can speak from them.
"""
import re

from core.log import create_logger

log = create_logger("voice.recall")

CORE_CHARS = 1200       # the core memories in the session prompt
ITEM_CHARS = 200        # one recalled text
SEARCH_LIMIT = 5
KINDS = {"memory": "记忆", "source": "资料", "wiki": "知识页"}


# Knowledge pages cite their sources inline ("[source:ms_@16]"): nothing to speak.
_CITATION = re.compile(r"\[source:[^\]]*\]")


class RecallUnavailable(Exception):
    """Retrieval is switched off for this user, or the call's authority no longer holds."""


async def core_memories(user_id: str, workspace_id: str) -> str:
    """The core memories the assistant reads every turn, most important first, as one line."""
    from core.config import get_config
    from db.base import get_db_session
    from memory.orchestrator import _stable_background
    from memory.policy import resolve_access_scope
    from voice.speech_text import clean
    config = get_config().memory
    if not config.enabled("retrieval_v2", user_id):
        return ""
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    background = await _stable_background(scope, config)
    texts = [clean(item.get("text") or "", limit=ITEM_CHARS).rstrip("。.；;") for item in background["items"]]
    return _bounded("；".join(text for text in texts if text), CORE_CHARS)


async def search(scope, query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """What matches ``query``: [{"text", "from", "time"?}], best first; [] when nothing is relevant."""
    from assistant.commands import _authority
    from core.config import get_config
    from db.base import get_db_session
    from memory.redaction import redact_credentials
    from memory.retrieval import search_memory
    from voice.speech_text import clean
    from voice.tools import when
    config = get_config().memory
    if not config.enabled("retrieval_v2", scope.user_id):
        raise RecallUnavailable("retrieval_off")
    async with get_db_session() as db:
        await _authority(db, user_id=scope.user_id, workspace_id=scope.workspace_id, main_id=scope.main_session_id)
    bundle = await search_memory(query=redact_credentials(query.strip()[:200]), user_id=scope.user_id,
                                 workspace_id=scope.workspace_id, include_all_projects=True, limit=limit,
                                 config=config)
    found = []
    for item in bundle["items"][:limit]:
        text = clean(_CITATION.sub("", item.get("text") or ""), limit=10_000)
        if not text:
            continue
        entry = {"text": _bounded(text, ITEM_CHARS), "from": KINDS.get(item.get("kind"), "记忆")}
        if moment := when(item.get("valid_from")):
            entry["time"] = moment
        found.append(entry)
    return found


def _bounded(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
