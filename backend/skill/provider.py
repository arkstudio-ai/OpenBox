"""Scoped Skill provider registry.

The registry in this module is deliberately an ordinary lifecycle-owned
object.  Agent/session code creates or obtains one from its sandbox client;
tests can create and dispose one without sharing process-global discovery
state.  Providers own I/O and revisions, while :class:`SkillRegistry` owns
scope shadowing, deterministic conflict resolution, LKG retention and safe
on-demand loading.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from core.log import create_logger
from core.markdown import MAX_DESCRIPTION_CHARS, clip_description, parse_frontmatter

log = create_logger("skill.provider")

_PROVIDER_ID = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_SOURCE_SCOPE = Literal["global", "user", "project", "workdir"]
_CACHE_TTL_SECONDS = 2.0
_CACHE_MAX_ENTRIES = 128
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_SOURCE_CHARS = 128
_MAX_STABLE_ID_CHARS = 2_048
_MAX_PATH_CHARS = 4_096
_MAX_LOCATOR_BYTES = 4_096
_MAX_METADATA_BYTES = 8_192
_MAX_ALLOWED_TOOLS = 64
_MAX_TOOL_NAME_CHARS = 128
_MAX_PROVIDER_CANDIDATES = 2_000
_MAX_CATALOG_CANDIDATES = 5_000
_MAX_PROVIDER_DIAGNOSTICS = 128
_MAX_DIAGNOSTIC_CHARS = 1_000
_MAX_REVISION_CHARS = 512
_MAX_PROVIDERS = 128
_MAX_HOST_SKILL_FILES = 2_000


@dataclass(frozen=True, slots=True)
class ScopeKey:
    """Tenant/project/workspace identity for every Skill lookup.

    ``workdir`` is never inferred here.  Session callers must pass the
    execution-plane directory they already resolved for that session.  Empty
    trailing fields intentionally describe a less-specific scope (for example
    ``ScopeKey(user_id="u1")`` is the user layer).
    """

    user_id: str = ""
    project_id: str = ""
    workdir: str = ""

    def __post_init__(self) -> None:
        for field_name in ("user_id", "project_id", "workdir"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"ScopeKey.{field_name} must be a string")
            if "\x00" in value:
                raise ValueError(f"ScopeKey.{field_name} cannot contain NUL")
            limit = _MAX_PATH_CHARS if field_name == "workdir" else 512
            if len(value) > limit:
                raise ValueError(f"ScopeKey.{field_name} exceeds {limit} characters")
        if self.project_id and not self.user_id:
            raise ValueError("project scope requires user_id")
        if self.workdir and not self.user_id:
            raise ValueError("workdir scope requires user_id")
        if self.workdir and not self.workdir.startswith("/"):
            raise ValueError("ScopeKey.workdir must be an absolute path")

    @property
    def level(self) -> _SOURCE_SCOPE:
        if self.workdir:
            return "workdir"
        if self.project_id:
            return "project"
        if self.user_id:
            return "user"
        return "global"

    def user_scope(self) -> "ScopeKey":
        return ScopeKey(user_id=self.user_id) if self.user_id else ScopeKey()

    def project_scope(self) -> "ScopeKey":
        if not self.project_id:
            return self.user_scope()
        return ScopeKey(user_id=self.user_id, project_id=self.project_id)


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    code: str
    message: str
    provider_id: str = ""
    skill: str = ""
    severity: Literal["info", "warning", "error"] = "warning"


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """Body-free provider contribution retained for a later verified load."""

    name: str
    description: str
    source: str
    scope: ScopeKey
    locator: Any
    stable_id: str
    path: str = ""
    allowed_tools: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    description: str
    source: str
    content: str
    path: str = ""
    base_dir: str = ""
    files: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    provider_id: str = ""
    provider_revision: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillProviderSnapshot:
    """One provider observation.

    ``complete=False`` is never authoritative absence.  ``available=True`` may
    accompany it when the provider is explicitly returning its own LKG.
    """

    candidates: tuple[SkillCandidate, ...]
    complete: bool
    revision: str
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    available: bool = True


@runtime_checkable
class SkillProvider(Protocol):
    """Lifecycle contract implemented by every Skill source."""

    id: str
    rank: int

    async def observe(self, scope: ScopeKey) -> SkillProviderSnapshot: ...

    async def list(self, scope: ScopeKey) -> Sequence[SkillCandidate]: ...

    async def load(
        self,
        scope: ScopeKey,
        candidate: SkillCandidate,
        *,
        revision: str,
    ) -> SkillDefinition | None: ...

    async def revision(self, scope: ScopeKey) -> str: ...

    def invalidate(self, scope: ScopeKey | None = None) -> None: ...

    async def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Selection:
    candidate: SkillCandidate
    provider_id: str
    provider_rank: int
    provider_revision: str


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    """Resolved catalog plus the exact selections used for safe loading."""

    scope: ScopeKey
    skills: tuple[SkillDefinition, ...]
    complete: bool
    revision: str
    diagnostics: tuple[SkillDiagnostic, ...]
    available: bool = True
    stale: bool = False
    _selections: tuple[_Selection, ...] = field(default=(), repr=False)

    def selection(self, name: str) -> _Selection | None:
        return next(
            (selection for selection in self._selections if selection.candidate.name == name),
            None,
        )


class SkillCatalogueUnavailable(RuntimeError):
    """No complete or last-known-good catalog exists for this scope."""


class SkillSnapshotStale(RuntimeError):
    """The provider revision changed after a skill was selected."""


class SkillScopeMismatch(PermissionError):
    """A selected catalog was presented to another tenant/project scope."""


@dataclass(slots=True)
class _CacheEntry:
    snapshot: SkillCatalogSnapshot
    expires_at: float


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_error(provider_id: str, phase: str, error: BaseException) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=f"provider_{phase}_failed",
        provider_id=provider_id,
        severity="error",
        message=f"Skill provider {provider_id!r} {phase} failed: {type(error).__name__}",
    )


def _validate_provider(provider: SkillProvider) -> None:
    provider_id = getattr(provider, "id", None)
    if (
        not isinstance(provider_id, str)
        or not provider_id
        or len(provider_id) > 128
        or any(character not in _PROVIDER_ID for character in provider_id)
    ):
        raise ValueError("Skill provider id is invalid")
    rank = getattr(provider, "rank", None)
    if not isinstance(rank, int) or isinstance(rank, bool):
        raise ValueError(f"Skill provider {provider_id!r} rank must be an integer")
    for method in ("observe", "list", "load", "revision", "invalidate", "dispose"):
        if not callable(getattr(provider, method, None)):
            raise TypeError(f"Skill provider {provider_id!r} is missing {method}()")


def _bounded_json(value: Any, *, field_name: str, max_bytes: int) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Skill candidate {field_name} must be finite JSON data") from exc
    if len(encoded) > max_bytes:
        raise ValueError(
            f"Skill candidate {field_name} exceeds {max_bytes} encoded bytes"
        )


def _validate_candidate(candidate: SkillCandidate, provider_id: str) -> None:
    if not isinstance(candidate.name, str) or not _SKILL_NAME.fullmatch(candidate.name):
        raise ValueError(f"Skill provider {provider_id!r} returned an invalid name")
    if candidate.name in {".", ".."}:
        raise ValueError(f"Skill provider {provider_id!r} returned an invalid name")
    if (
        not isinstance(candidate.description, str)
        or len(candidate.description) > MAX_DESCRIPTION_CHARS + 1
    ):
        raise ValueError(
            f"Skill provider {provider_id!r} returned an oversized description"
        )
    if (
        not isinstance(candidate.source, str)
        or not candidate.source
        or len(candidate.source) > _MAX_SOURCE_CHARS
    ):
        raise ValueError(f"Skill provider {provider_id!r} returned an invalid source")
    if (
        not isinstance(candidate.stable_id, str)
        or not candidate.stable_id
        or len(candidate.stable_id) > _MAX_STABLE_ID_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate.stable_id)
    ):
        raise ValueError(
            f"Skill provider {provider_id!r} returned an invalid stable id"
        )
    if not isinstance(candidate.path, str) or len(candidate.path) > _MAX_PATH_CHARS:
        raise ValueError(f"Skill provider {provider_id!r} returned an invalid path")
    if not isinstance(candidate.scope, ScopeKey):
        raise TypeError(f"Skill provider {provider_id!r} returned an invalid scope")
    if (
        not isinstance(candidate.allowed_tools, tuple)
        or len(candidate.allowed_tools) > _MAX_ALLOWED_TOOLS
        or any(
            not isinstance(tool, str)
            or not tool
            or len(tool) > _MAX_TOOL_NAME_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in tool)
            for tool in candidate.allowed_tools
        )
    ):
        raise ValueError(
            f"Skill provider {provider_id!r} returned invalid allowed-tools metadata"
        )
    if not isinstance(candidate.metadata, Mapping):
        raise TypeError(f"Skill provider {provider_id!r} returned invalid metadata")
    _bounded_json(
        candidate.locator,
        field_name="locator",
        max_bytes=_MAX_LOCATOR_BYTES,
    )
    _bounded_json(
        dict(candidate.metadata),
        field_name="metadata",
        max_bytes=_MAX_METADATA_BYTES,
    )


def _valid_revision(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= _MAX_REVISION_CHARS
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _validate_provider_snapshot(
    snapshot: SkillProviderSnapshot,
    provider_id: str,
) -> None:
    if not _valid_revision(snapshot.revision):
        raise ValueError(f"Skill provider {provider_id!r} returned an invalid revision")
    if len(snapshot.candidates) > _MAX_PROVIDER_CANDIDATES:
        raise ValueError(
            f"Skill provider {provider_id!r} returned too many candidates"
        )
    if len(snapshot.diagnostics) > _MAX_PROVIDER_DIAGNOSTICS:
        raise ValueError(
            f"Skill provider {provider_id!r} returned too many diagnostics"
        )
    for diagnostic in snapshot.diagnostics:
        if (
            not isinstance(diagnostic, SkillDiagnostic)
            or len(diagnostic.code) > 128
            or len(diagnostic.provider_id) > 128
            or len(diagnostic.skill) > 128
            or len(diagnostic.message) > _MAX_DIAGNOSTIC_CHARS
        ):
            raise ValueError(
                f"Skill provider {provider_id!r} returned an invalid diagnostic"
            )


def _scope_specificity(candidate: ScopeKey, requested: ScopeKey) -> int | None:
    if candidate.user_id and candidate.user_id != requested.user_id:
        return None
    if candidate.project_id and candidate.project_id != requested.project_id:
        return None
    if candidate.workdir and os.path.normpath(candidate.workdir) != os.path.normpath(
        requested.workdir
    ):
        return None
    if candidate.workdir:
        return 3
    if candidate.project_id:
        return 2
    if candidate.user_id:
        return 1
    return 0


def _scope_allows(snapshot: ScopeKey, caller: ScopeKey) -> bool:
    """A less-specific selected scope may be consumed by its descendant only."""
    if snapshot.user_id and snapshot.user_id != caller.user_id:
        return False
    if snapshot.project_id and snapshot.project_id != caller.project_id:
        return False
    if snapshot.workdir and os.path.normpath(snapshot.workdir) != os.path.normpath(
        caller.workdir
    ):
        return False
    return True


class SkillRegistry:
    """Merge scoped providers with revision-aware cache and lifecycle control."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        max_cache_entries: int = _CACHE_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be a finite non-negative number")
        if not isinstance(max_cache_entries, int) or max_cache_entries < 1:
            raise ValueError("max_cache_entries must be a positive integer")
        self._providers: dict[str, SkillProvider] = {}
        self._cache: OrderedDict[tuple[Any, ...], _CacheEntry] = OrderedDict()
        self._lkg: OrderedDict[ScopeKey, SkillCatalogSnapshot] = OrderedDict()
        self._inflight: dict[tuple[Any, ...], asyncio.Task[SkillCatalogSnapshot]] = {}
        self._epoch = 0
        self._ttl_seconds = ttl_seconds
        self._max_cache_entries = max_cache_entries
        self._clock = clock or time.monotonic
        self._disposed = False

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    @property
    def disposed(self) -> bool:
        return self._disposed

    def register(
        self, provider: SkillProvider
    ) -> Callable[[], Awaitable[None]]:
        if self._disposed:
            raise RuntimeError("SkillRegistry is disposed")
        _validate_provider(provider)
        if provider.id in self._providers:
            raise ValueError(f"Skill provider {provider.id!r} is already registered")
        if len(self._providers) >= _MAX_PROVIDERS:
            raise ValueError("SkillRegistry provider limit reached")
        self._providers[provider.id] = provider
        self._invalidate_local(drop_lkg=True)
        active = True

        async def unregister_exact() -> None:
            nonlocal active
            if active and self._providers.get(provider.id) is provider:
                active = False
                await self.unregister(provider.id)

        return unregister_exact

    async def unregister(self, provider_id: str) -> bool:
        provider = self._providers.pop(provider_id, None)
        if provider is None:
            return False
        self._invalidate_local(drop_lkg=True)
        await provider.dispose()
        return True

    def invalidate(
        self,
        provider_id: str | None = None,
        scope: ScopeKey | None = None,
        *,
        notify_provider: bool = True,
    ) -> None:
        if self._disposed:
            return
        if provider_id is not None:
            provider = self._providers.get(provider_id)
            if provider is None:
                return
            if notify_provider:
                provider.invalidate(scope)
        elif notify_provider:
            for provider in self._providers.values():
                provider.invalidate(scope)
        self._invalidate_local(scope)

    def _invalidate_local(
        self,
        scope: ScopeKey | None = None,
        *,
        drop_lkg: bool = False,
    ) -> None:
        self._epoch += 1
        if scope is None:
            self._cache.clear()
            if drop_lkg:
                self._lkg.clear()
            return
        for key in [key for key in self._cache if key[0] == scope]:
            self._cache.pop(key, None)
        if drop_lkg:
            self._lkg.pop(scope, None)

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        providers = list(self._providers.values())
        self._providers.clear()
        self._cache.clear()
        self._lkg.clear()
        inflight = list(self._inflight.values())
        self._inflight.clear()
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await asyncio.gather(
            *(provider.dispose() for provider in providers), return_exceptions=True
        )

    async def _revisions(
        self, scope: ScopeKey
    ) -> tuple[tuple[tuple[str, str], ...], tuple[SkillDiagnostic, ...]]:
        providers = [(provider_id, self._providers[provider_id]) for provider_id in sorted(self._providers)]
        results = await asyncio.gather(
            *(provider.revision(scope) for _, provider in providers),
            return_exceptions=True,
        )
        revisions: list[tuple[str, str]] = []
        diagnostics: list[SkillDiagnostic] = []
        for (provider_id, _provider), result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                diagnostics.append(_provider_error(provider_id, "revision", result))
                revisions.append((provider_id, "<unavailable>"))
            elif not _valid_revision(result):
                diagnostics.append(
                    SkillDiagnostic(
                        code="provider_revision_invalid",
                        message=f"Skill provider {provider_id!r} returned an invalid revision",
                        provider_id=provider_id,
                        severity="error",
                    )
                )
                revisions.append((provider_id, "<invalid>"))
            else:
                revisions.append((provider_id, result))
        return tuple(revisions), tuple(diagnostics)

    async def snapshot(self, scope: ScopeKey) -> SkillCatalogSnapshot:
        if self._disposed:
            raise RuntimeError("SkillRegistry is disposed")
        if not isinstance(scope, ScopeKey):
            raise TypeError("scope must be a ScopeKey")
        epoch: int | None = None
        revisions: tuple[tuple[str, str], ...] = ()
        revision_diagnostics: tuple[SkillDiagnostic, ...] = ()
        for _attempt in range(2):
            observed_epoch = self._epoch
            revisions, revision_diagnostics = await self._revisions(scope)
            if observed_epoch == self._epoch:
                epoch = observed_epoch
                break
        if epoch is None:
            diagnostic = SkillDiagnostic(
                code="registry_revision_raced",
                message="Skill provider registry kept changing during revision observation",
            )
            lkg = self._lkg.get(scope)
            if lkg is not None:
                self._lkg.move_to_end(scope)
                return replace(
                    lkg,
                    complete=False,
                    stale=True,
                    diagnostics=(*lkg.diagnostics, diagnostic),
                )
            return SkillCatalogSnapshot(
                scope=scope,
                skills=(),
                complete=False,
                revision=_digest({"scope": scope, "epoch": self._epoch}),
                diagnostics=(diagnostic,),
                available=False,
                stale=True,
            )

        key = (scope, epoch, revisions)
        cached = self._cache.get(key)
        if cached is not None and self._clock() < cached.expires_at:
            self._cache.move_to_end(key)
            return cached.snapshot

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._collect(scope, revisions, revision_diagnostics, epoch)
            )
            self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task and task.done():
                self._inflight.pop(key, None)

    async def list(self, scope: ScopeKey) -> list[SkillDefinition]:
        snapshot = await self.snapshot(scope)
        if not snapshot.available:
            raise SkillCatalogueUnavailable(
                "; ".join(diagnostic.message for diagnostic in snapshot.diagnostics)
                or "Skill catalog is unavailable"
            )
        return list(snapshot.skills)

    async def _collect(
        self,
        scope: ScopeKey,
        expected_revisions: tuple[tuple[str, str], ...],
        revision_diagnostics: tuple[SkillDiagnostic, ...],
        epoch: int,
    ) -> SkillCatalogSnapshot:
        providers = [(provider_id, self._providers[provider_id]) for provider_id in sorted(self._providers)]
        results = await asyncio.gather(
            *(provider.observe(scope) for _, provider in providers),
            return_exceptions=True,
        )
        observations: list[tuple[SkillProvider, SkillProviderSnapshot]] = []
        diagnostics = list(revision_diagnostics)
        complete = not revision_diagnostics
        unavailable = bool(revision_diagnostics)
        for (provider_id, provider), result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                diagnostics.append(_provider_error(provider_id, "observe", result))
                complete = False
                unavailable = True
                continue
            if not isinstance(result, SkillProviderSnapshot):
                diagnostics.append(
                    SkillDiagnostic(
                        code="provider_snapshot_invalid",
                        provider_id=provider_id,
                        severity="error",
                        message=f"Skill provider {provider_id!r} returned an invalid snapshot",
                    )
                )
                complete = False
                unavailable = True
                continue
            try:
                _validate_provider_snapshot(result, provider_id)
                for candidate in result.candidates:
                    if not isinstance(candidate, SkillCandidate):
                        raise TypeError("provider returned a non-candidate")
                    _validate_candidate(candidate, provider_id)
                    if _scope_specificity(candidate.scope, scope) is None:
                        raise SkillScopeMismatch(
                            "provider returned a candidate for another scope"
                        )
            except Exception as exc:
                diagnostics.append(
                    SkillDiagnostic(
                        code="provider_snapshot_rejected",
                        provider_id=provider_id,
                        severity="error",
                        message=(
                            f"Skill provider {provider_id!r} snapshot was rejected: "
                            f"{type(exc).__name__}"
                        ),
                    )
                )
                complete = False
                unavailable = True
                continue
            if result.revision != dict(expected_revisions).get(provider_id):
                diagnostics.append(
                    SkillDiagnostic(
                        code="provider_revision_raced",
                        provider_id=provider_id,
                        message=f"Skill provider {provider_id!r} changed during observation",
                    )
                )
                complete = False
            if not result.complete:
                complete = False
            if not result.available:
                unavailable = True
            diagnostics.extend(result.diagnostics)
            observations.append((provider, result))

        if epoch != self._epoch:
            complete = False
            diagnostics.append(
                SkillDiagnostic(
                    code="registry_revision_raced",
                    message="Skill provider registry changed during observation",
                )
            )

        try:
            selected, merge_diagnostics = self._merge(scope, observations)
        except Exception as exc:
            selected = {}
            merge_diagnostics = (
                SkillDiagnostic(
                    code="catalogue_merge_rejected",
                    severity="error",
                    message=f"Skill catalogue merge was rejected: {type(exc).__name__}",
                ),
            )
            complete = False
            unavailable = True
        diagnostics.extend(merge_diagnostics)
        catalog_revision = _digest(
            {
                "scope": (scope.user_id, scope.project_id, scope.workdir),
                "providers": expected_revisions,
                "epoch": epoch,
            }
        )
        fresh = self._materialize(
            scope,
            selected,
            complete=complete,
            available=not unavailable,
            stale=not complete,
            revision=catalog_revision,
            diagnostics=tuple(diagnostics),
        )

        if not complete:
            lkg = self._lkg.get(scope)
            if lkg is not None:
                self._lkg.move_to_end(scope)
                return replace(
                    lkg,
                    complete=False,
                    stale=True,
                    diagnostics=tuple([*lkg.diagnostics, *diagnostics]),
                )
            # A provider may explicitly supply its own LKG candidates.  This
            # is an available stale view, not authoritative live absence.
            if observations and not unavailable and selected:
                return fresh
            return replace(fresh, skills=(), _selections=(), available=False)

        self._lkg[scope] = fresh
        self._lkg.move_to_end(scope)
        while len(self._lkg) > self._max_cache_entries:
            self._lkg.popitem(last=False)
        key = (scope, self._epoch, expected_revisions)
        self._cache[key] = _CacheEntry(
            snapshot=fresh,
            expires_at=self._clock() + self._ttl_seconds,
        )
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)
        return fresh

    def _merge(
        self,
        scope: ScopeKey,
        observations: Sequence[tuple[SkillProvider, SkillProviderSnapshot]],
    ) -> tuple[dict[str, _Selection], tuple[SkillDiagnostic, ...]]:
        by_name: dict[str, list[tuple[int, int, str, str, SkillCandidate, str]]] = {}
        diagnostics: list[SkillDiagnostic] = []
        candidate_count = 0
        for provider, observation in observations:
            for candidate in observation.candidates:
                candidate_count += 1
                if candidate_count > _MAX_CATALOG_CANDIDATES:
                    raise ValueError("Resolved Skill catalog exceeds the candidate limit")
                if not isinstance(candidate, SkillCandidate):
                    raise TypeError(f"Skill provider {provider.id!r} returned a non-candidate")
                _validate_candidate(candidate, provider.id)
                specificity = _scope_specificity(candidate.scope, scope)
                if specificity is None:
                    diagnostics.append(
                        SkillDiagnostic(
                            code="candidate_scope_rejected",
                            provider_id=provider.id,
                            skill=candidate.name,
                            severity="error",
                            message=(
                                f"Skill {candidate.name!r} from provider {provider.id!r} "
                                "does not belong to the requested scope"
                            ),
                        )
                    )
                    continue
                by_name.setdefault(candidate.name, []).append(
                    (
                        -specificity,
                        provider.rank,
                        provider.id,
                        candidate.stable_id,
                        candidate,
                        observation.revision,
                    )
                )

        selected: dict[str, _Selection] = {}
        for name in sorted(by_name):
            contenders = sorted(by_name[name], key=lambda item: item[:4])
            winner = contenders[0]
            selected[name] = _Selection(
                candidate=winner[4],
                provider_id=winner[2],
                provider_rank=winner[1],
                provider_revision=winner[5],
            )
            for loser in contenders[1:]:
                same_scope = loser[0] == winner[0]
                diagnostics.append(
                    SkillDiagnostic(
                        code="skill_conflict",
                        provider_id=loser[2],
                        skill=name,
                        message=(
                            f"Skill {name!r} from provider {loser[2]!r} was shadowed "
                            f"by {winner[2]!r} ({'rank/provider-id' if same_scope else 'nearer scope'})"
                        ),
                    )
                )
        return selected, tuple(diagnostics)

    @staticmethod
    def _materialize(
        scope: ScopeKey,
        selected: Mapping[str, _Selection],
        *,
        complete: bool,
        available: bool,
        stale: bool,
        revision: str,
        diagnostics: tuple[SkillDiagnostic, ...],
    ) -> SkillCatalogSnapshot:
        summaries = tuple(
            SkillDefinition(
                name=selection.candidate.name,
                description=selection.candidate.description,
                source=selection.candidate.source,
                content="",
                path=selection.candidate.path,
                allowed_tools=selection.candidate.allowed_tools,
                provider_id=selection.provider_id,
                provider_revision=selection.provider_revision,
                metadata=selection.candidate.metadata,
            )
            for selection in (selected[name] for name in sorted(selected))
        )
        return SkillCatalogSnapshot(
            scope=scope,
            skills=summaries,
            complete=complete,
            revision=revision,
            diagnostics=diagnostics,
            available=available,
            stale=stale,
            _selections=tuple(selected[name] for name in sorted(selected)),
        )

    async def load(
        self,
        snapshot: SkillCatalogSnapshot,
        name: str,
        *,
        scope: ScopeKey | None = None,
    ) -> SkillDefinition | None:
        if self._disposed:
            raise RuntimeError("SkillRegistry is disposed")
        caller_scope = scope or snapshot.scope
        if not _scope_allows(snapshot.scope, caller_scope):
            raise SkillScopeMismatch("Skill snapshot belongs to another scope")
        if not snapshot.available:
            raise SkillCatalogueUnavailable("Skill catalog is unavailable")
        selection = snapshot.selection(name)
        if selection is None:
            return None
        provider = self._providers.get(selection.provider_id)
        if provider is None:
            raise SkillSnapshotStale("Selected Skill provider is no longer registered")
        before = await provider.revision(snapshot.scope)
        if before != selection.provider_revision:
            self._invalidate_local(snapshot.scope)
            raise SkillSnapshotStale(
                f"Skill provider {selection.provider_id!r} changed after selection"
            )
        definition = await provider.load(
            snapshot.scope,
            selection.candidate,
            revision=selection.provider_revision,
        )
        after = await provider.revision(snapshot.scope)
        if after != selection.provider_revision:
            self._invalidate_local(snapshot.scope)
            raise SkillSnapshotStale(
                f"Skill provider {selection.provider_id!r} changed while loading"
            )
        if definition is None:
            self._invalidate_local(snapshot.scope)
            return None
        if definition.name != selection.candidate.name:
            self._invalidate_local(snapshot.scope)
            raise SkillSnapshotStale("Loaded Skill identity does not match its selection")
        return replace(
            definition,
            provider_id=selection.provider_id,
            provider_revision=selection.provider_revision,
        )


def _normalize_tools(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return ()
    result: list[str] = []
    for item in values:
        if len(result) >= _MAX_ALLOWED_TOOLS:
            break
        if (
            isinstance(item, str)
            and item.strip()
            and len(item.strip()) <= _MAX_TOOL_NAME_CHARS
            and item.strip() not in result
        ):
            result.append(item.strip())
    return tuple(result)


def _host_fingerprint(roots: Sequence[Path]) -> tuple[str, tuple[SkillDiagnostic, ...], bool]:
    entries: list[tuple[str, int, int]] = []
    diagnostics: list[SkillDiagnostic] = []
    complete = True
    file_count = 0
    for root in roots:
        try:
            if not root.exists():
                entries.append((str(root), 0, 0))
                continue
            stat = root.stat()
            entries.append((str(root), stat.st_mtime_ns, stat.st_size))
            for md in root.rglob("SKILL.md"):
                file_count += 1
                if file_count > _MAX_HOST_SKILL_FILES:
                    complete = False
                    diagnostics.append(
                        SkillDiagnostic(
                            code="host_catalogue_too_large",
                            provider_id="host",
                            severity="error",
                            message="Host Skill catalogue exceeds the scan limit",
                        )
                    )
                    break
                md_stat = md.stat()
                entries.append((str(md), md_stat.st_mtime_ns, md_stat.st_size))
        except OSError as exc:
            complete = False
            diagnostics.append(
                SkillDiagnostic(
                    code="host_scan_failed",
                    provider_id="host",
                    severity="error",
                    message=f"Could not inspect Skill root {root}: {type(exc).__name__}",
                )
            )
    return _digest(entries), tuple(diagnostics), complete


class HostFilesystemSkillProvider:
    """Filesystem provider with fixed roots or explicit session workdirs."""

    def __init__(
        self,
        provider_id: str,
        rank: int,
        *,
        roots: Sequence[Path] = (),
        project: bool = False,
    ) -> None:
        self.id = provider_id
        self.rank = rank
        self._roots = tuple(Path(root) for root in roots)
        self._project = project
        self._invalidations: dict[ScopeKey, int] = {}
        self._epoch = 0
        self._disposed = False

    def _scope_and_roots(self, scope: ScopeKey) -> tuple[ScopeKey, tuple[Path, ...]]:
        if not self._project:
            return ScopeKey(), self._roots
        if not scope.workdir:
            return scope.project_scope(), ()
        base = Path(scope.workdir)
        roots = tuple(
            base / directory / "skills"
            for directory in (".openbox", ".openagent", ".claude", ".agents", ".cursor")
        )
        return scope, roots

    async def revision(self, scope: ScopeKey) -> str:
        if self._disposed:
            raise RuntimeError(f"Skill provider {self.id!r} is disposed")
        _candidate_scope, roots = self._scope_and_roots(scope)
        fingerprint, _diagnostics, _complete = await asyncio.to_thread(
            _host_fingerprint, roots
        )
        return f"{fingerprint}:{self._epoch}:{self._invalidations.get(scope, 0)}"

    async def observe(self, scope: ScopeKey) -> SkillProviderSnapshot:
        candidate_scope, roots = self._scope_and_roots(scope)
        revision = await self.revision(scope)
        fingerprint, diagnostics, complete = await asyncio.to_thread(
            _host_fingerprint, roots
        )
        expected_prefix = revision.split(":", 1)[0]
        if fingerprint != expected_prefix:
            complete = False
            diagnostics = (*diagnostics, SkillDiagnostic(
                code="host_revision_raced",
                provider_id=self.id,
                message=f"Skill provider {self.id!r} changed during scan",
            ))
        candidates: list[SkillCandidate] = []
        scan_diagnostics = list(diagnostics)
        observed_files = 0
        for root in roots:
            if not root.exists():
                continue
            try:
                files: list[Path] = []
                for skill_md in root.rglob("SKILL.md"):
                    observed_files += 1
                    if observed_files > _MAX_HOST_SKILL_FILES:
                        complete = False
                        scan_diagnostics.append(
                            SkillDiagnostic(
                                code="host_catalogue_too_large",
                                provider_id=self.id,
                                severity="error",
                                message="Host Skill catalogue exceeds the scan limit",
                            )
                        )
                        break
                    files.append(skill_md)
                files.sort(key=lambda path: path.as_posix())
            except OSError as exc:
                complete = False
                scan_diagnostics.append(_provider_error(self.id, "scan", exc))
                continue
            for skill_md in files:
                try:
                    raw = skill_md.read_text(encoding="utf-8")
                    metadata, _body = parse_frontmatter(raw)
                    name = str(metadata.get("name") or skill_md.parent.name)
                    description = clip_description(metadata.get("description", ""))
                    candidates.append(
                        SkillCandidate(
                            name=name,
                            description=description,
                            source="project" if self._project else "global",
                            scope=candidate_scope,
                            locator=str(skill_md),
                            stable_id=str(skill_md),
                            path=str(skill_md.parent),
                            allowed_tools=_normalize_tools(
                                metadata.get("allowed-tools")
                                or metadata.get("allowed_tools")
                                or metadata.get("tools")
                            ),
                            # Catalog snapshots retain routing/security fields
                            # only. Arbitrary frontmatter belongs to the
                            # on-demand body load, not the hot directory cache.
                            metadata={},
                        )
                    )
                except Exception as exc:
                    complete = False
                    scan_diagnostics.append(
                        SkillDiagnostic(
                            code="host_skill_invalid",
                            provider_id=self.id,
                            severity="error",
                            message=f"Could not parse {skill_md}: {type(exc).__name__}",
                        )
                    )
        final_revision = await self.revision(scope)
        if final_revision != revision:
            complete = False
            scan_diagnostics.append(
                SkillDiagnostic(
                    code="host_revision_raced",
                    provider_id=self.id,
                    message=f"Skill provider {self.id!r} changed while reading candidates",
                )
            )
        return SkillProviderSnapshot(
            candidates=tuple(candidates),
            complete=complete,
            revision=revision,
            diagnostics=tuple(scan_diagnostics),
            available=complete or bool(candidates),
        )

    async def list(self, scope: ScopeKey) -> Sequence[SkillCandidate]:
        return (await self.observe(scope)).candidates

    async def load(
        self,
        scope: ScopeKey,
        candidate: SkillCandidate,
        *,
        revision: str,
    ) -> SkillDefinition | None:
        if await self.revision(scope) != revision:
            raise SkillSnapshotStale(f"Skill provider {self.id!r} changed")
        skill_md = Path(str(candidate.locator))
        candidate_scope, roots = self._scope_and_roots(scope)
        try:
            resolved = skill_md.resolve(strict=True)
            if not any(resolved.is_relative_to(root.resolve()) for root in roots if root.exists()):
                raise SkillScopeMismatch("Selected host Skill escaped its scoped root")
            raw = resolved.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(raw)
            name = str(metadata.get("name") or resolved.parent.name)
        except FileNotFoundError:
            return None
        if name != candidate.name or candidate.scope != candidate_scope:
            raise SkillSnapshotStale("Host Skill identity changed after selection")
        if await self.revision(scope) != revision:
            raise SkillSnapshotStale(f"Skill provider {self.id!r} changed")
        return SkillDefinition(
            name=name,
            description=clip_description(metadata.get("description", "")),
            source=candidate.source,
            content=body,
            path=str(resolved.parent),
            allowed_tools=candidate.allowed_tools,
            metadata=dict(metadata),
        )

    def invalidate(self, scope: ScopeKey | None = None) -> None:
        self._epoch += 1
        if scope is None:
            self._invalidations.clear()
        else:
            self._invalidations[scope] = self._invalidations.get(scope, 0) + 1

    async def dispose(self) -> None:
        self._disposed = True
        self._invalidations.clear()


@dataclass(frozen=True, slots=True)
class _RemoteState:
    revision: str
    skills: tuple[Mapping[str, Any], ...]
    complete: bool
    available: bool
    diagnostic: SkillDiagnostic | None = None
    observed_at: float = 0.0


def _remote_candidate_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Retain a bounded allowlist from an untrusted remote catalogue row."""
    requires_mcp = row.get("requires_mcp")
    if not isinstance(requires_mcp, list):
        requires_mcp = []
    return {
        "icon": str(row.get("icon") or "")[:16],
        "homepage": str(row.get("homepage") or "")[:2_048],
        "requires_mcp": [
            str(item)[:128] for item in requires_mcp[:32]
        ],
        "install_dir": str(row.get("install_dir") or "")[:128],
        "package_digest": str(row.get("package_digest") or "")[:128],
    }


def _remote_catalogue_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Detach only bounded, body-free fields into the provider's LKG."""
    requires_mcp = row.get("requires_mcp")
    if not isinstance(requires_mcp, list):
        requires_mcp = []
    return {
        "name": str(row.get("name") or "")[:129],
        "description": clip_description(row.get("description", "")),
        "source": str(row.get("source") or "container")[:129],
        "install_dir": str(row.get("install_dir") or "")[:129],
        "package_digest": str(row.get("package_digest") or "")[:129],
        "icon": str(row.get("icon") or "")[:16],
        "homepage": str(row.get("homepage") or "")[:2_048],
        "requires_mcp": [str(item)[:128] for item in requires_mcp[:32]],
    }


class SandboxCatalogueSkillProvider:
    """Tenant-scoped WUYING/Action-Server Skill catalogue provider."""

    def __init__(self, sandbox, *, provider_id: str = "wuying-scoped", rank: int = 400):
        self.id = provider_id
        self.rank = rank
        self._sandbox = sandbox
        self._states: dict[ScopeKey, _RemoteState] = {}
        self._epoch = 0
        self._disposed = False
        self._bound_user_id: str | None = None

    def _check_scope(self, scope: ScopeKey) -> None:
        if not scope.user_id:
            raise SkillScopeMismatch("Sandbox Skill provider requires user_id")
        actual = str(getattr(self._sandbox, "user_scope", "") or "")
        if actual:
            from project.workspace import user_scope_for_identity

            if actual != user_scope_for_identity(scope.user_id):
                raise SkillScopeMismatch("Sandbox Skill provider belongs to another tenant")
        elif self._bound_user_id is None:
            # Legacy/test clients do not expose the pseudonymous execution
            # scope. Bind the provider instance to its first explicit tenant
            # so reusing that object can never turn into cross-user discovery.
            self._bound_user_id = scope.user_id
        elif self._bound_user_id != scope.user_id:
            raise SkillScopeMismatch("Legacy sandbox Skill provider is tenant-bound")

    async def _read(self, scope: ScopeKey) -> _RemoteState:
        self._check_scope(scope)
        get_state = getattr(self._sandbox, "get_catalogue_projection_state", None)
        if callable(get_state):
            state = await get_state()
            availability = getattr(state, "availability", "unavailable")
            payload = getattr(state, "snapshot", None)
            if availability not in {"available", "stale"} or not isinstance(payload, dict):
                previous = self._states.get(scope.user_scope())
                if previous is not None:
                    return replace(
                        previous,
                        complete=False,
                        diagnostic=SkillDiagnostic(
                            code="sandbox_catalogue_stale",
                            provider_id=self.id,
                            message="Using the sandbox Skill catalogue last-known-good view",
                        ),
                    )
                return _RemoteState(
                    revision=f"unavailable:{self._epoch}",
                    skills=(),
                    complete=False,
                    available=False,
                    diagnostic=SkillDiagnostic(
                        code="sandbox_catalogue_unavailable",
                        provider_id=self.id,
                        severity="error",
                        message="Sandbox Skill catalogue is unavailable",
                    ),
                )
            rows = payload.get("skills")
            if not isinstance(rows, list):
                raise ValueError("Sandbox Skill catalogue has malformed skills")
            safe_rows = tuple(
                _remote_catalogue_row(row)
                for row in rows[:_MAX_PROVIDER_CANDIDATES + 1]
                if isinstance(row, Mapping)
            )
            generation = str(
                payload.get("skills_generation")
                or payload.get("generation")
                or _digest(safe_rows)
            )
            provider_revision = f"{generation}:{self._epoch}"
            previous = self._states.get(scope.user_scope())
            # One tool-resolution pass may ask revision/observe through both
            # the remote and personal providers. SandboxClient deliberately
            # labels its second TTL hit "stale"; preserve the immediately
            # preceding authoritative read only for this small coalescing
            # window. It is not a freshness TTL and cannot mask a later outage.
            coalesced_complete = bool(
                availability == "stale"
                and previous is not None
                and previous.complete
                and previous.revision == provider_revision
                and time.monotonic() - previous.observed_at < 0.25
            )
            remote = _RemoteState(
                revision=provider_revision,
                skills=safe_rows,
                complete=availability == "available" or coalesced_complete,
                available=True,
                diagnostic=(
                    None
                    if availability == "available" or coalesced_complete
                    else SkillDiagnostic(
                        code="sandbox_catalogue_stale",
                        provider_id=self.id,
                        message="Using the sandbox Skill catalogue last-known-good view",
                    )
                ),
                observed_at=time.monotonic(),
            )
            self._states[scope.user_scope()] = remote
            return remote

        rows = await self._sandbox.list_skills()
        if not isinstance(rows, list):
            raise ValueError("Legacy sandbox Skill catalogue is malformed")
        safe_rows = tuple(
            _remote_catalogue_row(row)
            for row in rows[:_MAX_PROVIDER_CANDIDATES + 1]
            if isinstance(row, Mapping)
        )
        revision = _digest(safe_rows)
        remote = _RemoteState(
            revision=f"legacy:{revision}:{self._epoch}",
            skills=safe_rows,
            # A body/list legacy endpoint cannot prove an aggregate ETag but
            # this provider's single Skill list is a complete observation.
            complete=True,
            available=True,
            observed_at=time.monotonic(),
        )
        self._states[scope.user_scope()] = remote
        return remote

    async def revision(self, scope: ScopeKey) -> str:
        if self._disposed:
            raise RuntimeError(f"Skill provider {self.id!r} is disposed")
        return (await self._read(scope)).revision

    async def observe(self, scope: ScopeKey) -> SkillProviderSnapshot:
        state = await self._read(scope)
        candidates = tuple(
            SkillCandidate(
                name=str(row.get("name") or ""),
                description=clip_description(row.get("description", "")),
                source=str(row.get("source") or "container"),
                scope=scope.user_scope(),
                locator={
                    "name": str(row.get("name") or ""),
                    "install_dir": str(row.get("install_dir") or ""),
                    "package_digest": str(row.get("package_digest") or ""),
                },
                stable_id="|".join(
                    (
                        str(row.get("name") or ""),
                        str(row.get("install_dir") or ""),
                        str(row.get("package_digest") or ""),
                    )
                ),
                path=str(row.get("base_dir") or ""),
                metadata=_remote_candidate_metadata(row),
            )
            for row in state.skills
            if row.get("name")
        )
        return SkillProviderSnapshot(
            candidates=candidates,
            complete=state.complete,
            revision=state.revision,
            diagnostics=(state.diagnostic,) if state.diagnostic else (),
            available=state.available,
        )

    async def list(self, scope: ScopeKey) -> Sequence[SkillCandidate]:
        return (await self.observe(scope)).candidates

    async def load(
        self,
        scope: ScopeKey,
        candidate: SkillCandidate,
        *,
        revision: str,
    ) -> SkillDefinition | None:
        self._check_scope(scope)
        if await self.revision(scope) != revision:
            raise SkillSnapshotStale("Sandbox Skill catalogue changed after selection")
        locator = candidate.locator if isinstance(candidate.locator, Mapping) else {}
        requested = str(locator.get("install_dir") or locator.get("name") or candidate.name)
        payload = await self._sandbox.get_skill(requested)
        if not isinstance(payload, Mapping):
            raise ValueError("Sandbox Skill body is malformed")
        loaded_name = str(payload.get("name") or candidate.name)
        loaded_dir = str(payload.get("install_dir") or "")
        expected_dir = str(locator.get("install_dir") or "")
        if loaded_name != candidate.name or (expected_dir and loaded_dir and loaded_dir != expected_dir):
            raise SkillSnapshotStale("Sandbox Skill identity changed after selection")
        if await self.revision(scope) != revision:
            raise SkillSnapshotStale("Sandbox Skill catalogue changed while loading")
        content = str(payload.get("content") or "")
        return SkillDefinition(
            name=loaded_name,
            description=clip_description(payload.get("description", candidate.description)),
            source=str(payload.get("source") or candidate.source),
            content=content,
            base_dir=str(payload.get("base_dir") or ""),
            files=tuple(str(item) for item in (payload.get("files") or [])[:50]),
            metadata=dict(payload),
        )

    def invalidate(self, scope: ScopeKey | None = None) -> None:
        self._epoch += 1
        if scope is None:
            self._states.clear()
        else:
            self._states.pop(scope.user_scope(), None)

    async def dispose(self) -> None:
        self._disposed = True
        self._states.clear()
        self._bound_user_id = None


class PersonalLibrarySkillProvider:
    """Owner-filtered durable personal library projected through the sandbox."""

    def __init__(
        self,
        sandbox_provider: SandboxCatalogueSkillProvider,
        *,
        provider_id: str = "personal-user-library",
        rank: int = 200,
        list_owned: Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]] | None = None,
    ) -> None:
        self.id = provider_id
        self.rank = rank
        self._sandbox_provider = sandbox_provider
        self._list_owned = list_owned
        self._epoch = 0
        self._owned_cache: dict[ScopeKey, tuple[str, tuple[Mapping[str, Any], ...]]] = {}
        self._disposed = False

    async def _owned(self, scope: ScopeKey) -> tuple[str, tuple[Mapping[str, Any], ...]]:
        if not scope.user_id:
            raise SkillScopeMismatch("Personal Skill provider requires user_id")
        loader = self._list_owned
        if loader is None:
            from skill.user_library import list_owned_skills

            loader = list_owned_skills
        rows = await loader(scope.user_id)
        if len(rows) > _MAX_PROVIDER_CANDIDATES:
            raise ValueError("Personal Skill library exceeds the candidate limit")
        detached = tuple(
            {
                "id": str(row.get("id") or "")[:128],
                "name": str(row.get("name") or "")[:129],
                "install_dir": str(row.get("install_dir") or "")[:129],
                "version": row.get("version"),
                "lifecycle_generation": row.get("lifecycle_generation"),
                "updated_at": str(row.get("updated_at") or "")[:128],
            }
            for row in rows[:_MAX_PROVIDER_CANDIDATES + 1]
            if isinstance(row, Mapping)
        )
        revision = _digest(
            [
                (
                    row.get("id"),
                    row.get("name"),
                    row.get("install_dir"),
                    row.get("version"),
                    row.get("lifecycle_generation"),
                    row.get("updated_at"),
                )
                for row in detached
            ]
        )
        result = (f"{revision}:{self._epoch}", detached)
        self._owned_cache[scope.user_scope()] = result
        return result

    async def revision(self, scope: ScopeKey) -> str:
        if self._disposed:
            raise RuntimeError(f"Skill provider {self.id!r} is disposed")
        owned_revision, _rows = await self._owned(scope)
        remote_revision = await self._sandbox_provider.revision(scope)
        return f"{owned_revision}|{remote_revision}"

    async def observe(self, scope: ScopeKey) -> SkillProviderSnapshot:
        owned_revision, owned = await self._owned(scope)
        remote = await self._sandbox_provider.observe(scope)
        revision = f"{owned_revision}|{remote.revision}"
        by_name = {str(row.get("name") or ""): row for row in owned if row.get("name")}
        by_dir = {
            str(row.get("install_dir") or ""): row
            for row in owned
            if row.get("install_dir")
        }
        candidates: list[SkillCandidate] = []
        for candidate in remote.candidates:
            locator = candidate.locator if isinstance(candidate.locator, Mapping) else {}
            owned_row = by_name.get(candidate.name) or by_dir.get(
                str(locator.get("install_dir") or "")
            )
            if owned_row is None:
                continue
            candidates.append(
                replace(
                    candidate,
                    source="personal",
                    scope=scope.user_scope(),
                    stable_id=f"{owned_row.get('id', '')}|{candidate.stable_id}",
                    metadata={**dict(candidate.metadata), "library_id": owned_row.get("id")},
                )
            )
        return SkillProviderSnapshot(
            candidates=tuple(candidates),
            complete=remote.complete,
            revision=revision,
            diagnostics=remote.diagnostics,
            available=remote.available,
        )

    async def list(self, scope: ScopeKey) -> Sequence[SkillCandidate]:
        return (await self.observe(scope)).candidates

    async def load(
        self,
        scope: ScopeKey,
        candidate: SkillCandidate,
        *,
        revision: str,
    ) -> SkillDefinition | None:
        current = await self.revision(scope)
        if current != revision:
            raise SkillSnapshotStale("Personal Skill library changed after selection")
        remote_revision = revision.rsplit("|", 1)[-1]
        return await self._sandbox_provider.load(
            scope, candidate, revision=remote_revision
        )

    def invalidate(self, scope: ScopeKey | None = None) -> None:
        self._epoch += 1
        if scope is None:
            self._owned_cache.clear()
        else:
            self._owned_cache.pop(scope.user_scope(), None)

    async def dispose(self) -> None:
        self._disposed = True
        self._owned_cache.clear()


def create_default_skill_registry(
    sandbox=None,
    *,
    ttl_seconds: float = _CACHE_TTL_SECONDS,
) -> SkillRegistry:
    """Create a lifecycle-owned registry with OpenBox's standard providers."""
    registry = SkillRegistry(ttl_seconds=ttl_seconds)
    builtin_root = Path(__file__).resolve().parents[1] / ".openbox" / "skills"
    registry.register(
        HostFilesystemSkillProvider(
            "host-builtin",
            600,
            roots=(builtin_root,),
        )
    )
    registry.register(
        HostFilesystemSkillProvider(
            "host-project",
            100,
            project=True,
        )
    )
    if sandbox is not None:
        remote = SandboxCatalogueSkillProvider(sandbox)
        registry.register(remote)
        registry.register(PersonalLibrarySkillProvider(remote))
    return registry


def skill_registry_for(sandbox=None) -> SkillRegistry:
    """Return the sandbox-owned registry, never a process-global tenant map."""
    if sandbox is None:
        return create_default_skill_registry(None)
    registry = getattr(sandbox, "_openbox_skill_registry", None)
    if isinstance(registry, SkillRegistry) and not registry.disposed:
        return registry
    registry = create_default_skill_registry(sandbox)
    try:
        setattr(sandbox, "_openbox_skill_registry", registry)
    except Exception:
        # Slot/frozen legacy fixtures still get a correctly scoped ephemeral
        # registry; their lifecycle is the current call.
        pass
    return registry


async def dispose_skill_registry_for(sandbox) -> None:
    registry = getattr(sandbox, "_openbox_skill_registry", None)
    if isinstance(registry, SkillRegistry):
        await registry.dispose()
        try:
            delattr(sandbox, "_openbox_skill_registry")
        except Exception:
            pass
