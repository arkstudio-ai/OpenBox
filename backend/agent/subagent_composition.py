"""Fail-loud, durable composition for Task child Agents.

The live Agent registry and provider catalogue are mutable deployment state.
A continuable child cannot safely rebuild its identity from either one after a
worker restart, so Task snapshots the exact executable preset and provider
contract at acceptance.  The snapshot is private descriptor state; only the
ordinary child transcript and safe fork lineage are public.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from permission.permission import Rule


COMPOSITION_VERSION = 1
MAX_PERSONA_CHARS = 8_192
MAX_PRESET_PROMPT_CHARS = 65_536
MAX_PRESET_TOOLS = 4_096
MAX_SCHEMA_BYTES = 32_768
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_NODES = 1_024
MAX_SCHEMA_COLLECTION = 256
MAX_SCHEMA_STRING = 8_192
MAX_STRUCTURED_RESULT_BYTES = 128 * 1_024
MAX_COMPOSITION_BYTES = 128 * 1_024
_COMPOSITION_KEYS = {
    "version",
    "model",
    "reasoning",
    "agent_preset",
    "persona",
    "tool_allowlist",
    "output_schema",
    "provider",
    "seed_mode",
    "digest",
}
_PRESET_KEYS = {
    "name",
    "mode",
    "tools",
    "max_steps",
    "model",
    "temperature",
    "prompt",
    "permission",
    "portable_opt_in",
    "capabilities",
}
_PROVIDER_KEYS = {
    "provider_id", "capabilities", "reasoning_variants", "binding_digest",
}
_CAPABILITIES = frozenset({
    "model", "agent_preset", "tool_filter", "reasoning", "persona",
    "output_schema",
})
_PROVIDER_CAPABILITIES = frozenset({
    "model", "tool_filter", "reasoning", "persona", "output_schema",
})


class SubagentCompositionError(RuntimeError):
    """A requested Child-Agent composition cannot be honoured exactly."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SubagentCompositionError("subagent composition must be JSON-safe") from exc


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "digest"}
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def _bounded_string(value: Any, *, field: str, limit: int, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > limit:
        raise SubagentCompositionError(f"subagent {field} is invalid or too large")
    return value


def _string_list(
    value: Any,
    *,
    field: str,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise SubagentCompositionError(f"subagent {field} is invalid or too large")
    result: list[str] = []
    for item in value:
        text = _bounded_string(item, field=field, limit=1_024, empty=False)
        if allowed is not None and text not in allowed:
            raise SubagentCompositionError(f"subagent {field} contains unsupported value {text!r}")
        result.append(text)
    if len(set(result)) != len(result):
        raise SubagentCompositionError(f"subagent {field} contains duplicates")
    return tuple(result)


def _validate_schema_node(value: Any, *, depth: int, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
        raise SubagentCompositionError("subagent output schema exceeds structural bounds")
    if isinstance(value, str):
        if len(value) > MAX_SCHEMA_STRING:
            raise SubagentCompositionError("subagent output schema string is too large")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        if len(value) > MAX_SCHEMA_COLLECTION:
            raise SubagentCompositionError("subagent output schema list is too large")
        for item in value:
            _validate_schema_node(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        if len(value) > MAX_SCHEMA_COLLECTION:
            raise SubagentCompositionError("subagent output schema object is too large")
        for key, item in value.items():
            _bounded_string(key, field="output schema key", limit=MAX_SCHEMA_STRING)
            _validate_schema_node(item, depth=depth + 1, counter=counter)
        return
    raise SubagentCompositionError("subagent output schema is not JSON-safe")


def validate_output_schema(value: Any) -> dict[str, Any] | None:
    """Canonicalize one bounded object JSON schema or reject it."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SubagentCompositionError("subagent output_schema must be an object schema")
    if value.get("type") == "json_schema" and isinstance(value.get("schema"), dict):
        value = value["schema"]
    if value.get("type") != "object" or not isinstance(value.get("properties"), dict):
        raise SubagentCompositionError(
            "subagent output_schema must declare an object with properties"
        )
    _validate_schema_node(value, depth=0, counter=[0])
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise SubagentCompositionError("subagent output schema exceeds its byte budget")
    canonical = json.loads(encoded.decode("utf-8"))
    try:
        Draft202012Validator.check_schema(canonical)
    except SchemaError as exc:
        raise SubagentCompositionError(
            f"subagent output_schema is not valid JSON Schema: {exc.message}"
        ) from exc
    return canonical


def validate_structured_result(
    schema: Mapping[str, Any],
    value: Any,
) -> dict[str, Any]:
    """Validate and detach one terminal structured result locally."""

    canonical_schema = validate_output_schema(dict(schema))
    if canonical_schema is None:  # pragma: no cover - guarded by the type above
        raise SubagentCompositionError("subagent output_schema is missing")
    try:
        encoded = _canonical_json(value).encode("utf-8")
    except SubagentCompositionError:
        raise
    if len(encoded) > MAX_STRUCTURED_RESULT_BYTES:
        raise SubagentCompositionError(
            "subagent structured result exceeds its byte budget"
        )
    detached = json.loads(encoded.decode("utf-8"))
    try:
        Draft202012Validator(canonical_schema).validate(detached)
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path)
        location = f" at {path}" if path else ""
        raise SubagentCompositionError(
            f"subagent structured result violates output_schema{location}: {exc.message}"
        ) from exc
    if not isinstance(detached, dict):
        # The accepted schema is object-rooted, but keep this invariant local
        # instead of trusting a third-party validator implementation detail.
        raise SubagentCompositionError("subagent structured result must be an object")
    return detached


def _canonical_permission(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, (list, tuple)) or len(raw) > 4_096:
        raise SubagentCompositionError("subagent preset permission snapshot is invalid")
    result: list[dict[str, str]] = []
    for item in raw:
        try:
            rule = item if isinstance(item, Rule) else Rule.model_validate(item)
        except Exception as exc:
            raise SubagentCompositionError(
                "subagent preset permission snapshot is invalid"
            ) from exc
        result.append(rule.model_dump(mode="json"))
    return result


@dataclass(frozen=True, slots=True)
class FrozenAgentPreset:
    name: str
    mode: str
    tools: tuple[str, ...]
    max_steps: int
    model: str | None
    temperature: float
    prompt: str | None
    permission: tuple[dict[str, str], ...]
    portable_opt_in: bool
    capabilities: frozenset[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "tools": list(self.tools),
            "max_steps": self.max_steps,
            "model": self.model,
            "temperature": self.temperature,
            "prompt": self.prompt,
            "permission": [dict(rule) for rule in self.permission],
            "portable_opt_in": self.portable_opt_in,
            "capabilities": sorted(self.capabilities),
        }

    def to_agent_def(self, *, persona: str, effective_model: str):
        """Recreate the exact accepted preset without consulting live config."""
        from agent.agent import AgentDef

        prompt = self.prompt
        if persona:
            overlay = (
                "<delegated-persona>\n"
                f"{persona}\n"
                "</delegated-persona>"
            )
            prompt = f"{prompt}\n\n{overlay}" if prompt else overlay
        return AgentDef(
            name=self.name,
            description=f"Frozen Task preset {self.name}",
            tools=list(self.tools),
            max_steps=self.max_steps,
            # The composition's selected model is stronger than a live User
            # preference: every continuation step must keep it exact.
            model=effective_model,
            temperature=self.temperature,
            prompt=prompt,
            mode=self.mode,
            hidden=True,
            permission=[dict(rule) for rule in self.permission],
            portable_opt_in=self.portable_opt_in,
            subagent_capabilities=self.capabilities,
        )


def _parse_preset(raw: Any) -> FrozenAgentPreset:
    if not isinstance(raw, dict) or set(raw) != _PRESET_KEYS:
        raise SubagentCompositionError("subagent frozen preset is unsupported")
    name = _bounded_string(raw["name"], field="preset name", limit=64, empty=False)
    mode = _bounded_string(raw["mode"], field="preset mode", limit=16, empty=False)
    if mode not in {"subagent", "all"}:
        raise SubagentCompositionError("frozen preset is not spawnable")
    tools = _string_list(
        raw["tools"], field="preset tools", maximum=MAX_PRESET_TOOLS,
    )
    max_steps = raw["max_steps"]
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 10_000:
        raise SubagentCompositionError("subagent preset max_steps is invalid")
    model = raw["model"]
    if model is not None:
        model = _bounded_string(model, field="preset model", limit=128, empty=False)
    temperature = raw["temperature"]
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise SubagentCompositionError("subagent preset temperature is invalid")
    prompt = raw["prompt"]
    if prompt is not None:
        prompt = _bounded_string(
            prompt, field="preset prompt", limit=MAX_PRESET_PROMPT_CHARS,
        )
    permission = tuple(_canonical_permission(raw["permission"]))
    portable = raw["portable_opt_in"]
    if not isinstance(portable, bool):
        raise SubagentCompositionError("subagent preset portable flag is invalid")
    capabilities = frozenset(_string_list(
        raw["capabilities"],
        field="preset capabilities",
        maximum=len(_CAPABILITIES),
        allowed=_CAPABILITIES,
    ))
    required = {"model", "agent_preset", "tool_filter"}
    if not required.issubset(capabilities):
        raise SubagentCompositionError("subagent preset omits mandatory capabilities")
    return FrozenAgentPreset(
        name=name,
        mode=mode,
        tools=tools,
        max_steps=max_steps,
        model=model,
        temperature=float(temperature),
        prompt=prompt,
        permission=permission,
        portable_opt_in=portable,
        capabilities=capabilities,
    )


def freeze_agent_preset(agent_def: Any) -> FrozenAgentPreset:
    return _parse_preset({
        "name": agent_def.name,
        "mode": agent_def.mode,
        "tools": list(agent_def.tools),
        "max_steps": agent_def.max_steps,
        "model": agent_def.model,
        "temperature": agent_def.temperature,
        "prompt": agent_def.prompt,
        "permission": list(agent_def.permission),
        "portable_opt_in": bool(agent_def.portable_opt_in),
        "capabilities": sorted(agent_def.subagent_capabilities),
    })


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider_id: str
    capabilities: frozenset[str]
    reasoning_variants: frozenset[str]
    binding_digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capabilities": sorted(self.capabilities),
            "reasoning_variants": sorted(self.reasoning_variants),
            "binding_digest": self.binding_digest,
        }


def _parse_provider(raw: Any) -> ProviderCapabilities:
    if not isinstance(raw, dict) or set(raw) != _PROVIDER_KEYS:
        raise SubagentCompositionError("subagent provider capability snapshot is unsupported")
    provider_id = _bounded_string(
        raw["provider_id"], field="provider id", limit=64, empty=False,
    )
    capabilities = frozenset(_string_list(
        raw["capabilities"],
        field="provider capabilities",
        maximum=len(_PROVIDER_CAPABILITIES),
        allowed=_PROVIDER_CAPABILITIES,
    ))
    variants = frozenset(_string_list(
        raw["reasoning_variants"],
        field="provider reasoning variants",
        maximum=32,
    ))
    binding_digest = _bounded_string(
        raw["binding_digest"], field="provider binding digest", limit=64,
        empty=False,
    )
    if len(binding_digest) != 64 or any(
        char not in "0123456789abcdef" for char in binding_digest
    ):
        raise SubagentCompositionError("subagent provider binding digest is invalid")
    if "model" not in capabilities or "tool_filter" not in capabilities:
        raise SubagentCompositionError("provider omits mandatory Task capabilities")
    if bool(variants) != ("reasoning" in capabilities):
        raise SubagentCompositionError("provider reasoning declaration is inconsistent")
    return ProviderCapabilities(provider_id, capabilities, variants, binding_digest)


def provider_capabilities(model_id: str, config: Any) -> ProviderCapabilities:
    from agent.llm import resolved_subagent_provider_binding

    binding = resolved_subagent_provider_binding(model_id, config)
    if binding.get("declaration_consistent") is not True:
        raise SubagentCompositionError(
            f"model {model_id!r} capability declaration exceeds its provider"
        )
    readiness = binding.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        reason = (
            str(readiness.get("reason") or "not_ready")
            if isinstance(readiness, dict) else "not_ready"
        )
        raise SubagentCompositionError(
            f"provider binding for model {model_id!r} is not ready: {reason}"
        )
    provider_id = _bounded_string(
        binding.get("provider_id"), field="provider id", limit=64, empty=False,
    )
    capabilities = _string_list(
        binding.get("capabilities"),
        field="provider capabilities",
        maximum=len(_PROVIDER_CAPABILITIES),
        allowed=_PROVIDER_CAPABILITIES,
    )
    variants = _string_list(
        binding.get("reasoning_variants"),
        field="provider reasoning variants",
        maximum=32,
    )
    material = {
        "model_id": model_id,
        "provider_id": provider_id,
        "api_base": str(binding.get("api_base") or ""),
        "configured_provider": binding.get("configured_provider") is True,
        "declaration_consistent": True,
        "readiness": readiness,
        "capabilities": list(capabilities),
        "reasoning_variants": list(variants),
    }
    binding_digest = hashlib.sha256(
        _canonical_json(material).encode("utf-8")
    ).hexdigest()
    return _parse_provider({
        "provider_id": provider_id,
        "capabilities": list(capabilities),
        "reasoning_variants": list(variants),
        "binding_digest": binding_digest,
    })


@dataclass(frozen=True, slots=True)
class CompositionRequest:
    """Bounded wire-neutral request accepted from ``TaskArgs``."""

    model: str | None
    reasoning: str | None
    persona: str
    tools: tuple[str, ...] | None
    output_schema: dict[str, Any] | None
    seed_mode: str

    @classmethod
    def validate(
        cls,
        *,
        model: str | None,
        reasoning: str | None,
        persona: str | None,
        tools: Sequence[str] | None,
        output_schema: dict[str, Any] | None,
        seed_mode: str,
    ) -> "CompositionRequest":
        if model is not None:
            model = _bounded_string(model, field="model", limit=128, empty=False)
        if reasoning is not None:
            reasoning = _bounded_string(
                reasoning, field="reasoning", limit=32, empty=False,
            )
        persona_value = _bounded_string(
            persona or "", field="persona", limit=MAX_PERSONA_CHARS,
        )
        requested_tools = (
            _string_list(tools, field="requested tools", maximum=MAX_PRESET_TOOLS)
            if tools is not None
            else None
        )
        if seed_mode not in {"fresh", "fork"}:
            raise SubagentCompositionError("subagent seed mode is unsupported")
        return cls(
            model=model,
            reasoning=reasoning,
            persona=persona_value,
            tools=requested_tools,
            output_schema=validate_output_schema(output_schema),
            seed_mode=seed_mode,
        )


@dataclass(frozen=True, slots=True)
class SubagentComposition:
    model: str
    reasoning: str | None
    agent_preset: FrozenAgentPreset
    persona: str
    tool_allowlist: frozenset[str]
    output_schema: dict[str, Any] | None
    provider: ProviderCapabilities
    seed_mode: str
    digest: str

    def to_json(self) -> dict[str, Any]:
        payload = {
            "version": COMPOSITION_VERSION,
            "model": self.model,
            "reasoning": self.reasoning,
            "agent_preset": self.agent_preset.to_json(),
            "persona": self.persona,
            "tool_allowlist": sorted(self.tool_allowlist),
            "output_schema": self.output_schema,
            "provider": self.provider.to_json(),
            "seed_mode": self.seed_mode,
        }
        payload["digest"] = _digest(payload)
        return payload

    def frozen_agent(self):
        return self.agent_preset.to_agent_def(
            persona=self.persona,
            effective_model=self.model,
        )


def parse_subagent_composition(raw: Any) -> SubagentComposition:
    if not isinstance(raw, dict) or set(raw) != _COMPOSITION_KEYS:
        raise SubagentCompositionError("subagent composition snapshot is unsupported")
    if raw.get("version") != COMPOSITION_VERSION:
        raise SubagentCompositionError("subagent composition version is unsupported")
    serialized = _canonical_json(raw).encode("utf-8")
    if len(serialized) > MAX_COMPOSITION_BYTES:
        raise SubagentCompositionError("subagent composition exceeds its byte budget")
    expected = _digest(raw)
    supplied = raw.get("digest")
    if not isinstance(supplied, str) or supplied != expected:
        raise SubagentCompositionError("subagent composition digest does not match")
    model = _bounded_string(raw["model"], field="model", limit=128, empty=False)
    reasoning = raw["reasoning"]
    if reasoning is not None:
        reasoning = _bounded_string(reasoning, field="reasoning", limit=32, empty=False)
    persona = _bounded_string(raw["persona"], field="persona", limit=MAX_PERSONA_CHARS)
    tools = frozenset(_string_list(
        raw["tool_allowlist"], field="tool allowlist", maximum=MAX_PRESET_TOOLS,
    ))
    schema = validate_output_schema(raw["output_schema"])
    preset = _parse_preset(raw["agent_preset"])
    provider = _parse_provider(raw["provider"])
    seed_mode = _bounded_string(raw["seed_mode"], field="seed mode", limit=16, empty=False)
    if seed_mode not in {"fresh", "fork"}:
        raise SubagentCompositionError("subagent seed mode is unsupported")
    if not tools.issubset(set(preset.tools)):
        raise SubagentCompositionError("tool allowlist exceeds the frozen preset")
    if reasoning is not None and (
        "reasoning" not in preset.capabilities
        or reasoning not in provider.reasoning_variants
    ):
        raise SubagentCompositionError("reasoning request exceeds declared capabilities")
    if persona and (
        "persona" not in preset.capabilities
        or "persona" not in provider.capabilities
    ):
        raise SubagentCompositionError("persona request exceeds declared capabilities")
    if schema is not None and (
        "output_schema" not in preset.capabilities
        or "output_schema" not in provider.capabilities
    ):
        raise SubagentCompositionError("output schema exceeds declared capabilities")
    return SubagentComposition(
        model=model,
        reasoning=reasoning,
        agent_preset=preset,
        persona=persona,
        tool_allowlist=tools,
        output_schema=schema,
        provider=provider,
        seed_mode=seed_mode,
        digest=supplied,
    )


def _deployment_models(config: Any) -> set[str]:
    from agent.model_resolve import configured_models

    return set(configured_models(config))


def validate_composition_availability(
    composition: SubagentComposition,
    config: Any,
) -> None:
    """Fail before acceptance/resume if an exact provider contract drifted."""
    if composition.model not in _deployment_models(config):
        raise SubagentCompositionError(
            f"accepted subagent model {composition.model!r} is no longer configured"
        )
    current = provider_capabilities(composition.model, config)
    if current.to_json() != composition.provider.to_json():
        raise SubagentCompositionError(
            "accepted subagent provider capabilities changed"
        )


def build_subagent_composition(
    *,
    agent_def: Any,
    parent_tool_ids: Iterable[str],
    config: Any,
    inherited_model: str | None,
    requested_model: str | None,
    reasoning: str | None,
    persona: str | None,
    requested_tools: Sequence[str] | None,
    output_schema: dict[str, Any] | None,
    seed_mode: str,
) -> SubagentComposition:
    """Validate a request against deployment, provider, parent, and preset."""
    from agent.agent import BUILD_ONLY_WORKFLOW_TOOLS

    request = CompositionRequest.validate(
        model=requested_model,
        reasoning=reasoning,
        persona=persona,
        tools=requested_tools,
        output_schema=output_schema,
        seed_mode=seed_mode,
    )
    requested_model = request.model
    reasoning = request.reasoning
    persona = request.persona
    requested_tools = request.tools
    output_schema = request.output_schema
    seed_mode = request.seed_mode
    preset = freeze_agent_preset(agent_def)
    if preset.mode not in {"subagent", "all"}:
        raise SubagentCompositionError(f"agent preset {preset.name!r} is not spawnable")
    if requested_model and preset.model and requested_model != preset.model:
        raise SubagentCompositionError(
            f"agent preset {preset.name!r} fixes model {preset.model!r}"
        )
    model = requested_model or preset.model or inherited_model or config.model
    model = _bounded_string(model, field="model", limit=128, empty=False)
    if model not in _deployment_models(config):
        raise SubagentCompositionError(f"model {model!r} is not configured")
    provider = provider_capabilities(model, config)

    persona_value = persona
    schema = output_schema
    if reasoning is not None:
        if "reasoning" not in preset.capabilities:
            raise SubagentCompositionError(
                f"agent preset {preset.name!r} does not support reasoning composition"
            )
        if reasoning not in provider.reasoning_variants:
            raise SubagentCompositionError(
                f"provider {provider.provider_id!r} does not support reasoning {reasoning!r}"
            )
    if persona_value and "persona" not in preset.capabilities:
        raise SubagentCompositionError(
            f"agent preset {preset.name!r} does not support persona composition"
        )
    if schema is not None and (
        "output_schema" not in preset.capabilities
        or "output_schema" not in provider.capabilities
    ):
        raise SubagentCompositionError(
            f"model/provider for {model!r} does not support structured output"
        )

    parent_tools = set(parent_tool_ids)
    preset_tools = set(preset.tools)
    if requested_tools is None:
        tools = parent_tools.intersection(preset_tools)
    else:
        requested = set(_string_list(
            requested_tools, field="requested tools", maximum=MAX_PRESET_TOOLS,
        ))
        outside_parent = requested - parent_tools
        outside_preset = requested - preset_tools
        if outside_parent:
            raise SubagentCompositionError(
                "requested tools exceed parent authority: "
                + ", ".join(sorted(outside_parent))
            )
        if outside_preset:
            raise SubagentCompositionError(
                "requested tools are not in the child preset: "
                + ", ".join(sorted(outside_preset))
            )
        tools = requested
    forbidden = tools.intersection(BUILD_ONLY_WORKFLOW_TOOLS)
    if forbidden:
        raise SubagentCompositionError(
            "build-only media/workflow tools cannot be delegated: "
            + ", ".join(sorted(forbidden))
        )

    draft = SubagentComposition(
        model=model,
        reasoning=reasoning,
        agent_preset=preset,
        persona=persona_value,
        tool_allowlist=frozenset(tools),
        output_schema=schema,
        provider=provider,
        seed_mode=seed_mode,
        digest="",
    )
    accepted = parse_subagent_composition(draft.to_json())
    validate_composition_availability(accepted, config)
    return accepted


def narrow_follow_up_composition(
    existing: SubagentComposition,
    *,
    delegator_tool_ids: Iterable[str],
    requested_model: str | None,
    reasoning: str | None,
    persona: str | None,
    requested_tools: Sequence[str] | None,
    output_schema: dict[str, Any] | None,
) -> SubagentComposition:
    """Keep immutable fields exact and allow only a smaller tool boundary."""
    immutable_requests = {
        "model": (requested_model, existing.model),
        "reasoning": (reasoning, existing.reasoning),
        "persona": (persona, existing.persona),
    }
    for field, (requested, accepted) in immutable_requests.items():
        if requested is not None and requested != accepted:
            raise SubagentCompositionError(
                f"follow-up cannot change accepted subagent {field}"
            )
    if output_schema is not None:
        schema = validate_output_schema(output_schema)
        if schema != existing.output_schema:
            raise SubagentCompositionError(
                "follow-up cannot change the accepted subagent output schema"
            )
    allowed = set(existing.tool_allowlist).intersection(delegator_tool_ids)
    if requested_tools is not None:
        requested = set(_string_list(
            requested_tools, field="requested tools", maximum=MAX_PRESET_TOOLS,
        ))
        if not requested.issubset(allowed):
            raise SubagentCompositionError(
                "follow-up tools exceed the accepted or current parent authority"
            )
        allowed = requested
    draft = SubagentComposition(
        model=existing.model,
        reasoning=existing.reasoning,
        agent_preset=existing.agent_preset,
        persona=existing.persona,
        tool_allowlist=frozenset(allowed),
        output_schema=existing.output_schema,
        provider=existing.provider,
        seed_mode=existing.seed_mode,
        digest="",
    )
    return parse_subagent_composition(draft.to_json())
