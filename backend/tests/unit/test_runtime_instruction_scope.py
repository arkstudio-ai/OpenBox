from types import SimpleNamespace

from session.instruction import instruction_system_with_config


async def test_remote_runtime_excludes_host_discovery_but_keeps_explicit_policy(tmp_path, monkeypatch):
    checkout = tmp_path / 'server'
    checkout.mkdir()
    (checkout / 'AGENTS.md').write_text('host repository maintenance only')
    global_dir = tmp_path / 'config' / 'openbox'
    global_dir.mkdir(parents=True)
    (global_dir / 'AGENTS.md').write_text('operator personal instructions')
    policy = tmp_path / 'policy.md'
    policy.write_text('explicit deployment authorization boundary')
    monkeypatch.chdir(checkout)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    config = SimpleNamespace(instructions=[str(policy)])
    remote = await instruction_system_with_config(config, discover_local=False)
    assert len(remote) == 1 and 'explicit deployment authorization boundary' in remote[0]
    local = '\n'.join(await instruction_system_with_config(config))
    assert 'host repository maintenance only' in local
    assert 'operator personal instructions' in local
