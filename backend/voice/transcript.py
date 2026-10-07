"""The call as it was said, kept on the server: user words, replies and background notes.

Never sent to a client. It feeds the summaries (mid-call, so long calls stay
small; after hang-up, so the next call can pick up where this one ended) and
the QA debug log. It also tracks the provider session's conversation items in
creation order: after a summary the early ones are deleted in that order.
"""
from dataclasses import dataclass, field
from datetime import datetime

ROLES = {"user": "用户", "assistant": "前台", "note": "后台备注"}
RENDER_CHARS = 400  # one line in a summary request; notes can be long


@dataclass
class Line:
    at: datetime
    role: str  # user / assistant / note
    text: str
    item_id: str = ""


@dataclass
class Item:
    """One item of the live provider session (user audio, reply, note, function call or output)."""
    id: str
    role: str
    type: str


@dataclass
class CallTranscript:
    now: callable = field(default=None, repr=False)  # local wall clock; tests pass a fixed one
    lines: list[Line] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    def add(self, role: str, text: str, item_id: str = "") -> bool:
        """Record one line; a second line for the same item (text and audio transcripts) is ignored."""
        text = " ".join((text or "").split())
        if not text or (item_id and any(line.item_id == item_id and line.role == role for line in self.lines[-8:])):
            return False
        self.lines.append(Line(self._now(), role, text, item_id))
        return True

    def user_lines(self) -> int:
        return sum(line.role == "user" for line in self.lines)

    def item_created(self, item_id: str, role: str, item_type: str) -> None:
        if item_id and all(item.id != item_id for item in self.items):
            self.items.append(Item(item_id, role, item_type))

    def item_deleted(self, item_id: str) -> None:
        self.items = [item for item in self.items if item.id != item_id]

    def early_items(self, keep: int, protected: set[str] = frozenset()) -> list[str]:
        """Item ids older than the last ``keep``, oldest first, never a protected one."""
        early = self.items[:-keep] if keep else list(self.items)
        return [item.id for item in early if item.id not in protected]

    def reset_items(self) -> None:
        """A fresh provider session has no items of its own yet."""
        self.items = []

    def render(self, start: int = 0, end: int | None = None) -> str:
        return "\n".join(f"{line.at:%H:%M} {ROLES[line.role]}：{_bounded(line.text)}"
                         for line in self.lines[start:end])

    def _now(self) -> datetime:
        if self.now is not None:
            return self.now()
        from voice.prompt import local_now
        return local_now()


def _bounded(text: str) -> str:
    return text if len(text) <= RENDER_CHARS else text[:RENDER_CHARS - 1] + "…"
