"""skill.yaml — the machine contract of an executable skill (§5.1).

SKILL.md speaks to the model; the manifest speaks to the platform: runtime
kind, operations with their three-layer timeouts, declared phases with i18n
label keys, capability ceilings and policy. Untrusted (user-installed)
manifests can never select the internal runtime.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from core.log import create_logger

log = create_logger("skill_runtime.manifest")

SUPPORTED_API_VERSIONS = {"openbox.ai/v1"}


class ManifestError(Exception):
    pass


class RuntimeSpec(BaseModel):
    kind: Literal["internal", "sandbox"]
    handler: str
    handlerVersion: int = 1


class OperationSpec(BaseModel):
    inputSchema: dict | str | None = None
    outputSchema: dict | str | None = None
    queue: str = "default"
    #: Three-layer timeouts (§5.1): one bounded invocation, cumulative
    #: external wait, and the job's total deadline.
    invocationTimeoutSeconds: int = Field(default=120, ge=1, le=3600)
    maxExternalWaitSeconds: int = Field(default=86400, ge=1)
    maxTotalSeconds: int = Field(default=172800, ge=1)
    userInputTimeoutSeconds: int | None = Field(default=None, ge=1)
    maxAttempts: int = Field(default=8, ge=1, le=100)
    #: Optional deployment gate: name of a boolean OpenBoxConfig field that
    #: must be true for this operation to admit new jobs (greyed rollouts,
    #: e.g. skill_jobs_video_write). Unknown names fail closed.
    enabledConfigFlag: str | None = None


class CapabilitiesSpec(BaseModel):
    secrets: list[str] = []
    objectStorage: list[str] = []
    remoteExecutors: list[str] = []
    network: list[str] = []


class PolicySpec(BaseModel):
    billableOperations: list[str] = []
    cancelOnSkillDisable: bool = False


class SkillManifest(BaseModel):
    name: str
    version: str
    display_name: str = ""
    distribution: Literal["builtin", "user"] = "builtin"
    default_enabled: bool = True
    runtime: RuntimeSpec
    operations: dict[str, OperationSpec]
    #: phase name -> i18n label key; the UI renders only declared phases.
    phases: dict[str, str] = {}
    capabilities: CapabilitiesSpec = CapabilitiesSpec()
    policy: PolicySpec = PolicySpec()

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

    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    try:
        manifest = SkillManifest(
            name=metadata.get("name", ""),
            version=str(metadata.get("version", "0.0.0")),
            display_name=metadata.get("displayName", ""),
            distribution=spec.get("distribution", "builtin"),
            default_enabled=bool(spec.get("defaultEnabled", True)),
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
    """Minimal input validation: inline schemas enforce type/object shape and
    required keys. Full JSON Schema enforcement is a hardening follow-up."""
    if not isinstance(input_data, dict):
        raise ManifestError("input must be an object")
    schema = operation.inputSchema
    if isinstance(schema, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in input_data]
        if missing:
            raise ManifestError(f"input missing required field(s): {', '.join(missing)}")
