"""Knowledge titles use the existing versioned business observation contract.

Every call below opens a clean read-only transaction. In particular a provider
write/checkpoint transaction's held MemorySource/Document objects or authority
caches never certify later knowledge reads. No result is dispatch authority;
business_context and the existing exact request/response manifest own that.
"""
from copy import deepcopy
import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant import knowledge
from assistant.commands import command_digest
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot
from db.base import get_db_session

OPERATION = "knowledge.directory"
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


def _unverified():
    return AssistantError(410, "ASSISTANT_KNOWLEDGE_UNVERIFIED", "The original knowledge directory observation is unavailable")


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


async def capture(main, arguments):
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
        current = await knowledge._revalidate_refs_locked(db, scope, main.id, refs)
        if (current != value["items"] or value["scope"] != knowledge._scope_view(scope)
                or sources != _proof(scope, main.id, args, value, cursor_scope)):
            raise _unverified()
        if not fresh:
            return
        # A provider receives the exact captured page, including an empty
        # result. New inventory and pagination drift require a refreshed read.
        query = args.query.strip().casefold()
        live_cursor_scope = knowledge._selection(scope, main.id, query, args.limit)
        now = await knowledge._directory_locked(db, scope, main.id, query=query, limit=args.limit, cursor=args.cursor)
        if (live_cursor_scope == cursor_scope
                and _cursor_position(now["next_cursor"], live_cursor_scope, fresh=True)
                    == _cursor_position(value["next_cursor"], cursor_scope, fresh=True)):
            # Nonces and issuance times are transport properties. Keep the
            # original, still-valid token whose bytes the request consumed.
            now["next_cursor"] = value["next_cursor"]
        if now != value or live_cursor_scope != cursor_scope:
            raise AssistantError(409, "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED",
                "Knowledge directory changed before dispatch; refresh its observation")
