"""Display names for guided tasks, including older tasks with an entry label."""
from core.ui_language import LANGS
from light.entries import light_entries

ENTRIES = frozenset({"video", "menu", "poster"})


def production_title(session) -> str:
    title = session.title or ""
    entry_id = getattr(session, "light_entry", None)
    if entry_id not in ENTRIES:
        return title
    labels = [light_entries(lang).entry(entry_id).title for lang in LANGS]
    if title and (title not in labels or (getattr(session, "metadata_", None) or {}).get("manual_title")):
        return title

    form = session.light_production or {}
    promotion = form.get("promotion_brief") or {}
    for value in (form.get("poster_title"), form.get("product_name"), form.get("brief"),
                  promotion.get("offer"), form.get("notes")):
        if isinstance(value, str) and (topic := " ".join(value.split())):
            return topic if len(topic) <= 60 else topic[:59] + "…"

    stamp = session.created_at
    return " · ".join(part for part in (
        form.get("shop"), title or labels[0], stamp.strftime("%m-%d %H:%M:%S") if stamp else None,
    ) if part)
