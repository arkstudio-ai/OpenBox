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

from collections.abc import Mapping
from copy import deepcopy
import os
import re
from typing import Any

from core.log import create_logger

log = create_logger("skill.catalog")


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SERVER_OWNED_FIELDS = frozenset({
    "catalog_id",
    "category",
    "community",
    "installed",
    "library_id",
    "missing_mcp",
    "publication_status",
    "published_at",
})
_SKILL_URL_PREFIXES = ("https://", "http://", "git://", "ssh://", "git@")


def _safe_log_id(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))[:128]


def _required_catalog_text(value: object, field: str, *, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return result


def _safe_install_name(value: object, field: str = "name") -> str:
    name = _required_catalog_text(value, field)
    cleaned = name.replace(" ", "-")
    if (
        cleaned in {".", ".."}
        or cleaned.startswith(".")
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
    ):
        raise ValueError(f"{field} must be a single directory-safe name")
    return cleaned


def _string_list(value: object, field: str, *, limit: int = 128) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result: list[str] = []
    for raw in value:
        item = _required_catalog_text(raw, field)
        if item not in result:
            result.append(item)
        if len(result) > limit:
            raise ValueError(f"{field} has too many entries")
    return result


def _optional_catalog_text(
    value: object,
    field: str,
    *,
    limit: int,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_catalog_text(raw_key, f"{field} key")
        if not isinstance(raw_value, str):
            raise ValueError(f"{field}.{key} must be a string")
        result[key] = raw_value
    return result


def _validated_skill_install(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("install must be an object")
    url = value.get("url")
    content = value.get("content")
    if bool(url) == bool(content):
        raise ValueError("skill install must contain exactly one of url or content")
    result: dict[str, Any] = {}
    if url:
        candidate = _required_catalog_text(url, "install.url", limit=2048)
        # The Action Server repeats this validation immediately before git
        # clone. Keeping the catalogue check aligned prevents the store from
        # advertising an entry that the authoritative installer will reject.
        if not candidate.startswith(_SKILL_URL_PREFIXES):
            raise ValueError("install.url uses an unsupported clone scheme")
        result["url"] = candidate
    else:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("install.content is required")
        # Catalogue JSON is returned to the browser. Bound inline packages so
        # an operator typo cannot turn a listing request into an unbounded
        # response; large packages belong in a reviewed git repository.
        if len(content) > 512_000:
            raise ValueError("install.content is too large")
        result["content"] = content
    if value.get("name") is not None:
        result["name"] = _safe_install_name(value.get("name"), "install.name")
    return result


def _validated_mcp_config(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("config must be an object")
    server_type = value.get("type", "stdio")
    if server_type not in {"stdio", "remote"}:
        raise ValueError("config.type must be stdio or remote")
    result: dict[str, Any] = {"type": server_type}
    if server_type == "stdio":
        result["command"] = _required_catalog_text(
            value.get("command"), "config.command", limit=2048
        )
        args = value.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("config.args must be a list of strings")
        result["args"] = list(args)
        result["env"] = _string_mapping(value.get("env"), "config.env")
    else:
        url = _required_catalog_text(value.get("url"), "config.url", limit=2048)
        if not url.startswith(("https://", "http://")):
            raise ValueError("config.url must use http or https")
        result["url"] = url
        result["headers"] = _string_mapping(value.get("headers"), "config.headers")
    timeout = value.get("timeout", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ValueError("config.timeout must be an integer between 1 and 600")
    result["timeout"] = timeout
    return result


def _validated_catalog_entry(raw: object, *, kind: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("entry must be an object")
    entry = deepcopy(dict(raw))
    entry_id = _required_catalog_text(entry.get("id"), "id")
    if not _SAFE_ID.fullmatch(entry_id):
        raise ValueError("id contains unsupported characters")
    if entry_id.startswith("community:"):
        # That namespace is resolved from immutable, owner-filtered database
        # snapshots. An operator overlay must not impersonate that provenance.
        raise ValueError("community: ids are reserved")
    declared_kind = entry.get("kind", kind)
    if declared_kind != kind:
        raise ValueError(f"entry kind must be {kind}")
    entry["id"] = entry_id
    entry["kind"] = kind
    entry["name"] = _safe_install_name(entry.get("name"))
    entry["title"] = _required_catalog_text(
        entry.get("title") or entry["name"], "title", limit=256
    )
    for field, limit in (
        ("description", 8_000),
        ("publisher", 256),
        ("icon", 32),
    ):
        if field in entry:
            entry[field] = _optional_catalog_text(entry.get(field), field, limit=limit)
    if "homepage" in entry:
        homepage = _optional_catalog_text(
            entry.get("homepage"), "homepage", limit=2048
        )
        if homepage and not homepage.startswith(("https://", "http://")):
            raise ValueError("homepage must use http or https")
        entry["homepage"] = homepage
    for field in _SERVER_OWNED_FIELDS:
        entry.pop(field, None)
    if "tags" in entry:
        entry["tags"] = _string_list(entry.get("tags"), "tags", limit=32)
    if kind == "skill":
        entry["requires_mcp"] = _string_list(
            entry.get("requires_mcp"), "requires_mcp", limit=32
        )
        if any(not _SAFE_ID.fullmatch(dep) for dep in entry["requires_mcp"]):
            raise ValueError("requires_mcp contains an invalid catalog id")
        install = _validated_skill_install(entry.get("install"))
        # Resolve every catalogue Skill to one deterministic directory. Without
        # this, a URL basename or inline frontmatter could differ from the name
        # used by GET /catalog to calculate installed state and conflicts.
        install.setdefault("name", entry["name"])
        entry["install"] = install
    else:
        entry["config"] = _validated_mcp_config(entry.get("config"))
        required_env = entry.get("required_env", [])
        if not isinstance(required_env, list):
            raise ValueError("required_env must be a list")
        normalized_required_env: list[dict[str, Any]] = []
        for raw_env in required_env:
            if not isinstance(raw_env, Mapping):
                raise ValueError("required_env entries must be objects")
            key = _required_catalog_text(raw_env.get("key"), "required_env.key")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("required_env.key must be an environment variable name")
            secret = raw_env.get("secret", False)
            if not isinstance(secret, bool):
                raise ValueError("required_env.secret must be a boolean")
            normalized_required_env.append({
                "key": key,
                "label": _required_catalog_text(
                    raw_env.get("label") or key, "required_env.label", limit=256
                ),
                "secret": secret,
            })
        entry["required_env"] = normalized_required_env
    return entry


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


def _merge_remote(
    entries: list[dict],
    remote: object,
    *,
    kind: str,
) -> list[dict]:
    """Overlay validated remote entries, retaining a valid built-in on error."""
    by_id = {e["id"]: deepcopy(e) for e in entries}
    if not isinstance(remote, list):
        return list(by_id.values())
    for item in remote:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        candidate = {**by_id.get(item_id, {}), **item}
        try:
            by_id[item_id] = _validated_catalog_entry(candidate, kind=kind)
        except ValueError as exc:
            # Log identity and reason only. Inline Skill content, MCP headers
            # and environment values must never enter backend logs.
            log.warning(
                "Ignored invalid remote catalog entry kind=%s id=%s reason=%s",
                kind,
                _safe_log_id(item_id),
                str(exc),
            )
    return list(by_id.values())


def _filter_installable_skills(
    skills: list[dict],
    mcp: list[dict],
    *,
    builtin_skills: Mapping[str, dict] | None = None,
) -> list[dict]:
    """Keep only skills whose declared catalogue dependencies can resolve."""
    mcp_ids = {entry["id"] for entry in mcp}
    result: list[dict] = []
    for entry in skills:
        missing = [dep for dep in entry.get("requires_mcp", []) if dep not in mcp_ids]
        if not missing:
            result.append(entry)
            continue
        fallback = (builtin_skills or {}).get(entry["id"])
        if fallback is not None and fallback != entry:
            fallback_missing = [
                dep for dep in fallback.get("requires_mcp", []) if dep not in mcp_ids
            ]
            if not fallback_missing:
                result.append(deepcopy(fallback))
                log.warning(
                    "Ignored remote skill override with unknown MCP dependencies id=%s",
                    _safe_log_id(entry["id"]),
                )
                continue
        log.warning(
            "Ignored catalog skill with unknown MCP dependencies id=%s",
            _safe_log_id(entry["id"]),
        )
    return result


async def load_catalog() -> dict[str, list[dict]]:
    """Return the one validated catalogue used by both listing and install."""
    skills = [_validated_catalog_entry(e, kind="skill") for e in SKILL_CATALOG]
    builtin_skills = {entry["id"]: deepcopy(entry) for entry in skills}
    mcp = [_validated_catalog_entry(e, kind="mcp") for e in MCP_CATALOG]

    url = os.environ.get("OPENBOX_CATALOG_URL", "").strip()
    if url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict):
                skills = _merge_remote(
                    skills,
                    data.get("skills") or [],
                    kind="skill",
                )
                mcp = _merge_remote(
                    mcp,
                    data.get("mcp") or [],
                    kind="mcp",
                )
                log.info(f"Merged remote catalog from {url}")
        except Exception as e:
            # A reachable store beats an empty one: the built-in catalogue is
            # still perfectly installable without the overlay.
            log.warning(f"Could not load catalog from {url}: {e}")

    skills = _filter_installable_skills(
        skills,
        mcp,
        builtin_skills=builtin_skills,
    )
    return {"skills": skills, "mcp": mcp}


def catalog_index(catalog: Mapping[str, object] | None = None) -> dict[str, dict]:
    """Index one validated catalogue snapshot for install and dependencies.

    Passing the result of :func:`load_catalog` is the production path. The
    no-argument form remains useful to offline tooling and returns the same
    validated built-in baseline rather than a second, weaker representation.
    """
    if catalog is None:
        skills = [_validated_catalog_entry(e, kind="skill") for e in SKILL_CATALOG]
        mcp = [_validated_catalog_entry(e, kind="mcp") for e in MCP_CATALOG]
    else:
        raw_skills = catalog.get("skills", [])
        raw_mcp = catalog.get("mcp", [])
        if not isinstance(raw_skills, list) or not isinstance(raw_mcp, list):
            raise ValueError("catalog skills and mcp must be lists")
        # load_catalog already validated these rows. Revalidation makes this
        # public helper safe for tests/offline callers that construct a snapshot.
        skills = [_validated_catalog_entry(e, kind="skill") for e in raw_skills]
        mcp = [_validated_catalog_entry(e, kind="mcp") for e in raw_mcp]

    installable_skills = _filter_installable_skills(skills, mcp)
    if len(installable_skills) != len(skills):
        raise ValueError("catalog contains unresolved skill dependencies")
    skills = installable_skills

    index: dict[str, dict] = {}
    for entry in skills:
        index[f"skill:{entry['id']}"] = entry
    for entry in mcp:
        index[f"mcp:{entry['id']}"] = entry
    return index
