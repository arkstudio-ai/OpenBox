"""Qdrant REST adapter, with versioned IDs and metadata-only payloads."""
import hashlib
import json
import os
import re
import time
import uuid

import httpx

from memory.index.base import DocumentSnapshot, IndexHit
from memory.index.lexical import TOKENIZER_VERSION
from memory.providers.common import MemoryProviderError, response_json


def index_config(config) -> dict:
    return {"provider": "bailian", "model": config.embedding_model,
            "dimensions": config.embedding_dimensions, "distance": "Cosine",
            "normalization": "provider", "chunker": "bounded-paragraph-v1",
            "chunk_chars": config.source_chunk_chars, "tokenizer": TOKENIZER_VERSION}


def config_hash(config) -> str:
    return hashlib.sha256(json.dumps(index_config(config), sort_keys=True).encode()).hexdigest()


def chunks_for(text: str, max_chars: int) -> list[str]:
    # Bound by characters before calling providers. Keep offsets deterministic.
    return [text[start:start + max_chars] for start in range(0, len(text), max_chars)] or [""]


class QdrantMemoryIndex:
    def __init__(self, config, *, generation: str | None = None, client=None):
        self.config = config
        self.generation = generation or config.index_generation
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.generation):
            raise ValueError("Invalid memory index generation")
        self.collection = "openbox_" + self.generation.replace("-", "_")
        self.client = client
        self.fingerprint = config_hash(config)

    async def _request(self, method: str, path: str, body=None, *, missing_ok=False):
        client = self.client or httpx.AsyncClient(timeout=self.config.provider_timeout_seconds)
        headers = {}
        if os.getenv("QDRANT_API_KEY"):
            headers["api-key"] = os.environ["QDRANT_API_KEY"]
        try:
            response = await client.request(method, self.config.qdrant_url.rstrip("/") + path,
                                            headers=headers, json=body)
            if missing_ok and response.status_code == 404:
                return None
            data = response_json(response)
            return data.get("result")
        except httpx.TimeoutException:
            raise MemoryProviderError("qdrant_timeout") from None
        except httpx.HTTPError:
            raise MemoryProviderError("qdrant_unavailable") from None
        finally:
            if self.client is None:
                await client.aclose()

    async def ensure_collection(self):
        current = await self._request("GET", f"/collections/{self.collection}", missing_ok=True)
        if current is None:
            await self._request("PUT", f"/collections/{self.collection}",
                {"vectors": {"size": self.config.embedding_dimensions, "distance": "Cosine"}})
            current = await self._request("GET", f"/collections/{self.collection}")
            for key in ("user_id", "workspace_id", "project_id", "object_id", "kind", "visibility", "config_hash"):
                await self._request("PUT", f"/collections/{self.collection}/index?wait=true",
                                    {"field_name": key, "field_schema": "keyword"})
        vectors = current.get("config", {}).get("params", {}).get("vectors", {})
        if vectors.get("size") != self.config.embedding_dimensions or vectors.get("distance") != "Cosine":
            raise MemoryProviderError("index_configuration_conflict")

    async def upsert_version(self, document: DocumentSnapshot, vectors: list[list[float]], chunks: list[str]) -> list[str]:
        if len(vectors) != len(chunks) or not chunks:
            raise ValueError("Index chunks and vectors must match")
        await self.ensure_collection()
        points, ids = [], []
        for offset, (vector, chunk) in enumerate(zip(vectors, chunks, strict=True)):
            chunk_hash = hashlib.sha256(chunk.encode()).hexdigest()
            identity = f"{document.kind}:{document.id}:{document.revision}:{offset}:{chunk_hash}:{self.generation}"
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "openbox-memory:" + identity))
            ids.append(point_id)
            payload = {"kind": document.kind, "object_id": document.id, "revision": document.revision,
                       "chunk_id": str(offset), "chunk_hash": chunk_hash, "user_id": document.user_id,
                       "workspace_id": document.workspace_id, "project_id": document.project_id or "",
                       "visibility": "PERSONAL", "acl_epoch": document.acl_epoch,
                       "content_hash": document.content_hash, "index_generation": self.generation,
                       "config_hash": self.fingerprint}
            if document.expires_at:
                from datetime import datetime
                payload["expires_at"] = datetime.fromisoformat(document.expires_at).timestamp()
            points.append({"id": point_id, "vector": vector, "payload": payload})
        result = await self._request("PUT", f"/collections/{self.collection}/points?wait=true", {"points": points})
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise MemoryProviderError("index_write_unconfirmed")
        return ids

    async def delete_object_versions(self, kind: str, object_id: str, *, keep_revision: int | None = None):
        if await self._request("GET", f"/collections/{self.collection}", missing_ok=True) is None:
            return
        query = {"must": [{"key": "kind", "match": {"value": kind}},
                           {"key": "object_id", "match": {"value": object_id}}]}
        if keep_revision is not None:
            # Strictly older only: a slow worker for r1 must never remove r2,
            # which a faster worker may have written while it waited.
            query["must"].append({"key": "revision", "range": {"lt": keep_revision}})
        result = await self._request("POST", f"/collections/{self.collection}/points/delete?wait=true", {"filter": query})
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise MemoryProviderError("index_delete_unconfirmed")

    async def delete_revision(self, kind: str, object_id: str, revision: int):
        if await self._request("GET", f"/collections/{self.collection}", missing_ok=True) is None:
            return
        query = {"must": [{"key": "kind", "match": {"value": kind}},
                          {"key": "object_id", "match": {"value": object_id}},
                          {"key": "revision", "match": {"value": revision}}]}
        result = await self._request("POST", f"/collections/{self.collection}/points/delete?wait=true", {"filter": query})
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise MemoryProviderError("index_delete_unconfirmed")

    def _scope_filter(self, scope, kind: str) -> dict:
        must = [{"key": "user_id", "match": {"value": scope.actor_user_id}},
                {"key": "workspace_id", "match": {"value": scope.workspace_id}},
                {"key": "visibility", "match": {"value": "PERSONAL"}},
                {"key": "kind", "match": {"value": kind}},
                {"key": "config_hash", "match": {"value": self.fingerprint}}]
        projects = [""] + (list(scope.project_ids) if scope.include_all_projects else [scope.project_id] if scope.project_id else [])
        must.append({"key": "project_id", "match": {"any": projects}})
        return {"must": must, "must_not": [{"key": "expires_at", "range": {"lte": time.time()}}]}

    async def search(self, vector: list[float], scope, *, kind: str, limit: int) -> list[IndexHit]:
        result = await self._request("POST", f"/collections/{self.collection}/points/query",
            {"query": vector, "filter": self._scope_filter(scope, kind), "limit": max(1, min(limit, 100)),
             "with_payload": ["kind", "object_id", "revision", "chunk_id"], "with_vector": False})
        if not isinstance(result, dict) or not isinstance(result.get("points"), list):
            raise MemoryProviderError("invalid_response")
        hits = []
        for point in result["points"]:
            payload = point.get("payload", {})
            if payload.get("kind") != kind:
                continue
            hits.append(IndexHit(kind, str(payload["object_id"]), int(payload["revision"]),
                                 float(point["score"]), payload.get("chunk_id")))
        return hits

    async def point_ids(self, kind: str, object_id: str) -> list[str]:
        if await self._request("GET", f"/collections/{self.collection}", missing_ok=True) is None:
            return []
        ids, offset = [], None
        while True:
            body = {"filter": {"must": [{"key": "kind", "match": {"value": kind}},
                                        {"key": "object_id", "match": {"value": object_id}}]},
                    "limit": 100, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            result = await self._request("POST", f"/collections/{self.collection}/points/scroll", body)
            ids.extend(str(point["id"]) for point in result["points"])
            offset = result.get("next_page_offset")
            if offset is None:
                return ids

    async def health(self) -> dict:
        try:
            root = await self._request("GET", "/")
            collection = await self._request("GET", f"/collections/{self.collection}", missing_ok=True)
            return {"status": "ready" if collection else "not_initialized", "generation": self.generation,
                    "collection": self.collection, "config_hash": self.fingerprint,
                    "points_count": collection.get("points_count") if collection else 0}
        except MemoryProviderError as exc:
            return {"status": "unavailable", "reason_code": exc.code, "generation": self.generation}

    async def reconcile(self, scope, *, limit=200, cursor=None):
        from memory.reconcile import reconcile
        return await reconcile(scope, self.config, generation=self.generation, limit=limit, cursor=cursor)

    async def rebuild_generation(self, scope, *, limit=200, cursor=None):
        from memory.reconcile import rebuild_generation
        return await rebuild_generation(scope, self.config, generation=self.generation, limit=limit, cursor=cursor)
