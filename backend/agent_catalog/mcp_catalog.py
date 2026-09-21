"""A bounded, credential-free view of the owner's sandbox MCP catalogue."""
from core.config import get_config


async def catalog(actor):
    enabled = get_config().team_tools_enabled
    empty = {"services": [], "available": False, "enabled": enabled}
    from sandbox.manager import sandbox_manager
    try:
        client = await sandbox_manager.get_client_any(user_id=actor.owner_user_id, workspace_id=actor.workspace_id)
        if client is None:
            return empty
        state = await client.get_catalogue_projection_state()
        if state.snapshot is None:
            return empty
        snapshot = state.snapshot
    except Exception:
        return empty
    services = {}
    for row in snapshot.get("mcp_servers", [])[:128]:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, str) and 0 < len(name) <= 128:
            services[name] = {"name": name, "status": str(row.get("status", "unknown"))[:40], "tools": [], "resources": []}
    for kind, field in (("tools", "name"), ("resources", "uri")):
        for row in snapshot.get("mcp_" + kind, [])[:4096]:
            if not isinstance(row, dict):
                continue
            server, name = row.get("server"), row.get(field)
            if not isinstance(server, str) or not 0 < len(server) <= 128 or not isinstance(name, str) or not 0 < len(name) <= 512:
                continue
            if server not in services and len(services) < 128:
                services[server] = {"name": server, "status": "connected", "tools": [], "resources": []}
            if server in services and len(services[server][kind]) < 128:
                services[server][kind].append(name)
    return {"services": sorted(services.values(), key=lambda item: item["name"]),
            "available": state.availability == "available", "enabled": enabled}
