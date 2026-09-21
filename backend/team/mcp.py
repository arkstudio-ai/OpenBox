"""Dynamic MCP delegation: frozen references intersect the current run grant.

MCP references name an exact service and glob patterns for tool names. Resource
patterns use ``resource:<uri>``; ``*`` grants both tools and resources. Static
meta-tool IDs keep the legacy frozen authority intact while the underlying
catalogue can change on every model step.
"""
from __future__ import annotations

from contextvars import ContextVar
from fnmatch import fnmatchcase

from core.config import get_config
from team.errors import TeamError
from team import runtime_binding
from tool.tool import ToolResult

META_TOOLS = frozenset({"mcp_find_tool", "mcp_call_tool", "mcp_read_resource"})
_approved_identity: ContextVar[tuple | None] = ContextVar("team_mcp_identity", default=None)


def matches(refs: list[dict], server: str, name: str) -> bool:
    return any(ref.get("server") == server and any(
        fnmatchcase(name, pattern) for pattern in ref.get("tools", ["*"])
    ) for ref in refs)


def validate_refs(spec, config, grant=None) -> None:
    if not spec.mcp_refs:
        return
    if not getattr(config, "team_tools_enabled", False):
        raise TeamError("TOOL_NOT_TEAM_READY", "MCP delegation is disabled in this deployment.", status=422)
    if grant is not None:
        permitted = {ref["server"] for ref in grant.get("mcp_refs", []) if ref.get("tools", ["*"])}
        missing = {ref.server for ref in spec.mcp_refs} - permitted
        if missing:
            raise TeamError("PERMISSION_REQUIRES_USER", "These MCP services were not approved for this run.",
                current={"mcp_servers": sorted(missing)}, status=422)


async def _scope():
    binding = runtime_binding.current_binding()
    if binding is None:
        return None
    grant = await runtime_binding.current_grant()
    categories = set(binding.spec.get("execution_policy", {}).get("tool_categories", []))
    if binding.role == "coordinator" or (categories and "MCP" not in categories) or not getattr(get_config(), "team_tools_enabled", False):
        return binding, [], []
    requested = binding.spec.get("mcp_refs", [])
    approved = requested if binding.role == "trial" else grant.get("mcp_refs", [])
    return binding, requested, approved


def _allowed(scope, server: str, name: str) -> bool:
    return scope is None or (matches(scope[1], server, name) and matches(scope[2], server, name))


async def meta_tools_allowed() -> bool:
    scope = await _scope()
    if scope is None:
        return True
    return bool({ref["server"] for ref in scope[1] if ref.get("tools", ["*"])} &
        {ref["server"] for ref in scope[2] if ref.get("tools", ["*"])})


def _same_context(binding, ctx):
    return (ctx.user_id == binding.owner_user_id and ctx.workspace_id == binding.workspace_id
        and ctx.project_id == binding.project_id and ctx.session_id == binding.member_id)


async def filter_bindings(bindings, *, ctx=None):
    scope = await _scope()
    if scope is None:
        return bindings
    if ctx is not None:
        await ctx.assert_run_current()
        if not _same_context(scope[0], ctx):
            return []
    return [item for item in bindings if _allowed(scope, item.server, item.name)]


async def filter_resources(resources):
    scope = await _scope()
    if scope is None:
        return resources
    return [item for item in resources if isinstance(item, dict) and
        _allowed(scope, item.get("server", ""), "resource:" + str(item.get("uri", "")))]


async def report_unavailable(available_servers: set[str]) -> None:
    """One durable notice per missing requested service, without failing a run."""
    scope = await _scope()
    if scope is None or scope[0].run_id is None or scope[0].role != "member":
        return
    binding, requested, approved = scope
    missing = ({ref["server"] for ref in requested} & {ref["server"] for ref in approved}) - available_servers
    from team.journal import Actor, command, digest
    for server in sorted(missing):
        key = "mcp-unavailable:" + digest([binding.member_id, server])
        async def notice(writer, server=server, key=key):
            if writer.run.state not in {"running", "waiting"}:
                return {"reported": False}
            writer.append("team.notice", "notice", {"id": key, "code": "MCP_SERVICE_UNAVAILABLE",
                "member_id": binding.member_id, "server": server,
                "message": "The requested MCP service is unavailable; the member continues with its remaining tools."})
            return {"reported": True}
        try:
            await command(binding.run_id, Actor(binding.owner_user_id, binding.workspace_id, "server"),
                key, {"server": server}, notice)
        except TeamError:
            # A concurrently closed run does not need a new availability notice.
            continue


async def guard(ctx, server: str, name: str) -> ToolResult | None:
    scope = await _scope()
    if scope is None:
        return None
    binding = scope[0]
    await ctx.assert_run_current()
    if _same_context(binding, ctx) and _allowed(scope, server, name):
        return None
    return ToolResult(title="MCP delegation denied", output="This MCP target is outside the member's current approved service and tool patterns.",
        metadata={"blocked": True, "code": "TOOL_NOT_ALLOWED"})


async def authorize(ctx, server, name, permission, callback):
    """Preserve normal permission planes after checking the raw MCP identity."""
    blocked = await guard(ctx, server, name)
    if blocked is not None:
        return blocked
    binding = runtime_binding.current_binding()
    token = _approved_identity.set((binding, server, name, permission))
    try:
        blocked = await callback()
        return blocked if blocked is not None else await guard(ctx, server, name)
    finally:
        _approved_identity.reset(token)


async def preapproved(permission, patterns, input_data) -> bool | None:
    """Only resolve asks; permission.ask checks every deny before reaching here."""
    scope = await _scope()
    if scope is None or scope[0].role != "member":
        return None
    if permission in {"mcp_find_tool", "mcp_call_tool"}:
        return await meta_tools_allowed()
    if permission == "mcp_read_resource":
        from tool.mcp_tool import _canonical_resource_id
        server, uri = input_data.get("server"), input_data.get("uri")
        if not isinstance(server, str) or not isinstance(uri, str):
            return False
        return (all(pattern == _canonical_resource_id(server, uri) for pattern in patterns)
            and _allowed(scope, server, "resource:" + uri))
    if permission.startswith("mcp:v2:"):
        identity = _approved_identity.get()
        return bool(identity and identity[0] == scope[0] and identity[3] == permission
            and _allowed(scope, identity[1], identity[2]))
    return None
