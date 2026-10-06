"""Memory observations use the existing exact-request business source chain."""
from copy import deepcopy
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant import memory
from assistant.commands import command_digest
from assistant.knowledge_provenance import KnowledgeReference
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot, clean_snapshot
from db.base import get_db_session

OPERATIONS = frozenset({"memory.search", "memory.read"})


class ScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly include the actor's owned live projects and personal background; omit project_id.")

    @model_validator(mode="after")
    def one_selection(self):
        memory._filters(self.project_id, self.include_all_projects)
        return self


class SearchArgs(ScopeArgs):
    query: str = Field(min_length=1, max_length=500, description="Search confirmed memories and uploaded document text with existing BM25 and Qdrant retrieval.")
    limit: int = Field(default=10, ge=1, le=memory.MAX_ITEMS, strict=True)


class MemoryReference(KnowledgeReference):
    kind: Literal["memory", "source"]

    @model_validator(mode="before")
    @classmethod
    def exact_reference(cls, value):
        if not memory.valid_reference(value):
            raise ValueError("Use the complete source_ref returned by memory.search")
        return value


class ReadArgs(ScopeArgs):
    source_ref: MemoryReference = Field(description="Complete unchanged source_ref from memory.search.")
    source_id: str | None = Field(default=None, min_length=1, max_length=64,
        description="Omit to read the confirmed memory or uploaded text chunk. Select an exact sources[].id for its available original evidence; a complete chunk need not be the complete document.")
    max_chars: int = Field(default=8000, ge=1, le=memory.MAX_READ_CHARS, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)


def _unverified():
    return AssistantError(410, "ASSISTANT_MEMORY_UNVERIFIED", "The original memory observation is unavailable")


async def _access(db, main, args):
    return await memory._access(db, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id,
        project_id=args.project_id, include_all_projects=args.include_all_projects)


def _position(token, selection, *, fresh):
    if token is None:
        return None
    value = memory._cursor(token, selection=selection, check_expiry=fresh)
    return {key: value[key] for key in ("version", "offset")}


def _proof(main, args, value, operation):
    if operation == "memory.search":
        resource = {"kind": operation, "arguments_hash": command_digest(args.model_dump()),
            "observation_hash": command_digest(value), "entries": [item["source_ref"] for item in value["items"]]}
    else:
        # Scope comes from the checked response. It is checked against a fresh
        # SQL scope again on every validation; it cannot expand a read.
        from types import SimpleNamespace
        scope = SimpleNamespace(user_id=main.user_id, **value["scope"])
        selection = memory._selection(scope, main.id, args.source_ref.model_dump(), args.source_id,
                                      args.max_chars, value["projection_hash"])
        resource = {"kind": operation, "cursor_scope": selection,
            "start": _position(args.cursor, selection, fresh=False),
            "next": _position(value["next_cursor"], selection, fresh=False),
            "source_ref": args.source_ref.model_dump(), "source_id": args.source_id,
            **{key: value[key] for key in ("projection_version", "projection_hash", "chunk_hash",
                                         "offset", "end_offset", "total_chars")}}
    return {"resources": [resource], "tasks": []}


async def capture(main, arguments, *, operation):
    if operation not in OPERATIONS:
        raise _unverified()
    args = (SearchArgs if operation == "memory.search" else ReadArgs).model_validate(arguments)
    value = await (memory.search if operation == "memory.search" else memory.read)(
        user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id, **args.model_dump())
    return value, {"version": 2, "operation": operation, "arguments": deepcopy(arguments),
        "digest": command_digest(value), "projection": value, "sources": _proof(main, args, value, operation)}


async def validate(main, snapshot, *, fresh=False):
    try:
        operation = snapshot["operation"]
        if operation not in OPERATIONS:
            raise ValueError()
        args = (SearchArgs if operation == "memory.search" else ReadArgs).model_validate(snapshot["arguments"])
        value = snapshot["projection"]
        if (not isinstance(value, dict) or value.get("untrusted_data") is not True
                or not isinstance(value.get("scope"), dict)
                or len(json.dumps(snapshot, ensure_ascii=False).encode()) > 2 * memory.MAX_RESPONSE_BYTES):
            raise ValueError()
        if operation == "memory.search":
            if (set(value) != {"items", "status", "scope", "untrusted_data", "search"}
                    or not isinstance(value["items"], list) or len(value["items"]) > args.limit
                    or value["status"] != ("evidence" if value["items"] else "no_available_evidence")
                    or not isinstance(value["search"], dict)
                    or value["search"].get("bounded") is not True or value["search"].get("exhaustive") is not False):
                raise ValueError()
            refs = [item["source_ref"] for item in value["items"]]
        else:
            if (set(value) != {"item", "selected_source", "text", "offset", "end_offset", "total_chars",
                    "projection_version", "projection_hash", "chunk_hash", "next_cursor", "truncated", "untrusted_data", "scope"}
                    or not isinstance(value["text"], str) or not 1 <= len(value["text"]) <= args.max_chars
                    or any(type(value[key]) is not int for key in ("offset", "end_offset", "total_chars"))
                    or not 0 <= value["offset"] < value["end_offset"] <= value["total_chars"]
                    or len(value["text"]) != value["end_offset"] - value["offset"]
                    or type(value["truncated"]) is not bool
                    or value["next_cursor"] is not None and (not isinstance(value["next_cursor"], str)
                        or not 1 <= len(value["next_cursor"]) <= 4096)):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise _unverified() from None
    # No embedding, index, rerank or new top-k occurs during replay, provider
    # validation, public history, copying or derived use. Revalidate the exact
    # original observation in a clean SQL snapshot, never held ORM authority.
    async with clean_snapshot() as db:
        scope = await _access(db, main, args)
        # This frozen scope was just resolved in this observation's clean RR.
        # An all-project selection cannot authorize any individual project.
        local_scopes = {} if scope.include_all_projects else {scope.project_id: scope}
        if value["scope"] != memory._scope_view(scope):
            raise _unverified()
        if operation == "memory.search":
            if await memory.revalidate_items(db, scope, main.id, refs, local_scopes=local_scopes) != value["items"]:
                raise _unverified()
        else:
            now = await memory._read_locked(db, scope, main.id, source_ref=args.source_ref.model_dump(),
                source_id=args.source_id, max_chars=args.max_chars, cursor=args.cursor, check_expiry=fresh,
                local_scopes=local_scopes)
            selection = memory._selection(scope, main.id, args.source_ref.model_dump(), args.source_id,
                                          args.max_chars, now["projection_hash"])
            if (_position(now["next_cursor"], selection, fresh=fresh)
                    == _position(value["next_cursor"], selection, fresh=fresh)):
                now["next_cursor"] = value["next_cursor"]
            if now != value:
                raise _unverified()
        if snapshot.get("sources") != _proof(main, args, value, operation):
            raise _unverified()
