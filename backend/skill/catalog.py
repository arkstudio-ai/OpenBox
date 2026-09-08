"""The skill store's catalogue: skills and MCP servers a person can install.

Every entry here is real and installable — a live git repository or a published
package — because an entry that cannot install is worse than no entry at all.
Versions are deliberately unpinned: npx resolves the current release, and
pinning here would rot silently.

Some skills only work with an MCP server behind them: their instructions call
tools that do not exist until that server is connected. Those skills declare
``requires_mcp``, listing catalogue MCP ids, so installing one can offer to
bring its servers along instead of leaving the person with a skill that loads
and then fails at its first tool call.

``OPENBOX_CATALOG_URL`` may point at a JSON document with the same shape, which
is merged over this list by id. That is how an operator ships an internal
catalogue without forking the backend.

Every entry also declares a ``listing`` — whether the store puts it on the
shelf. It is only the *default*: the admin console writes ``catalog_overrides``
rows, and those win, so taking an entry down survives a redeploy while a fresh
install still starts from the shelf we shipped.
"""
from __future__ import annotations

import json
import os
from typing import Any

from core.log import create_logger

log = create_logger("skill.catalog")

#: Shelf states a catalogue entry can be in. Entries live in code and never go
#: through submission review, so the author-facing states (``pending`` /
#: ``rejected``) that ``user_skills.listing`` also carries do not apply here.
LISTED = "listed"
DELISTED = "delisted"

#: Publisher name that marks an entry as our own rather than somebody else's.
OFFICIAL_PUBLISHER = "OpenBox"


def catalog_entry_id(kind: str, entry_id: str) -> str:
    """The store-wide key for a catalogue entry: ``skill:web-research``.

    ``skill_installs.catalog_id``, ``catalog_overrides.catalog_id`` and the
    admin console's routes all address entries by this string, so it is spelled
    in exactly one place.
    """
    return f"{kind}:{entry_id}"


def catalog_entry_origin(entry: dict) -> str:
    """Which shelf an entry belongs on: ours, or a third party's."""
    return "official" if entry.get("publisher") == OFFICIAL_PUBLISHER else "third_party"


#: MCP servers. `config` is exactly the body POST /api/agent/mcp accepts, so the
#: store installs one by handing this through unchanged.
MCP_CATALOG: list[dict[str, Any]] = [
    {
        "id": "filesystem",
        "kind": "mcp",
        "name": "filesystem",
        "title": "Filesystem",
        "icon": "📁",
        "description": "Read, write and search files in a directory the server is pointed at.",
        "publisher": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "tags": ["files", "official"],
        "listing": LISTED,
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
            "env": {},
            "timeout": 60,
        },
    },
    {
        "id": "memory",
        "kind": "mcp",
        "name": "memory",
        "title": "Memory",
        "icon": "🧠",
        "description": "A knowledge graph the agent can write to and recall across turns.",
        "publisher": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        "tags": ["memory", "official"],
        "listing": LISTED,
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {},
            "timeout": 60,
        },
    },
    {
        "id": "sequential-thinking",
        "kind": "mcp",
        "name": "sequential-thinking",
        "title": "Sequential Thinking",
        "icon": "🪜",
        "description": "Step-by-step reasoning scaffold for problems worth breaking down.",
        "publisher": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
        "tags": ["reasoning", "official"],
        "listing": LISTED,
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "env": {},
            "timeout": 60,
        },
    },
    {
        "id": "playwright",
        "kind": "mcp",
        "name": "playwright",
        "title": "Playwright",
        "icon": "🎭",
        "description": "Drive a real browser: navigate, click, fill forms and read the page.",
        "publisher": "Microsoft",
        "homepage": "https://github.com/microsoft/playwright-mcp",
        "tags": ["browser", "automation"],
        "listing": LISTED,
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless"],
            "env": {},
            "timeout": 120,
        },
    },
    {
        "id": "deepwiki",
        "kind": "mcp",
        "name": "deepwiki",
        "title": "DeepWiki",
        "icon": "📚",
        "description": "Ask questions about any public GitHub repository's documentation.",
        "publisher": "Devin",
        "homepage": "https://mcp.deepwiki.com",
        "tags": ["docs", "remote"],
        "listing": LISTED,
        "config": {
            "type": "remote",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {},
            "timeout": 60,
        },
    },
    {
        "id": "everything",
        "kind": "mcp",
        "name": "everything",
        "title": "Everything (reference)",
        "icon": "🧪",
        "description": "The protocol's reference server. Useful for checking an MCP setup works.",
        "publisher": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
        "tags": ["testing", "official"],
        "listing": LISTED,
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-everything"],
            "env": {},
            "timeout": 90,
        },
    },
    {
        "id": "firecrawl",
        "kind": "mcp",
        "name": "firecrawl",
        "title": "Firecrawl",
        "icon": "🔥",
        "description": "Crawl and scrape sites into clean markdown. Needs a Firecrawl API key.",
        "publisher": "Firecrawl",
        "homepage": "https://github.com/firecrawl/firecrawl-mcp-server",
        "tags": ["web", "scraping"],
        "listing": LISTED,
        # Declared so the install form asks for the key rather than installing a
        # server that connects and then fails on every call.
        "required_env": [
            {"key": "FIRECRAWL_API_KEY", "label": "Firecrawl API key", "secret": True},
        ],
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "firecrawl-mcp"],
            "env": {},
            "timeout": 90,
        },
    },
]


#: Skills. `install` is the body POST /api/agent/skill/install accepts.
SKILL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "anthropic-skills",
        "kind": "skill",
        "name": "anthropic-skills",
        "title": "Anthropic Skills",
        "icon": "🎁",
        "description": (
            "Anthropic's open skill collection — documents, spreadsheets, slides, "
            "PDFs, brand styling, frontend design and more. Installs as one pack."
        ),
        "publisher": "Anthropic",
        "homepage": "https://github.com/anthropics/skills",
        "tags": ["pack", "official", "documents"],
        # Off the shelf by default: the pack installs a whole collection at
        # once, which is a poor first impression of a store built around
        # single, purposeful skills. An operator can shelve it per
        # deployment from the admin console.
        "listing": DELISTED,
        "requires_mcp": [],
        "install": {"url": "https://github.com/anthropics/skills.git", "name": "anthropic-skills"},
    },
    {
        "id": "web-research",
        "kind": "skill",
        "name": "web-research",
        "title": "Web Research",
        "icon": "🔎",
        "description": (
            "A method for researching a question on the open web: plan the search, "
            "read sources rather than snippets, and cite what the answer rests on."
        ),
        "publisher": "OpenBox",
        "tags": ["research", "web"],
        "listing": LISTED,
        # The workflow tells the model to crawl pages; without Firecrawl it has
        # instructions for tools it does not have.
        "requires_mcp": ["firecrawl"],
        "install": {
            "name": "web-research",
            "content": """---
name: web-research
description: Research a question on the open web — plan the search, read whole sources instead of snippets, and cite what the answer rests on. Use when a question needs current information the model cannot know.
icon: 🔎
requires-mcp: firecrawl
---

# Web Research

## When this applies

A question turns on facts that change — prices, releases, who holds a role,
what a library's current API is. Answering those from memory produces
confident, stale answers, which are worse than saying you need to look.

## Method

1. **Write the claim you are trying to settle** before searching. "Is X faster
   than Y for Z workload" searches very differently from "X vs Y".
2. **Search broadly first, then narrowly.** The first query maps the
   vocabulary; later ones use the terms the sources actually use.
3. **Open the sources.** Use `firecrawl_scrape` on the pages that look load
   bearing. Search result snippets are written to be clicked, not to be
   accurate — a snippet has never been a citation.
4. **Prefer primary sources.** Release notes over a blog about the release;
   the standard over a summary of the standard.
5. **Note the date on everything.** An undated page is a page you cannot rely
   on for anything time-sensitive.

## Reporting

State the answer first, then what it rests on. For each load-bearing claim give
the source and its date. Where sources disagree, say so and say which one you
believe and why — silently picking one hides the disagreement from the reader.

If the search did not settle the question, say that plainly. A clear "the
public sources do not answer this" is a useful result; a confident guess
dressed as research is not.
""",
        },
    },
    {
        "id": "repo-explainer",
        "kind": "skill",
        "name": "repo-explainer",
        "title": "Repo Explainer",
        "icon": "🗺️",
        "description": (
            "Explain an unfamiliar GitHub repository — architecture, entry points "
            "and the paths that matter — using its published documentation."
        ),
        "publisher": "OpenBox",
        "tags": ["code", "onboarding"],
        "listing": LISTED,
        "requires_mcp": ["deepwiki"],
        "install": {
            "name": "repo-explainer",
            "content": """---
name: repo-explainer
description: Explain how an unfamiliar GitHub repository is put together — architecture, entry points, and the code paths that matter. Use when someone asks what a public project does or how to start working in it.
icon: 🗺️
requires-mcp: deepwiki
---

# Repo Explainer

## When this applies

Someone points at a public repository and wants to know what it is, how it is
structured, or where to start changing it.

## Method

1. `read_wiki_structure` on `owner/name` first. The page list is the project's
   own map of itself, and it tells you what the maintainers think matters.
2. `read_wiki_contents` on the two or three pages that bear on the question.
   Read the architecture page even when the question sounds narrow — a narrow
   question asked from the wrong mental model gets a wrong answer.
3. `ask_question` for anything the pages leave open. Ask about mechanism ("how
   does X get from A to B"), not vocabulary.

## Reporting

Lead with what the project is in one sentence, then how it is laid out, then
the answer to what was actually asked.

Name real paths and symbols — `src/foo/bar.ts`, `Widget.render` — so the reader
can go look. A tour with no addresses in it is not a tour.

Say when the documentation is thin or stale rather than filling the gap with a
plausible guess; on an unfamiliar codebase a guess is indistinguishable from a
fact to the person reading it.
""",
        },
    },
    {
        "id": "browser-qa",
        "kind": "skill",
        "name": "browser-qa",
        "title": "Browser QA",
        "icon": "🧭",
        "description": (
            "Verify a web change in a real browser: drive the page, read the "
            "console and network, and report evidence rather than impressions."
        ),
        "publisher": "OpenBox",
        "tags": ["testing", "browser"],
        "listing": LISTED,
        "requires_mcp": ["playwright"],
        "install": {
            "name": "browser-qa",
            "content": """---
name: browser-qa
description: Check a web page or app in a real browser — drive the UI, read console and network errors, and report what actually happened. Use when a change needs verifying in a browser rather than by reading code.
icon: 🧭
requires-mcp: playwright
---

# Browser QA

## When this applies

A change is meant to be visible in a browser and someone wants to know whether
it works. Reading the diff establishes intent; only running it establishes
behaviour.

## Method

1. **Navigate and read the page structure** before clicking anything. Work from
   the accessibility tree rather than a screenshot where you can — it carries
   the text and roles a screenshot only implies.
2. **Check the console and network** on load. Errors that appear before you
   touch anything explain most of what goes wrong afterwards.
3. **Drive the actual path** the change affects — click, type, submit — then
   re-read the page to confirm what changed. An action you did not verify is an
   assumption.
4. **Check the states that break**: empty, loading, error, long text, narrow
   viewport. Bugs live in the states nobody demos.

## Reporting

Say what you did, what you observed, and what that means — in that order.

Quote real console output and real network status codes. "Seems to work" is not
a result; "clicked Save, POST /api/x returned 200, the row appears in the list
after reload" is.

When something fails, report the smallest reproduction you found, not the whole
session.
""",
        },
    },
]


def _merge_remote(entries: list[dict], remote: list[dict]) -> list[dict]:
    """Overlay a remote catalogue onto the built-in one, keyed by id."""
    by_id = {e["id"]: dict(e) for e in entries}
    for item in remote:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        by_id[item["id"]] = {**by_id.get(item["id"], {}), **item}
    return list(by_id.values())


async def _apply_shelf_overrides(entries: list[dict], kind: str) -> None:
    """Overlay the admin console's shelf decisions onto code defaults."""
    from db.base import get_db_session
    from db.models.catalog_override import CatalogOverride
    from sqlalchemy import select

    keys = {catalog_entry_id(kind, e["id"]): e for e in entries if e.get("id")}
    if not keys:
        return
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(CatalogOverride).where(
                    CatalogOverride.catalog_id.in_(list(keys))
                )
            )
        ).scalars()
        for row in rows:
            entry = keys[row.catalog_id]
            entry["listing"] = row.listing
            entry["featured"] = row.featured
            entry["listing_note"] = row.note


async def load_catalog() -> dict[str, list[dict]]:
    """The catalogue the store renders: built-in, plus any operator overlay."""
    skills = [dict(e) for e in SKILL_CATALOG]
    mcp = [dict(e) for e in MCP_CATALOG]

    url = os.environ.get("OPENBOX_CATALOG_URL", "").strip()
    if url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict):
                skills = _merge_remote(skills, data.get("skills") or [])
                mcp = _merge_remote(mcp, data.get("mcp") or [])
                log.info(f"Merged remote catalog from {url}")
        except Exception as e:
            # A reachable store beats an empty one: the built-in catalogue is
            # still perfectly installable without the overlay.
            log.warning(f"Could not load catalog from {url}: {e}")

    for kind, entries in (("skill", skills), ("mcp", mcp)):
        for entry in entries:
            entry["catalog_id"] = catalog_entry_id(kind, entry.get("id", ""))
            entry.setdefault("kind", kind)
            entry.setdefault("listing", LISTED)
            entry.setdefault("featured", False)
            entry.setdefault("listing_note", None)
            # Third-party entries are somebody else's work under our roof; the
            # store says so rather than letting them read as ours.
            entry.setdefault("origin", catalog_entry_origin(entry))
            entry["official"] = entry["origin"] == "official"
        try:
            await _apply_shelf_overrides(entries, kind)
        except Exception as e:
            # Single-user mode runs with no central database at all, and a
            # store that renders its code defaults beats one that 500s. The
            # cost of falling back is that a delisted entry reappears, so say
            # so in the log rather than swallowing it.
            log.warning(f"Could not apply catalog overrides for {kind}: {e}")

    return {"skills": skills, "mcp": mcp}


def catalog_index() -> dict[str, dict]:
    """Built-in entries keyed by ``kind:id``, for resolving dependencies.

    Deliberately unfiltered by ``listing``: the official content skills declare
    ``requires_mcp``, and a skill whose server cannot be installed loads and
    then fails at its first tool call. Filtering here would mean delisting one
    MCP server silently breaks every skill that depends on it, which is a much
    larger blast radius than the operator asked for. Shelf state governs what
    the store *shows*; dependency resolution reads the code catalogue.
    """
    index: dict[str, dict] = {}
    for entry in SKILL_CATALOG:
        index[catalog_entry_id("skill", entry["id"])] = entry
    for entry in MCP_CATALOG:
        index[catalog_entry_id("mcp", entry["id"])] = entry
    return index


async def shelf_index() -> dict[str, dict]:
    """Catalogue entries keyed by ``kind:id``, with shelf state applied.

    The counterpart to :func:`catalog_index` for anything a person asked for
    *by name*: browsing and installing both have to see the same shelf, or a
    delisted entry is merely hidden rather than withheld and an operator's
    decision only costs the person a stale link. Dependency resolution keeps
    reading the unfiltered index — that carve-out is deliberate and scoped to
    ``requires_mcp`` alone.
    """
    catalog = await load_catalog()
    return {
        entry["catalog_id"]: entry
        for group in (catalog["skills"], catalog["mcp"])
        for entry in group
        if entry.get("catalog_id")
    }
