"""Provider-neutral tool catalogue and schema-exposure planning.

Registration, authorization and provider visibility are deliberately separate
concepts.  This module starts *after* agent allowlists and whole-tool
permissions have produced an eligible mapping; it never reads Skill content
and never performs database, provider or sandbox I/O.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Literal, Mapping

from tool.tool import ToolInfo


ToolSource = Literal["builtin", "custom", "mcp", "synthetic"]
ToolPlane = Literal["platform", "sandbox"]

SAME_RESPONSE_SAFE_TOOL_IDS_V1 = frozenset({
    "read",
    "glob",
    "grep",
    "todo_read",
})

BUILD_RESIDENT_IDS = frozenset({
    "bash",
    "read",
    "write",
    "glob",
    "grep",
    "skill",
    "skill_search",
    "question",
    "task",
    "capability_search",
})

AGENT_RESIDENT_IDS: Mapping[str, frozenset[str]] = MappingProxyType({
    "build": BUILD_RESIDENT_IDS,
    "plan": frozenset({"bash", "read", "glob", "grep", "question", "plan_exit"}),
    "explore": frozenset({"bash", "read", "glob", "grep"}),
    "general": frozenset({"bash", "read", "glob", "grep", "skill", "skill_search"}),
})

INTENT_PACKS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "planning": ("todo_write", "todo_read", "plan_enter"),
    "efficiency": ("batch", "multiedit"),
    "research": ("web_search", "web_fetch"),
    "browser": ("browser_mode", "computer"),
    "vision": ("view_image",),
    "delivery": ("share_file",),
    "automation": ("cron",),
    "image": ("image_gen", "view_image"),
    "video": (
        "creator_context",
        "image_gen",
        "video_project",
        "video_generate",
        "video_transcribe",
        "video_render",
    ),
    "skill_admin": ("skill_manage", "share_file"),
})

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_URL = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)


def _frozen_mapping(values: Mapping) -> Mapping:
    return MappingProxyType(dict(values))


def _clean_hint(value: str, *, sandbox: bool) -> str:
    cleaned = _SPACE.sub(" ", _CONTROL_CHARS.sub("", value or "")).strip()
    if sandbox:
        cleaned = html.escape(cleaned, quote=True)
    # A hint is a bounded routing aid, never the full operational contract.
    return cleaned[:200]


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    provider_name: str
    discovery_hint: str
    parameter_names: tuple[str, ...]
    source: ToolSource
    plane: ToolPlane
    pack: str | None
    schema_digest: str
    schema_chars: int
    same_response_safe: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.provider_name:
            raise ValueError("catalogue identities must be non-empty")
        if self.source not in {"builtin", "custom", "mcp", "synthetic"}:
            raise ValueError("unknown tool source")
        if self.plane not in {"platform", "sandbox"}:
            raise ValueError("unknown tool plane")
        if self.plane == "sandbox" and self.pack is not None:
            raise ValueError("sandbox-plane tools cannot belong to an intent pack")
        if self.plane == "sandbox" and self.same_response_safe:
            raise ValueError("sandbox-plane tools cannot execute on native reveal")
        if self.schema_chars < 0:
            raise ValueError("schema_chars must be non-negative")


@dataclass(frozen=True)
class EligibleCatalog:
    tools: Mapping[str, ToolInfo]
    entries: Mapping[str, CatalogEntry]
    generation: str

    def __post_init__(self) -> None:
        tool_ids = tuple(self.tools)
        entry_ids = tuple(self.entries)
        if tool_ids != entry_ids:
            raise ValueError("eligible tools and catalogue entries must have identical stable ids")
        object.__setattr__(self, "tools", _frozen_mapping(self.tools))
        object.__setattr__(self, "entries", _frozen_mapping(self.entries))


@dataclass(frozen=True)
class ExposurePlan:
    direct_ids: tuple[str, ...]
    deferred_ids: tuple[str, ...]
    discovery_ids: tuple[str, ...]
    reasons: Mapping[str, str]
    strategy: str
    schema_chars: int

    def __post_init__(self) -> None:
        direct = set(self.direct_ids)
        deferred = set(self.deferred_ids)
        if direct & deferred:
            raise ValueError("a tool cannot be direct and deferred in one plan")
        object.__setattr__(self, "reasons", _frozen_mapping(self.reasons))


@dataclass(frozen=True)
class ExposureSignals:
    user_task_text: str = ""
    urls: tuple[str, ...] = ()
    attachment_kinds: tuple[str, ...] = ()
    has_open_todos: bool = False
    has_active_video_production: bool = False
    has_active_video_job: bool = False
    browser_workflow_active: bool = False
    deliverable_asset_ids: tuple[str, ...] = ()
    signal_errors: tuple[str, ...] = ()


def _normalized_schema(tool: ToolInfo) -> dict:
    # Local import avoids an import cycle while agent.llm constructs adapters.
    from agent.llm import _tool_parameters_schema

    return _tool_parameters_schema(tool)


def _infer_identity(lookup_name: str, tool: ToolInfo) -> tuple[str, str, ToolSource, ToolPlane]:
    execute = getattr(tool, "execute", None)
    canonical = (
        getattr(tool, "canonical_id", None)
        or getattr(execute, "_mcp_canonical_id", None)
        or tool.id
        or lookup_name
    )
    provider_name = getattr(tool, "provider_name", None) or lookup_name
    source = str(getattr(tool, "source", "builtin") or "builtin")
    plane = str(getattr(tool, "plane", "platform") or "platform")

    if getattr(execute, "_mcp_canonical_id", None) or lookup_name.startswith("mcp_"):
        source = "mcp"
        plane = "sandbox"
    return str(canonical), str(provider_name), source, plane  # type: ignore[return-value]


def build_eligible_catalog(tools: Mapping[str, ToolInfo]) -> EligibleCatalog:
    """Project a permission-filtered ToolInfo mapping into a stable catalogue."""
    projected: list[tuple[str, ToolInfo, CatalogEntry]] = []
    for lookup_name, tool in tools.items():
        canonical_id, provider_name, source, plane = _infer_identity(lookup_name, tool)
        schema = _normalized_schema(tool)
        properties = schema.get("properties", {})
        parameter_names = tuple(sorted(properties)) if isinstance(properties, dict) else ()
        description = str(tool.description or "")
        digest_payload = json.dumps(
            {"description": description, "parameters": schema},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        schema_chars = len(json.dumps(
            {
                "name": provider_name,
                "description": description,
                "parameters": schema,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        explicit_hint = str(getattr(tool, "discovery_hint", "") or "")
        hint = _clean_hint(
            explicit_hint or description,
            sandbox=plane == "sandbox",
        )
        pack = getattr(tool, "pack", None)
        # Production allowlist is the sole source of same-response authority.
        # Custom/platform metadata cannot impersonate an audited built-in even
        # by choosing the same canonical id.
        same_response_safe = (
            source == "builtin"
            and canonical_id in SAME_RESPONSE_SAFE_TOOL_IDS_V1
        )
        if plane == "sandbox":
            pack = None
            same_response_safe = False

        entry = CatalogEntry(
            id=canonical_id,
            provider_name=provider_name,
            discovery_hint=hint,
            parameter_names=parameter_names,
            source=source,  # type: ignore[arg-type]
            plane=plane,  # type: ignore[arg-type]
            pack=pack,
            schema_digest=schema_digest,
            schema_chars=schema_chars,
            same_response_safe=same_response_safe,
        )
        projected.append((canonical_id, tool, entry))

    projected.sort(key=lambda item: item[0])
    canonical_ids = [item[0] for item in projected]
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("duplicate canonical tool identity")
    provider_names = [item[2].provider_name for item in projected]
    if len(provider_names) != len(set(provider_names)):
        raise ValueError("duplicate provider tool name")

    stable_tools = {canonical: tool for canonical, tool, _entry in projected}
    stable_entries = {canonical: entry for canonical, _tool, entry in projected}
    generation_source = "\n".join(
        f"{entry.id}:{entry.provider_name}:{entry.schema_digest}"
        for entry in stable_entries.values()
    )
    generation = hashlib.sha256(generation_source.encode("utf-8")).hexdigest()
    return EligibleCatalog(stable_tools, stable_entries, generation)


def legacy_eager_plan(catalog: EligibleCatalog, *, strategy: str = "legacy_eager") -> ExposurePlan:
    ids = tuple(catalog.entries)
    return ExposurePlan(
        direct_ids=ids,
        deferred_ids=(),
        discovery_ids=(),
        reasons={tool_id: strategy for tool_id in ids},
        strategy=strategy,
        schema_chars=sum(catalog.entries[tool_id].schema_chars for tool_id in ids),
    )


def strip_skill_directives(text: str) -> str:
    """Remove knowledge-loader syntax before task-intent routing.

    This is deliberately narrow and deterministic. It prevents a request such
    as "only load/summarize video-production skill" from turning the skill's
    name into a media capability trigger.
    """
    lines = []
    for line in (text or "").splitlines():
        # A user often writes "do not load a skill. Create a video" on one
        # line. Treating the whole line as one directive erased the real task
        # and made explicit media requests lose their intent pack. Strong
        # sentence boundaries let us remove only the knowledge-loading clause;
        # commas remain inside one segment so "only load X, do not execute a
        # video" stays a single negative instruction and cannot self-trigger.
        segments = re.split(r"(?<=[。！？!?;；])|(?<=\.)\s+", line)
        kept: list[str] = []
        for segment in segments:
            stripped = segment.strip()
            if not stripped:
                continue
            if re.fullmatch(
                r"(?:/[\w.-]+|skill[:\s]+[\w.-]+)[。！？!?;；.]?",
                stripped,
                re.IGNORECASE,
            ):
                continue
            mentions_loader = bool(
                re.search(r"skill|技能", stripped, re.IGNORECASE)
                and re.search(
                    r"加载|总结|阅读|load|summari[sz]e|read",
                    stripped,
                    re.IGNORECASE,
                )
            )
            knowledge_only = mentions_loader and bool(
                re.search(
                    r"只|仅|不要|不执行|only|without|do\s+not",
                    stripped,
                    re.IGNORECASE,
                )
            )
            workflow_action = bool(
                re.search(
                    r"生成|创建|制作|渲染|转写|审批|generate|create|make|render|transcribe|approve",
                    stripped,
                    re.IGNORECASE,
                )
            )
            if knowledge_only or (mentions_loader and not workflow_action):
                continue
            kept.append(stripped)
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines)


def route_product_state_packs(signals: ExposureSignals) -> tuple[str, ...]:
    """Return packs required to resume durable product state safely."""
    packs: set[str] = set()
    if signals.browser_workflow_active:
        packs.add("browser")
    if signals.has_open_todos:
        packs.add("planning")
    if signals.has_active_video_production or signals.has_active_video_job:
        packs.add("video")
    if signals.deliverable_asset_ids:
        packs.add("delivery")
    return tuple(name for name in INTENT_PACKS if name in packs)


def route_explicit_intent_packs(signals: ExposureSignals) -> tuple[str, ...]:
    """Return packs requested by the current user text or attachments."""
    text = strip_skill_directives(signals.user_task_text).lower()
    packs: set[str] = set()
    urls = signals.urls or tuple(_URL.findall(text))
    kinds = {kind.lower() for kind in signals.attachment_kinds}

    if urls or re.search(r"\b(search|research|verify|latest|docs?)\b|搜索|查证|最新|文档", text):
        packs.add("research")
    if re.search(r"browser|网页|浏览器|桌面|点击", text):
        packs.add("browser")
    if re.search(r"\bplan\b|计划|多步骤|分阶段", text):
        packs.add("planning")
    if re.search(r"每天|每周|每月|定时|周期|提醒|监控|cron|schedule", text):
        packs.add("automation")
    image_attachment = bool(kinds & {"image", "photo", "screenshot", "png", "jpg", "jpeg", "webp"})
    if image_attachment or re.search(r"生成.{0,8}(图片|图像)|画一|改图|image", text):
        packs.add("image")
    if re.search(r"视频|短片|口播|成片|video|字幕|逐字稿", text):
        packs.add("video")
    if re.search(r"下载|导出|交付|share file", text):
        packs.add("delivery")
    if re.search(r"(?:创建|导入|导出|制作).{0,12}(?:skill|技能)", text, re.IGNORECASE):
        packs.add("skill_admin")
    return tuple(name for name in INTENT_PACKS if name in packs)


def route_intent_packs(signals: ExposureSignals) -> tuple[str, ...]:
    """Compatibility union of explicit intent and durable product state."""
    packs = set(route_explicit_intent_packs(signals))
    packs.update(route_product_state_packs(signals))
    return tuple(name for name in INTENT_PACKS if name in packs)


def preferred_editor_id(model_id: str, eligible_ids: Iterable[str]) -> str | None:
    """Select one schema-compatible editor for the current model family."""
    eligible = set(eligible_ids)
    model = (model_id or "").lower()
    prefers_patch = "codex" in model or "gpt-5" in model or "trinity" in model
    order = ("apply_patch", "edit") if prefers_patch else ("edit", "apply_patch")
    return next((tool_id for tool_id in order if tool_id in eligible), None)


def portable_plan(
    catalog: EligibleCatalog,
    *,
    agent_name: str,
    signals: ExposureSignals = ExposureSignals(),
    revealed_ids: Iterable[str] = (),
    editor_id: str | None = None,
) -> ExposurePlan:
    """Plan direct/deferred definitions without performing any I/O."""
    eligible = set(catalog.entries)
    direct: set[str] = set(AGENT_RESIDENT_IDS.get(agent_name, frozenset())) & eligible
    reasons: dict[str, str] = {tool_id: "resident" for tool_id in direct}

    # These are logical discovery slots, not workflow capabilities.  Their
    # presence in the eligible catalogue already proves that the AgentDef and
    # permission pass allowed them.  Keep them resident for config-defined
    # agents too: a name-based resident table must not make an explicitly
    # allowlisted custom agent unable to discover its own deferred tools.
    for tool_id in ("capability_search", "skill_search"):
        if tool_id in eligible:
            direct.add(tool_id)
            reasons[tool_id] = "resident"

    if editor_id and editor_id in eligible:
        direct.add(editor_id)
        reasons[editor_id] = "model_editor"

    # A persisted reveal is the lowest-priority materialization source. Do it
    # before intent/state routing so current product state cannot be demoted
    # to a trim-eligible historical reveal.
    for tool_id in revealed_ids:
        if tool_id in eligible and tool_id not in direct:
            direct.add(tool_id)
            reasons[tool_id] = "revealed"

    for pack_name in route_explicit_intent_packs(signals):
        for tool_id in INTENT_PACKS[pack_name]:
            entry = catalog.entries.get(tool_id)
            if entry is None or entry.plane != "platform":
                continue
            direct.add(tool_id)
            if reasons.get(tool_id) not in {"resident", "model_editor"}:
                reasons[tool_id] = f"intent:{pack_name}"

    # Product state is applied after text intent so recovery always wins when
    # both routes select the same tool. The budgeter treats these definitions
    # as required and never silently trims an in-progress workflow's controls.
    for pack_name in route_product_state_packs(signals):
        for tool_id in INTENT_PACKS[pack_name]:
            entry = catalog.entries.get(tool_id)
            if entry is None or entry.plane != "platform":
                continue
            direct.add(tool_id)
            if reasons.get(tool_id) not in {"resident", "model_editor"}:
                reasons[tool_id] = f"product:{pack_name}"

    direct_ids = tuple(sorted(direct))
    deferred_ids = tuple(sorted(eligible - direct))
    # A resident search definition with no deferred frontier is harmless, but
    # must not advertise a discoverable catalogue that does not exist.
    discovery_ids = deferred_ids if "capability_search" in direct else ()
    return ExposurePlan(
        direct_ids=direct_ids,
        deferred_ids=deferred_ids,
        discovery_ids=discovery_ids,
        reasons={tool_id: reasons[tool_id] for tool_id in direct_ids},
        strategy="portable",
        schema_chars=sum(catalog.entries[tool_id].schema_chars for tool_id in direct_ids),
    )


def provider_tools_for_plan(
    catalog: EligibleCatalog,
    plan: ExposurePlan,
    *,
    native: bool = False,
) -> dict[str, ToolInfo]:
    """Return the provider-name mapping for one immutable exposure plan."""
    ids = (*plan.direct_ids, *plan.deferred_ids) if native else plan.direct_ids
    return {
        catalog.entries[tool_id].provider_name: catalog.tools[tool_id]
        for tool_id in ids
        if tool_id in catalog.entries
    }


def step_executable_ids(plan: ExposurePlan) -> frozenset[str]:
    """Only direct definitions execute before a verified native reveal."""
    return frozenset(plan.direct_ids)
