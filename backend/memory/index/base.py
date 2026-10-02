from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DocumentSnapshot:
    kind: str
    id: str
    revision: int
    text: str
    user_id: str
    workspace_id: str
    project_id: str | None
    acl_epoch: int
    content_hash: str
    sources: tuple[dict, ...] = ()
    valid_from: str | None = None
    valid_to: str | None = None
    expires_at: str | None = None
    confirmation_status: str = "CONFIRMED"
    category: str | None = None


@dataclass(frozen=True)
class IndexHit:
    kind: str
    id: str
    revision: int
    score: float
    chunk_id: str | None = None


class MemoryIndex(Protocol):
    async def upsert_version(self, document: DocumentSnapshot, vectors: list[list[float]], chunks: list[str]) -> list[str]: ...
    async def delete_object_versions(self, kind: str, object_id: str, *, keep_revision: int | None = None) -> None: ...
    async def search(self, vector: list[float], scope, *, kind: str, limit: int) -> list[IndexHit]: ...
    async def health(self) -> dict: ...
    async def reconcile(self, scope, *, limit: int = 200, cursor: str | None = None) -> dict: ...
    async def rebuild_generation(self, scope, *, limit: int = 200, cursor: str | None = None) -> dict: ...
