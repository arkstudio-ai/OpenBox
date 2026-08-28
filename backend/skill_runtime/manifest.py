"""skill.yaml — the machine contract of an executable skill (§5.1).

SKILL.md speaks to the model; the manifest speaks to the platform: runtime
kind, operations with their three-layer timeouts, declared phases with i18n
label keys, capability ceilings and policy. Untrusted (user-installed)
manifests can never select the internal runtime.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.log import create_logger

log = create_logger("skill_runtime.manifest")

SUPPORTED_API_VERSIONS = {"openbox.ai/v1"}
MAX_INPUT_BYTES = 512 * 1024
MAX_INLINE_SCHEMA_BYTES = 64 * 1024

_SUPPORTED_SCHEMA_KEYS = {
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "items",
    "oneOf",
    # Annotation-only fields are safe to retain even though validation ignores
    # them. Platform extensions are enumerated so a typo in an authorization
    # hint does not silently downgrade an operator-only question.
    "title",
    "description",
    "default",
    "examples",
    "x-operator-only",
    "x-openbox-review",
}


class ManifestError(Exception):
    pass


def validate_schema_definition(
    schema: dict,
    *,
    label: str,
    _depth: int = 0,
) -> None:
    """Reject schema assertions this compact validator cannot enforce."""
    if _depth > 32:
        raise ManifestError(f"{label} schema nesting exceeds 32 levels")
    unknown = sorted(str(key) for key in set(schema) - _SUPPORTED_SCHEMA_KEYS)
    if unknown:
        raise ManifestError(
            f"{label} schema uses unsupported keyword(s): {', '.join(unknown)}"
        )

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ManifestError(f"{label} schema properties must be an object")
        for key, child in properties.items():
            if not isinstance(key, str) or not isinstance(child, dict):
                raise ManifestError(f"{label} schema contains an invalid property")
            validate_schema_definition(
                child,
                label=f"{label}.{key}",
                _depth=_depth + 1,
            )

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise ManifestError(f"{label} schema items must be an object")
        validate_schema_definition(
            items,
            label=f"{label}[]",
            _depth=_depth + 1,
        )

    branches = schema.get("oneOf")
    if branches is not None:
        if not isinstance(branches, list) or not branches:
            raise ManifestError(f"{label} schema oneOf must be a non-empty list")
        for index, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise ManifestError(
                    f"{label} schema oneOf[{index}] must be an object"
                )
            validate_schema_definition(
                branch,
                label=f"{label}.oneOf[{index}]",
                _depth=_depth + 1,
            )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise ManifestError(
            f"{label} schema supports only boolean additionalProperties"
        )

    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(key, str) or not key for key in required)
    ):
        raise ManifestError(f"{label} schema required must be a string list")

    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
        for key in (lower, upper):
            if key in schema and (
                not isinstance(schema[key], int)
                or isinstance(schema[key], bool)
                or schema[key] < 0
            ):
                raise ManifestError(f"{label} schema has an invalid {key}")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ManifestError(f"{label} schema has inverted {lower}/{upper}")

    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise ManifestError(f"{label} schema enum must be a non-empty list")

    for annotation in ("title", "description"):
        if annotation in schema and not isinstance(schema[annotation], str):
            raise ManifestError(f"{label} schema {annotation} must be a string")
    for extension in ("x-operator-only",):
        if extension in schema and not isinstance(schema[extension], bool):
            raise ManifestError(f"{label} schema {extension} must be boolean")
    if "x-openbox-review" in schema and not isinstance(
        schema["x-openbox-review"], str
    ):
        raise ManifestError(f"{label} schema x-openbox-review must be a string")


def _bounded_json(value, *, label: str, max_bytes: int) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{label} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise ManifestError(f"{label} exceeds the {max_bytes}-byte limit")


class _StrictModel(BaseModel):
    # A typo in a cancellation/timeout/capability key must not silently fall
    # back to a different security policy.
    model_config = ConfigDict(extra="forbid")


class RuntimeSpec(_StrictModel):
    kind: Literal["internal", "sandbox"]
    handler: str = Field(min_length=1, max_length=240)
    handlerVersion: int = Field(default=1, ge=1)


class OperationSpec(_StrictModel):
    inputSchema: dict | str | None = None
    outputSchema: dict | str | None = None
    queue: str = Field(default="default", min_length=1, max_length=40)
    #: Three-layer timeouts (§5.1): one bounded invocation, cumulative
    #: external wait, and the job's total deadline.
    invocationTimeoutSeconds: int = Field(default=120, ge=1, le=3600)
    maxExternalWaitSeconds: int = Field(default=86400, ge=1, le=31_536_000)
    maxTotalSeconds: int = Field(default=172800, ge=1, le=31_536_000)
    userInputTimeoutSeconds: int | None = Field(
        default=None, ge=1, le=31_536_000
    )
    maxAttempts: int = Field(default=8, ge=1, le=100)
    #: Optional deployment gate: name of a boolean OpenBoxConfig field that
    #: must be true for this operation to admit new jobs (greyed rollouts,
    #: e.g. skill_jobs_video_write). Unknown names fail closed.
    enabledConfigFlag: str | None = Field(default=None, max_length=80)
    #: Does a cancellation have to reach the handler? True for operations that
    #: create external state (a provider task, a remote media job): the
    #: handler must run to cancel it provider-side and settle against facts.
    #: False (default) means the operation owns nothing outside the ledger, so
    #: the runtime settles the cancel itself and never starts more work — the
    #: safe default for a skill that never declared otherwise.
    cancelRequiresHandler: bool = False


class CapabilitiesSpec(_StrictModel):
    secrets: list[str] = Field(default_factory=list)
    objectStorage: list[str] = Field(default_factory=list)
    remoteExecutors: list[str] = Field(default_factory=list)
    network: list[str] = Field(default_factory=list)


class PolicySpec(_StrictModel):
    billableOperations: list[str] = Field(default_factory=list)
    cancelOnSkillDisable: bool = False


class SkillManifest(_StrictModel):
    name: str = Field(max_length=150)
    version: str = Field(max_length=40)
    display_name: str = Field(default="", max_length=200)
    distribution: Literal["builtin", "user"] = "builtin"
    default_enabled: bool = True
    runtime: RuntimeSpec
    operations: dict[str, OperationSpec]
    #: phase name -> i18n label key; the UI renders only declared phases.
    phases: dict[str, str] = Field(default_factory=dict)
    capabilities: CapabilitiesSpec = Field(default_factory=CapabilitiesSpec)
    policy: PolicySpec = Field(default_factory=PolicySpec)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("name must be alphanumeric with - or _")
        return v

    @field_validator("operations")
    @classmethod
    def _at_least_one_operation(cls, v: dict) -> dict:
        if not v:
            raise ValueError("at least one operation is required")
        for name in v:
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 80
                or not all(char.isalnum() or char in "._-" for char in name)
            ):
                raise ValueError(f"invalid operation name: {name!r}")
        return v

    @property
    def skill_key(self) -> str:
        return f"builtin:{self.name}" if self.distribution == "builtin" else f"user:{self.name}"

    def operation(self, name: str) -> OperationSpec | None:
        return self.operations.get(name)


def parse_manifest(text: str, *, trusted: bool) -> SkillManifest:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a mapping")

    api_version = raw.get("apiVersion")
    if api_version not in SUPPORTED_API_VERSIONS:
        raise ManifestError(f"unsupported apiVersion: {api_version!r}")
    if raw.get("kind") != "Skill":
        raise ManifestError(f"unsupported kind: {raw.get('kind')!r}")

    unknown_root = sorted(
        str(key) for key in set(raw) - {"apiVersion", "kind", "metadata", "spec"}
    )
    if unknown_root:
        raise ManifestError(f"unknown manifest field(s): {', '.join(unknown_root)}")
    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ManifestError("metadata and spec must be mappings")
    unknown_metadata = sorted(
        str(key) for key in set(metadata) - {"name", "version", "displayName"}
    )
    if unknown_metadata:
        raise ManifestError(
            f"unknown metadata field(s): {', '.join(unknown_metadata)}"
        )
    unknown_spec = sorted(
        str(key)
        for key in set(spec)
        - {
            "distribution",
            "defaultEnabled",
            "runtime",
            "operations",
            "phases",
            "capabilities",
            "policy",
        }
    )
    if unknown_spec:
        raise ManifestError(f"unknown spec field(s): {', '.join(unknown_spec)}")
    try:
        manifest = SkillManifest(
            name=metadata.get("name", ""),
            version=str(metadata.get("version", "0.0.0")),
            display_name=metadata.get("displayName", ""),
            distribution=spec.get("distribution", "builtin"),
            # Let Pydantic validate/coerce YAML booleans. Python's bool("false")
            # is True and would silently enable a quoted false value.
            default_enabled=spec.get("defaultEnabled", True),
            runtime=spec.get("runtime") or {},
            operations=spec.get("operations") or {},
            phases=spec.get("phases") or {},
            capabilities=spec.get("capabilities") or {},
            policy=spec.get("policy") or {},
        )
    except ValidationError as e:
        raise ManifestError(f"invalid manifest: {e}") from e

    if not trusted:
        if manifest.runtime.kind == "internal":
            raise ManifestError("untrusted manifests may not select the internal runtime")
        if manifest.distribution == "builtin":
            raise ManifestError("untrusted manifests may not claim builtin distribution")
    unknown_billable = sorted(
        set(manifest.policy.billableOperations) - set(manifest.operations)
    )
    if unknown_billable:
        raise ManifestError(
            "billableOperations names unknown operation(s): "
            + ", ".join(unknown_billable)
        )
    for operation_name, operation in manifest.operations.items():
        for schema_name, schema in (
            ("inputSchema", operation.inputSchema),
            ("outputSchema", operation.outputSchema),
        ):
            if isinstance(schema, dict):
                _bounded_json(
                    schema,
                    label=f"operation {operation_name} {schema_name}",
                    max_bytes=MAX_INLINE_SCHEMA_BYTES,
                )
                validate_schema_definition(
                    schema,
                    label=f"operation {operation_name} {schema_name}",
                )
    invalid_phases = [
        name
        for name, label in manifest.phases.items()
        if not name or len(name) > 64 or not isinstance(label, str) or not label
    ]
    if invalid_phases:
        raise ManifestError(f"invalid phase declaration(s): {', '.join(invalid_phases)}")
    return manifest


# ---------------------------------------------------------------------------
# Builtin manifest catalog (image-shipped packages under backend/builtin_skills)
# ---------------------------------------------------------------------------

_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_skills"
_builtin_cache: dict[str, SkillManifest] | None = None


def load_builtin_manifests(*, refresh: bool = False) -> dict[str, SkillManifest]:
    global _builtin_cache
    if _builtin_cache is not None and not refresh:
        return _builtin_cache
    manifests: dict[str, SkillManifest] = {}
    if _BUILTIN_DIR.is_dir():
        for manifest_path in sorted(_BUILTIN_DIR.glob("*/skill.yaml")):
            try:
                manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"), trusted=True)
            except ManifestError as e:
                log.error(f"Skipping builtin manifest {manifest_path}: {e}")
                continue
            if manifest.skill_key in manifests:
                log.error(f"Duplicate builtin skill key {manifest.skill_key} at {manifest_path}")
                continue
            manifests[manifest.skill_key] = manifest
    _builtin_cache = manifests
    return manifests


def get_manifest(skill_key: str) -> SkillManifest | None:
    return load_builtin_manifests().get(skill_key)


def validate_input(operation: OperationSpec, input_data: dict) -> None:
    """Validate an admitted operation payload against its inline contract."""
    if not isinstance(input_data, dict):
        raise ManifestError("input must be an object")
    _bounded_json(input_data, label="input", max_bytes=MAX_INPUT_BYTES)
    schema = operation.inputSchema
    if isinstance(schema, dict):
        validate_schema_value(schema, input_data, label="input")
    elif schema is not None:
        raise ManifestError(
            "referenced inputSchema is not resolved by this runtime; use an inline schema"
        )


def _matches_json_type(expected: str, value) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def validate_schema_value(
    schema: dict,
    value,
    *,
    label: str = "value",
    _depth: int = 0,
) -> None:
    """Enforce the safe, commonly used JSON-Schema subset at trust boundaries.

    Builtin manifests and waiting-user cards currently use object properties,
    required fields, primitive types, enum/const and oneOf. Keeping this subset
    in-process avoids advertising a contract the server silently ignores while
    ignoring unsupported annotation keywords. Unknown ``type`` values are
    rejected rather than turning a schema typo into an authorization bypass.
    """
    if not isinstance(schema, dict) or not schema:
        return
    if _depth == 0:
        validate_schema_definition(schema, label=label)
    if _depth > 32:
        raise ManifestError(f"{label} schema nesting exceeds 32 levels")

    expected = schema.get("type")
    known_types = {"object", "array", "string", "boolean", "integer", "number", "null"}
    if isinstance(expected, str):
        if expected not in known_types:
            raise ManifestError(f"{label} schema declares unsupported type {expected!r}")
        if not _matches_json_type(expected, value):
            raise ManifestError(f"{label} must be {expected}")
    elif isinstance(expected, list):
        if not expected or any(
            not isinstance(item, str) or item not in known_types for item in expected
        ):
            raise ManifestError(f"{label} schema declares an unsupported type")
        if not any(_matches_json_type(item, value) for item in expected):
            raise ManifestError(f"{label} has an unsupported type")
    elif expected is not None:
        raise ManifestError(f"{label} schema type must be a string or list")

    if "const" in schema and value != schema["const"]:
        raise ManifestError(f"{label} must equal {schema['const']!r}")
    if "enum" in schema:
        enum_values = schema.get("enum")
        if not isinstance(enum_values, list) or not enum_values:
            raise ManifestError(f"{label} schema enum must be a non-empty list")
        if value not in enum_values:
            raise ManifestError(f"{label} is not one of the allowed values")

    if isinstance(value, dict):
        required = schema.get("required") or []
        if not isinstance(required, list) or any(
            not isinstance(key, str) for key in required
        ):
            raise ManifestError(f"{label} schema required must be a string list")
        missing = [key for key in required if key not in value]
        if missing:
            raise ManifestError(f"{label} missing required field(s): {', '.join(missing)}")
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            raise ManifestError(f"{label} schema properties must be an object")
        for key, child_schema in properties.items():
            if not isinstance(key, str) or not isinstance(child_schema, dict):
                raise ManifestError(f"{label} schema contains an invalid property")
            if key in value:
                validate_schema_value(
                    child_schema,
                    value[key],
                    label=f"{label}.{key}",
                    _depth=_depth + 1,
                )
        if schema.get("additionalProperties") is False:
            extras = sorted(str(key) for key in set(value) - set(properties))
            if extras:
                raise ManifestError(
                    f"{label} contains unsupported field(s): {', '.join(extras)}"
                )

    if isinstance(value, str):
        try:
            if "minLength" in schema and len(value) < int(schema["minLength"]):
                raise ManifestError(f"{label} is shorter than minLength")
            if "maxLength" in schema and len(value) > int(schema["maxLength"]):
                raise ManifestError(f"{label} is longer than maxLength")
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"{label} schema has an invalid string bound") from exc

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if "minimum" in schema and value < schema["minimum"]:
                raise ManifestError(f"{label} is below minimum")
            if "maximum" in schema and value > schema["maximum"]:
                raise ManifestError(f"{label} is above maximum")
        except TypeError as exc:
            raise ManifestError(f"{label} schema has an invalid numeric bound") from exc

    if isinstance(value, list):
        try:
            if "minItems" in schema and len(value) < int(schema["minItems"]):
                raise ManifestError(f"{label} has fewer than minItems")
            if "maxItems" in schema and len(value) > int(schema["maxItems"]):
                raise ManifestError(f"{label} has more than maxItems")
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"{label} schema has an invalid array bound") from exc
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_schema_value(
                    item_schema,
                    item,
                    label=f"{label}[{index}]",
                    _depth=_depth + 1,
                )

    branches = schema.get("oneOf")
    if "oneOf" in schema and not isinstance(branches, list):
        raise ManifestError(f"{label} schema oneOf must be a list")
    if isinstance(branches, list):
        matched = 0
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            try:
                validate_schema_value(
                    branch, value, label=label, _depth=_depth + 1
                )
            except ManifestError:
                continue
            matched += 1
        if matched != 1:
            raise ManifestError(f"{label} must match exactly one allowed shape")
