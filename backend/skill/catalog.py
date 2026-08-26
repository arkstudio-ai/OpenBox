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
"""
from __future__ import annotations

import json
import os
from typing import Any

from core.log import create_logger

log = create_logger("skill.catalog")


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

    return {"skills": skills, "mcp": mcp}


def catalog_index() -> dict[str, dict]:
    """Built-in entries keyed by ``kind:id``, for resolving dependencies."""
    index: dict[str, dict] = {}
    for entry in SKILL_CATALOG:
        index[f"skill:{entry['id']}"] = entry
    for entry in MCP_CATALOG:
        index[f"mcp:{entry['id']}"] = entry
    return index
