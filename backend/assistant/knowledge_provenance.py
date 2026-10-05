"""Knowledge titles and body pages use the versioned business observation contract.

Every call below opens a clean read-only transaction. In particular a provider
write/checkpoint transaction's held MemorySource/Document objects or authority
caches never certify later knowledge reads. No result is dispatch authority;
business_context and the existing exact request/response manifest own that.
"""
from copy import deepcopy
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant import knowledge
from assistant.commands import command_digest
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot
from db.base import get_db_session

OPERATION = "knowledge.directory"
READ_OPERATION = "knowledge.read"
PROOF_VERSION = 1


class DirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly search the actor's owned live projects as well as personal background. Omit project_id.")
    query: str = Field(default="", max_length=200, description="Literal title search only, not document contents.")
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_selection(self):
        if self.project_id is not None and self.include_all_projects:
            raise ValueError("Select one project or explicitly select all owned projects")
        return self


class KnowledgeReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    kind: Literal["wiki"]
    id: str = Field(min_length=1, max_length=64)
    assistant_session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    workspace_id: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(min_length=1, max_length=64)
    visibility: Literal["PERSONAL"]
    revision: int = Field(ge=1, le=0x7FFFFFFF)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acl_epoch: int = Field(ge=1, le=0x7FFFFFFF)

    @model_validator(mode="before")
    @classmethod
    def exact_reference(cls, value):
        if not knowledge._valid_reference(value):
            raise ValueError("Use the exact source_ref returned by knowledge.directory")
        return value


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: KnowledgeReference = Field(description="Complete source_ref from knowledge.directory, unchanged.")
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_all_projects: bool = Field(default=False, strict=True,
        description="Explicitly allow the actor's owned live projects as well as personal background. Omit project_id.")
    max_chars: int = Field(default=8000, ge=1, le=knowledge.MAX_READ_CHARS, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_selection(self):
        if self.project_id is not None and self.include_all_projects:
            raise ValueError("Select one project or explicitly select all owned projects")
        return self


def _unverified():
    return AssistantError(410, "ASSISTANT_KNOWLEDGE_UNVERIFIED", "The original knowledge observation is unavailable")


def _selection(scope, main_id, args):
    # Adding a project does not rewrite a past inventory observation. Existing
    # items must remain authorized, and a selected project/ACL is always live.
    return {"version": PROOF_VERSION, "user_id": scope.user_id, "workspace_id": scope.workspace_id,
        "main_id": main_id, "project_id": scope.project_id, "include_all_projects": scope.include_all_projects,
        "query": args.query.strip().casefold(), "limit": args.limit, "acl_epoch": scope.acl_epoch}


def _cursor_position(token, cursor_scope, *, fresh):
    if token is None:
        return None
    value = knowledge._cursor(token, selection=cursor_scope, check_expiry=fresh)
    return {key: value[key] for key in ("version", "after")}


def _proof(scope, main_id, args, value, cursor_scope):
    return {"resources": [{"kind": OPERATION, "selection": _selection(scope, main_id, args),
        "cursor_scope": cursor_scope, "start": _cursor_position(args.cursor, cursor_scope, fresh=False),
        "next": _cursor_position(value["next_cursor"], cursor_scope, fresh=False),
        "entries": [item["source_ref"] for item in value["items"]]}], "tasks": []}


async def _access(db, main, args):
    return await knowledge._access(db, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id,
        project_id=args.project_id, include_all_projects=args.include_all_projects)


async def capture(main, arguments, *, operation=OPERATION):
    if operation == READ_OPERATION:
        return await _capture_read(main, arguments)
    args = DirectoryArgs.model_validate(arguments)
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, main, args)
        query = args.query.strip().casefold()
        value = await knowledge._directory_locked(db, scope, main.id, query=query, limit=args.limit, cursor=args.cursor)
        cursor_scope = knowledge._selection(scope, main.id, query, args.limit)
        proof = _proof(scope, main.id, args, value, cursor_scope)
    return value, {"version": 2, "operation": OPERATION, "arguments": deepcopy(arguments),
        "digest": command_digest(value), "projection": value, "sources": proof}


async def validate(main, snapshot, *, fresh=False):
    if snapshot.get("operation") == READ_OPERATION:
        await _validate_read(main, snapshot, fresh=fresh)
        return
    try:
        args = DirectoryArgs.model_validate(snapshot["arguments"])
        value, sources = snapshot["projection"], snapshot["sources"]
        if (not isinstance(value, dict) or set(value) != {"items", "next_cursor", "untrusted_data", "scope"}
                or value["untrusted_data"] is not True or not isinstance(value["items"], list)
                or len(value["items"]) > args.limit or any(not isinstance(item, dict) for item in value["items"])
                or value["next_cursor"] is not None and (not isinstance(value["next_cursor"], str)
                    or not 1 <= len(value["next_cursor"]) <= 4096)
                or not isinstance(sources, dict) or set(sources) != {"resources", "tasks"}
                or sources["tasks"] != [] or not isinstance(sources["resources"], list) or len(sources["resources"]) != 1):
            raise _unverified()
        proof = sources["resources"][0]
        cursor_scope = proof["cursor_scope"]
        if not isinstance(cursor_scope, str) or not re.fullmatch(r"[0-9a-f]{64}", cursor_scope):
            raise _unverified()
        refs = [item["source_ref"] for item in value["items"]]
    except (KeyError, TypeError, ValueError):
        raise _unverified() from None
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 2 * knowledge.MAX_RESPONSE_BYTES:
        raise _unverified()
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, main, args)
        # Seed only the exact scope freshly resolved in this observation.
        # Background and other projects still resolve their own authority.
        local_scopes = {} if scope.include_all_projects else {scope.project_id: scope}
        current = await knowledge._revalidate_refs_locked(db, scope, main.id, refs,
            local_scopes=dict(local_scopes))
        if (current != value["items"] or value["scope"] != knowledge._scope_view(scope)
                or sources != _proof(scope, main.id, args, value, cursor_scope)):
            raise _unverified()
        if not fresh:
            return
        # A provider receives the exact captured page, including an empty
        # result. New inventory and pagination drift require a refreshed read.
        query = args.query.strip().casefold()
        live_cursor_scope = knowledge._selection(scope, main.id, query, args.limit)
        now = await knowledge._directory_locked(db, scope, main.id, query=query, limit=args.limit,
            cursor=args.cursor, local_scopes=dict(local_scopes))
        if (live_cursor_scope == cursor_scope
                and _cursor_position(now["next_cursor"], live_cursor_scope, fresh=True)
                    == _cursor_position(value["next_cursor"], cursor_scope, fresh=True)):
            # Nonces and issuance times are transport properties. Keep the
            # original, still-valid token whose bytes the request consumed.
            now["next_cursor"] = value["next_cursor"]
        if now != value or live_cursor_scope != cursor_scope:
            raise AssistantError(409, "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED",
                "Knowledge directory changed before dispatch; refresh its observation")


def _read_cursor_position(token, cursor_scope, *, fresh):
    if token is None:
        return None
    state = knowledge._read_cursor(token, selection=cursor_scope, check_expiry=fresh)
    return {key: state[key] for key in ("version", "offset")}


def _read_proof(scope, main_id, args, value):
    cursor_scope = knowledge._read_selection(scope, main_id, args.source_ref.model_dump(),
                                            args.max_chars, value["projection_hash"])
    return {"resources": [{"kind": READ_OPERATION, "cursor_scope": cursor_scope,
        "start": _read_cursor_position(args.cursor, cursor_scope, fresh=False),
        "next": _read_cursor_position(value["next_cursor"], cursor_scope, fresh=False),
        "source_ref": args.source_ref.model_dump(),
        **{key: value[key] for key in ("projection_version", "projection_hash", "chunk_hash", "offset",
                                       "end_offset", "total_chars")}}], "tasks": []}


async def _capture_read(main, arguments):
    args = ReadArgs.model_validate(arguments)
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, main, args)
        value = await knowledge._read_locked(db, scope, main.id, source_ref=args.source_ref.model_dump(),
            max_chars=args.max_chars, cursor=args.cursor)
        proof = _read_proof(scope, main.id, args, value)
    return value, {"version": 2, "operation": READ_OPERATION, "arguments": deepcopy(arguments),
        "digest": command_digest(value), "projection": value, "sources": proof}


async def _validate_read(main, snapshot, *, fresh):
    try:
        args = ReadArgs.model_validate(snapshot["arguments"])
        value = snapshot["projection"]
        if (not isinstance(value, dict) or set(value) != {"item", "text", "offset", "end_offset", "total_chars",
                "projection_version", "projection_hash", "chunk_hash", "next_cursor", "truncated", "untrusted_data", "scope"}
                or value["untrusted_data"] is not True or type(value["truncated"]) is not bool
                or not isinstance(value["text"], str) or not 1 <= len(value["text"]) <= args.max_chars
                or any(type(value[key]) is not int for key in ("offset", "end_offset", "total_chars"))
                or not 0 <= value["offset"] < value["end_offset"] <= value["total_chars"]
                or value["end_offset"] - value["offset"] != len(value["text"])
                or value["next_cursor"] is not None and (not isinstance(value["next_cursor"], str)
                    or not 1 <= len(value["next_cursor"]) <= 4096)
                or len(json.dumps(snapshot, ensure_ascii=False).encode()) > 2 * knowledge.MAX_RESPONSE_BYTES):
            raise _unverified()
    except (KeyError, TypeError, ValueError):
        raise _unverified() from None
    # Neither a held ORM object nor a previous validation can certify body
    # bytes. Replay and public history use the same current source checks;
    # only transport expiry is ignored for a historical exact observation.
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, main, args)
        local_scopes = {} if scope.include_all_projects else {scope.project_id: scope}
        now = await knowledge._read_locked(db, scope, main.id, source_ref=args.source_ref.model_dump(),
            max_chars=args.max_chars, cursor=args.cursor, check_expiry=fresh, local_scopes=local_scopes)
        cursor_scope = knowledge._read_selection(scope, main.id, args.source_ref.model_dump(),
                                                args.max_chars, now["projection_hash"])
        if (_read_cursor_position(now["next_cursor"], cursor_scope, fresh=fresh)
                == _read_cursor_position(value["next_cursor"], cursor_scope, fresh=fresh)):
            now["next_cursor"] = value["next_cursor"]
        if now != value or snapshot.get("sources") != _read_proof(scope, main.id, args, value):
            raise _unverified()
