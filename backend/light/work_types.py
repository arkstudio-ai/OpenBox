"""The Light jobs the personal assistant can hand to a helper (docs/LIGHT_MODE_IMPL_PLAN.md §11).

A ``tasks.submit`` ``work_type`` named after a Light home entry starts a helper
room that loads that entry's cloud skill and never touches the desktop. The
names are the entry ids in ``light/entries.zh-CN.json`` (``video`` is ``light_video``
so it cannot be confused with the main line's ``video`` = "format undecided"),
so nothing has to be renamed when the line is merged back.
"""
from __future__ import annotations

from light.entries import light_entries

#: work_type -> Light home entry id
ENTRY_FOR_WORK_TYPE = {"light_video": "video", "menu": "menu", "poster": "poster", "footage_edit": "footage_edit"}
#: work_type -> workflow (= the entry's skill name under ``backend/.openbox/skills/``)
WORKFLOWS = {"light_video": "light-video", "menu": "menu-maker", "poster": "poster-maker", "footage_edit": "footage-edit"}
LIGHT_WORK_TYPES = frozenset(WORKFLOWS)


def entry_for(work_type: str | None):
    entry_id = ENTRY_FOR_WORK_TYPE.get(work_type or "")
    return light_entries().entry(entry_id) if entry_id else None


def default_tier(work_type: str | None) -> str | None:
    """The entry's ``default_tier`` (``low`` for the Light video); None for jobs that are not a video."""
    entry = entry_for(work_type)
    return entry.default_tier if entry is not None else None
