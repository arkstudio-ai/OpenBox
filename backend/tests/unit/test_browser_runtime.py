"""Every automatic entry point must fail closed on an unusable browser."""
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox import browser_runtime as runtime


def test_payload_fits_cloud_assistant_and_carries_both_pinned_assets():
    script = runtime.runtime_install_script()
    assert all(len(base64.b64encode(s.encode())) < 16_384 for s in runtime.runtime_cloud_commands())
    assert '--install-deps' in script and '--register-service' in script
    assert set(runtime.runtime_files()) == {
        'repair_browser_runtime.py', 'dev-browser-package-lock.json', 'dev-browser-sources.json'
    }
    assert 'pkill' not in script and 'reboot' not in script
    assert "'systemctl','restart'" not in script


@pytest.mark.parametrize('output', ['', '{"ready":true}', '{"version":"old","ready":true}', '[]'])
def test_success_requires_current_version_and_explicit_readiness(output):
    with pytest.raises(runtime.BrowserRuntimeUnavailable):
        runtime.verified_result(output)


async def test_nonzero_exit_is_not_disguised_by_success_text():
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=1, stdout=json.dumps({'version': runtime.RUNTIME_VERSION, 'ready': True}))))
    with pytest.raises(runtime.BrowserRuntimeUnavailable):
        await runtime.ensure_browser_runtime(client)


async def test_successful_current_runtime_is_returned():
    data = {'version': runtime.RUNTIME_VERSION, 'ready': True, 'changed': False}
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout='diagnostic\n' + json.dumps(data))))
    assert await runtime.ensure_browser_runtime(client) == data
    client.execute.assert_awaited_once()
    assert client.execute.await_args.args[0].endswith(' --check')


async def test_old_version_check_cannot_skip_runtime_preparation():
    old = SimpleNamespace(exit_code=0, stdout='{"version":"old","ready":true}')
    current = SimpleNamespace(exit_code=0, stdout=json.dumps({'version':runtime.RUNTIME_VERSION,'ready':True}))
    client = SimpleNamespace(execute=AsyncMock(side_effect=[old, current]))
    assert (await runtime.ensure_browser_runtime(client))['version'] == runtime.RUNTIME_VERSION
    assert client.execute.await_count == 2
    assert '--register-service' in client.execute.await_args.args[0]


async def test_first_browser_use_checks_runtime_before_taking_gui_lease(monkeypatch):
    import core.config as config
    from sandbox import browser
    events = []
    monkeypatch.setattr(config, 'get_config', lambda: SimpleNamespace(sandbox_provider='wuying'))
    async def ready(client):
        events.append('runtime')
    @asynccontextmanager
    async def lease(**kwargs):
        events.append('lease')
        yield
    async def launch(*args):
        events.append('launch')
        return {'mode': 'local'}
    monkeypatch.setattr(runtime, 'ensure_browser_runtime', ready)
    monkeypatch.setattr(browser, '_ensure_browser_locked', launch)
    await browser.ensure_browser(SimpleNamespace(desktop_lease=lease), 'ecd-new', 'local')
    assert events == ['runtime', 'lease', 'launch']


async def test_runtime_failure_prevents_browser_launch(monkeypatch):
    import core.config as config
    from sandbox import browser
    monkeypatch.setattr(config, 'get_config', lambda: SimpleNamespace(sandbox_provider='wuying'))
    monkeypatch.setattr(runtime, 'ensure_browser_runtime', AsyncMock(
        side_effect=runtime.BrowserRuntimeUnavailable('offline')))
    launch = AsyncMock()
    monkeypatch.setattr(browser, '_ensure_browser_locked', launch)
    with pytest.raises(runtime.BrowserRuntimeUnavailable):
        await browser.ensure_browser(SimpleNamespace(), 'ecd-new', 'local')
    launch.assert_not_awaited()


async def test_channel_install_requires_runtime_before_credentials_or_services(monkeypatch):
    from sandbox import channel
    monkeypatch.setattr(channel, 'get_config', lambda: SimpleNamespace(wuying_channel='ssh'))
    prepare = AsyncMock(side_effect=runtime.BrowserRuntimeUnavailable('offline'))
    monkeypatch.setattr(channel, 'ensure_desktop_browser_runtime', prepare)
    command = AsyncMock()
    monkeypatch.setattr(channel, 'run_desktop_command', command)
    with pytest.raises(runtime.BrowserRuntimeUnavailable):
        await channel.WuyingChannel().install({'id': 'test', 'desktop_id': 'ecd-new'})
    prepare.assert_awaited_once_with('ecd-new')
    command.assert_not_awaited()


@pytest.mark.parametrize('display_ready', [True, False])
async def test_cold_browser_uses_real_display_or_isolated_headless_profile(monkeypatch, display_ready):
    from sandbox import browser
    ready = {'Browser': 'Chrome/151', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9333/test'}
    monkeypatch.setattr(browser, '_probe_chrome', AsyncMock(side_effect=[None, ready]))
    monkeypatch.setattr(browser, 'ensure_x_helper', AsyncMock())
    monkeypatch.setattr(browser.asyncio, 'sleep', AsyncMock())
    launch = AsyncMock()
    monkeypatch.setattr(browser, '_fire_and_forget', launch)
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=0 if display_ready else 3)))
    assert await browser.ensure_chrome(client, 'ecd-cold') == ready
    script = launch.await_args.args[1]
    assert ('--headless=new' in script) is (not display_ready)
    if not display_ready:
        assert '--no-create-home' in script  # never copy the image's /etc/skel state
        assert '/var/lib/openbox/browser' in script
        assert '--no-sandbox' not in script
        assert 'pkill' not in script
        assert 'recovering unresponsive OpenBox Chrome pid(s)' in script
        assert 'Chrome launched but its renderer did not become healthy' in script
        assert '9>&-' in script


async def test_existing_browser_is_never_restarted_to_change_presentation(monkeypatch):
    from sandbox import browser
    ready = {'Browser': 'Chrome/151', 'User-Agent': 'HeadlessChrome/151', 'webSocketDebuggerUrl': 'ws://local/test'}
    monkeypatch.setattr(browser, '_probe_chrome', AsyncMock(return_value=ready))
    client = SimpleNamespace(execute=AsyncMock())
    assert await browser.ensure_chrome(client, 'ecd-existing') == ready
    assert browser.is_headless(ready)
    client.execute.assert_not_awaited()


async def test_chrome_probe_requires_renderer_cdp_execution(monkeypatch):
    from sandbox import browser
    version = {'Browser': 'Chrome/151', 'webSocketDebuggerUrl': 'ws://local/test'}
    monkeypatch.setattr(browser, '_curl_json', AsyncMock(return_value=version))
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout=json.dumps(version))))
    assert await browser._probe_chrome(client) == version
    command = client.execute.await_args.args[0]
    assert 'python3' in command and 'base64 -d' in command


async def test_http_only_chrome_is_not_considered_healthy(monkeypatch):
    from sandbox import browser
    monkeypatch.setattr(browser, '_curl_json', AsyncMock(return_value={'Browser': 'Chrome/151'}))
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=1, stdout='')))
    assert await browser._probe_chrome(client) is None


@pytest.mark.parametrize('working_browser', [True, False])
async def test_channel_readiness_checks_real_browser_without_requiring_web_sdk_login(monkeypatch, working_browser):
    import httpx
    from sandbox import browser, channel
    @asynccontextmanager
    async def lease(**kwargs):
        yield
    sandbox = SimpleNamespace(desktop_lease=lease, execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout='test-host\nOPENBOX_NO_DISPLAY\n', stderr='')))
    monkeypatch.setattr(channel, 'SandboxClient', lambda **kwargs: sandbox)
    monkeypatch.setattr(channel, 'route_for_record', lambda record: ('localhost',8000,'test'))
    class HTTP:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url):
            return httpx.Response(200, request=httpx.Request('GET',url))
    monkeypatch.setattr(channel.httpx, 'AsyncClient', lambda **kwargs: HTTP())
    monkeypatch.setattr(channel, 'ensure_browser_runtime', AsyncMock())
    result = {'chrome':{'Browser':'Chrome/151','User-Agent':'HeadlessChrome/151','webSocketDebuggerUrl':'ws://local/test'},'relay':{'chromeAvailable':working_browser}}
    monkeypatch.setattr(browser, 'ensure_browser', AsyncMock(return_value=result))
    update = AsyncMock()
    monkeypatch.setattr(channel.cloud_desktop_repo, 'update', update)
    if working_browser:
        checked = await channel.WuyingChannel().verify({'id':'test','desktop_id':'ecd-test'})
        assert checked['display_ready'] is False
        assert checked['browser_presentation'] == 'headless'
        assert update.await_args.kwargs['tunnel_state'] == 'up'
    else:
        with pytest.raises(runtime.BrowserRuntimeUnavailable):
            await channel.WuyingChannel().verify({'id':'test','desktop_id':'ecd-test'})
        assert all(c.kwargs.get('tunnel_state') != 'up' for c in update.await_args_list)
