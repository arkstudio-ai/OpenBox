"""Platform desktop calls keep the original tool's client and admission."""
import json

import httpx
import pytest

from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from platforms.desktop import service as desktop
from sandbox.client import user_scope_for
from tests.unit.test_assistant_sandbox_tool_scope import (  # noqa: F401
    assistant_database, resource, gateway, standalone, prepare, dispatch, rows,
)
from tool.tool import ToolResult, define_tool
from tests.unit.test_assistant_sandbox_tool_scope import EmptyArgs


@pytest.fixture
async def platform_route(standalone, monkeypatch):
    ctx, sent, respond = standalone
    async def transport(request):
        response = await respond(request)
        if request.url.path == '/desktop/lease/acquire':
            return httpx.Response(200, json={'token': 'fixture-token', 'wait_ms': 0})
        return response
    ctx.sandbox._transport = httpx.MockTransport(transport)
    ctx.sandbox._headers['X-OpenBox-User-Scope'] = user_scope_for(ctx.user_id)
    async with get_db_session() as db:
        row = await db.get(CloudDesktop, 'desktop-' + ctx.session_id)
        record = {name: getattr(row, name) for name in ('id', 'desktop_id', 'workspace_id')}
    route = {'host': 'fixture.invalid', 'port': 80, 'api_key': 'fixture-key'}
    monkeypatch.setattr('sandbox.channel.route_for_record', lambda _: (
        route['host'], route['port'], route['api_key']))
    return ctx, sent, record, route


def probe(record):
    async def body(_args, ctx):
        result = await desktop.run_command_on_desktop(
            record, 'printf fixture', parse=lambda output: {'output': output},
            summary='read-only fixture', operation='login-state', lease=True,
            session_id=ctx.session_id, tool_call_id=ctx.part_id,
        )
        return ToolResult(output=json.dumps(result))
    return define_tool('desktop_scope_probe', description='fixture',
        parameters=EmptyArgs, execute=body)


async def test_platform_desktop_probe_sends_with_original_client_and_effect(platform_route, monkeypatch):
    ctx, sent, record, _ = platform_route
    hooks, prepared = await prepare(ctx, probe(record), {}, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert not result.metadata.get('error'), result.output
    assert json.loads(result.output)['output'] == '123'
    row, = await rows(ctx)
    assert row.state == 'succeeded' and row.attempt_count == 1
    assert [request.url.path for request in sent] == [
        '/desktop/lease/acquire', '/execute', '/desktop/lease/release',
    ]
    assert sent[1].headers['X-OpenBox-Resource-Operation'] == row.id
    assert sent[1].headers['X-OpenBox-Resource-Owner-Id'] == ctx.workspace_id
    assert sent[1].headers['X-OpenBox-User-Scope'] == user_scope_for(ctx.user_id)


@pytest.mark.parametrize('changed', ['workspace', 'desktop', 'host', 'port', 'key', 'actor'])
async def test_platform_client_cannot_adopt_another_binding(platform_route, monkeypatch, changed):
    ctx, sent, record, route = platform_route
    if changed in ('workspace', 'desktop'):
        record[changed + '_id'] = 'unrelated-fixture'
    elif changed == 'actor':
        ctx.sandbox._headers['X-OpenBox-User-Scope'] = user_scope_for('another-user')
    else:
        key = 'api_key' if changed == 'key' else changed
        route[key] = 81 if changed == 'port' else 'unrelated-fixture'
    hooks, prepared = await prepare(ctx, probe(record), {}, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert result.metadata.get('error') and not sent and not await rows(ctx)


async def test_platform_reuse_keeps_closed_resource_blocked(platform_route, resource, monkeypatch):
    from tests.unit.test_assistant_resource_control import close
    ctx, sent, record, _ = platform_route
    hooks, prepared = await prepare(ctx, probe(record), {}, monkeypatch)
    await close(resource)
    result = await dispatch(hooks, prepared)
    assert result.metadata.get('error') and not sent and not await rows(ctx)


async def test_auth_center_outside_tool_scope_keeps_its_own_client(platform_route):
    ctx, _, record, _ = platform_route
    client = desktop._client_for(record)
    try:
        assert client is not ctx.sandbox
        assert client.desktop_id == record['desktop_id']
        assert client.base_url == ctx.sandbox.base_url
    finally:
        await client.aclose()
