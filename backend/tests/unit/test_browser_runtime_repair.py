"""Runtime repair must not relax the inherited profile gate for other users."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[2] / "sandbox" / "browser_runtime_repair.py"
spec = importlib.util.spec_from_file_location("repair_browser_runtime", MODULE)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def test_stock_chrome_is_unchanged():
    source = '#!/bin/bash\nexec /opt/google/chrome/chrome "$@"\n'
    assert repair.patched_chrome_gate(source) == source


def test_gate_patch_is_narrow_and_idempotent():
    source = '# BOSSIP_CHROME_GATE\n' + repair.GATE_ANCHOR + '\nexec old-gate\n'
    updated = repair.patched_chrome_gate(source)
    assert repair.patched_chrome_gate(updated) == updated
    assert source.splitlines()[-1] in updated
    assert '${HOME}/.config/obx-chrome' in updated
    assert 'stat -c %u "$HOME"' in updated
    assert '[[ ! -L "${bossip_requested}" ]]' in updated
    assert 'stat -c %u "${bossip_requested}"' in updated
    assert 'BOSSIP_CHROME_GATE=off' not in updated


def test_unknown_gate_fails_closed():
    with pytest.raises(RuntimeError, match="Unrecognized"):
        repair.patched_chrome_gate('# BOSSIP_CHROME_GATE\nexec unknown\n')


def test_existing_commands_are_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(repair.shutil, "which", lambda name: '/existing/' + name)
    assert repair.ensure_node_commands(tmp_path, tmp_path / 'missing') == []


def test_missing_runtime_does_not_create_partial_links(tmp_path, monkeypatch):
    monkeypatch.setattr(repair.shutil, "which", lambda name: None)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'node').write_text('#!/bin/sh\nexit 0\n')
    (runtime / 'node').chmod(0o755)
    with pytest.raises(RuntimeError, match='Missing executable'):
        repair.ensure_node_commands(tmp_path, runtime)
    assert not (tmp_path / 'node').exists()


def test_broken_existing_symlink_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(repair.shutil, "which", lambda name: None)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    for name in ('node', 'npm', 'npx'):
        (runtime / name).write_text('#!/bin/sh\nexit 0\n')
        (runtime / name).chmod(0o755)
    (tmp_path / 'node').symlink_to(tmp_path / 'unavailable')
    with pytest.raises(RuntimeError, match='Refusing to replace'):
        repair.ensure_node_commands(tmp_path, runtime)
    assert (tmp_path / 'node').is_symlink()


def test_dependency_install_uses_isolated_configs_and_validates_before_promotion(tmp_path, monkeypatch):
    skill = tmp_path / 'dev-browser'
    skill.mkdir()
    (skill / 'package.json').write_text('{"dependencies": {}}')
    (skill / 'package-lock.json').write_text('{"packages":{"":{"dependencies":{}}}}')
    backup = tmp_path / 'backup'
    backup.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert not (skill / 'node_modules').exists()
        if command[0] == 'npm':
            assert '--ignore-scripts' in command
            assert '--userconfig=/dev/null' in command
            assert '--globalconfig=/dev/null' not in command
            assert set(kwargs['env']) == {'PATH', 'HOME', 'CI', 'npm_config_cache', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'}
            staging = kwargs['cwd']
            (staging / 'node_modules').mkdir()
            (staging / 'package-lock.json').write_text('{}')

    monkeypatch.setattr(repair.subprocess, 'run', run)
    repair.install_dependencies(skill, backup, 'https://registry.npmjs.org')
    assert len(calls) == 3
    assert (skill / 'node_modules').is_dir()
    assert (skill / 'package-lock.json').is_file()


def test_failed_dependency_install_preserves_existing_modules(tmp_path, monkeypatch):
    skill = tmp_path / 'dev-browser'
    skill.mkdir()
    (skill / 'package.json').write_text('{}')
    (skill / 'package-lock.json').write_text('{"packages":{"":{}}}')
    modules = skill / 'node_modules'
    modules.mkdir()
    (modules / 'keep').write_text('old runtime')
    backup = tmp_path / 'backup'
    backup.mkdir()

    def fail(*args, **kwargs):
        raise RuntimeError('offline')

    monkeypatch.setattr(repair.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='offline'):
        repair.install_dependencies(skill, backup, 'https://registry.npmjs.org')
    assert (modules / 'keep').read_text() == 'old runtime'


def test_installer_and_bootstrap_have_identical_pinned_dependencies():
    root = Path(__file__).resolve().parents[3]
    assert (root / 'container/dev-browser/package-lock.json').read_bytes() == (
        root / 'backend/sandbox/assets/dev-browser-package-lock.json').read_bytes()
    package = json.loads((root / 'container/dev-browser/package.json').read_text())
    lock = json.loads((root / 'container/dev-browser/package-lock.json').read_text())
    assert package['dependencies'] == lock['packages']['']['dependencies']


def test_missing_lock_refuses_unpinned_network_install(tmp_path, monkeypatch):
    skill, backup = tmp_path / 'skill', tmp_path / 'backup'
    skill.mkdir()
    backup.mkdir()
    (skill / 'package.json').write_text('{}')
    monkeypatch.setattr(repair.subprocess, 'run', lambda *a, **k: pytest.fail('must not run npm'))
    with pytest.raises(RuntimeError, match='Pinned'):
        repair.install_dependencies(skill, backup, 'https://registry.npmjs.org')


def test_matching_versions_still_require_typescript_to_execute(tmp_path, monkeypatch):
    (tmp_path / 'package.json').write_text('{"dependencies": {}}')
    lock = tmp_path / 'package-lock.json'
    lock.write_text('{"packages":{"":{"dependencies":{}}}}')
    def broken(*args, **kwargs):
        raise repair.subprocess.CalledProcessError(1, ['node'])
    monkeypatch.setattr(repair.subprocess, 'run', broken)
    assert repair.dependency_problems(tmp_path, lock)


def test_healthy_runtime_is_a_noop_without_npm_or_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(repair, 'runtime_problems', lambda *a: [])
    monkeypatch.setattr(repair, 'install_dependencies', lambda *a: pytest.fail('must not reinstall'))
    monkeypatch.setattr(repair.tempfile, 'mkdtemp', lambda **k: pytest.fail('must not create backup'))
    repair.repair_runtime(SimpleNamespace(lock_file=tmp_path / 'lock', register_service=False))
    assert json.loads(capsys.readouterr().out)['changed'] is False


def test_repair_lock_rejects_concurrent_installer_and_releases(tmp_path):
    path = tmp_path / 'runtime.lock'
    with repair.runtime_lock(path):
        with pytest.raises(RuntimeError, match='Another browser runtime repair'):
            with repair.runtime_lock(path, timeout=0):
                pytest.fail('must not enter second installer')
    with repair.runtime_lock(path, timeout=0):
        pass


def test_boot_gate_is_noninteractive_and_does_not_start_chrome():
    assert 'Before=openbox-action-server.service' in repair.SERVICE
    assert '--install-deps' in repair.SERVICE
    assert '--register-service' not in repair.SERVICE  # no daemon-reload loop
    assert 'pkill' not in repair.SERVICE
    assert 'npm install' not in repair.SERVICE


@pytest.mark.parametrize('value,expected', [('512', False), ('2048', True), ('4096', True),
                                           ('infinity', True), ('max', True), ('bad', False)])
def test_task_budget_includes_threads_and_preserves_higher_limits(value, expected):
    assert repair.sufficient_task_limit(value) is expected


def test_task_budget_check_requires_persistent_and_live_limits(tmp_path):
    root, cgroup = tmp_path / 'systemd', tmp_path / 'cgroup'
    config = root / (repair.ACTION_SERVICE + '.d/' + repair.TASK_BUDGET_DROPIN)
    assert repair.task_budget_problems(root, cgroup)
    config.parent.mkdir(parents=True)
    config.write_text('[Service]\nTasksMax=2048\n')
    assert repair.task_budget_problems(root, cgroup) == []  # not started yet
    cgroup.mkdir()
    (cgroup / 'pids.max').write_text('512')
    assert repair.task_budget_problems(root, cgroup)
    (cgroup / 'pids.max').write_text('2048')
    assert repair.task_budget_problems(root, cgroup) == []


@pytest.mark.parametrize('current,pid,expected,live_update', [
    ('512', '123', '2048', True), ('512', '0', '2048', False),
    ('4096', '123', '4096', False), ('infinity', '123', 'infinity', False),
])
def test_service_task_budget_is_persistent_without_restarting(tmp_path, monkeypatch, current, pid, expected, live_update):
    root, cgroup = tmp_path / 'systemd', tmp_path / 'cgroup'
    cgroup.mkdir()
    (cgroup / 'pids.max').write_text('max' if current == 'infinity' else current)
    if pid == '0':
        (cgroup / 'pids.max').unlink()
    monkeypatch.setattr(repair, 'SYSTEMD_ROOT', root)
    monkeypatch.setattr(repair, 'SYSTEMD_CONTROL_ROOT', tmp_path / 'systemd-control')
    monkeypatch.setattr(repair, 'ACTION_CGROUP', cgroup)
    calls = []
    state = {'current': current}
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'show':
            return SimpleNamespace(stdout=f"LoadState=loaded\nMainPID={pid}\nTasksMax={state['current']}\n")
        if command[1] == 'daemon-reload':
            state['current'] = expected
        if command[1] == 'set-property':
            assert '--runtime' not in command  # legacy /etc pin outranks /run
            (cgroup / 'pids.max').write_text(expected)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(repair.subprocess, 'run', run)
    assert repair.register_boot_service() is True
    config = root / (repair.ACTION_SERVICE + '.d/' + repair.TASK_BUDGET_DROPIN)
    assert f'TasksMax={expected}' in config.read_text()
    assert any(c[1] == 'set-property' for c in calls) is live_update
    assert not any('restart' in c or 'stop' in c for c in calls)
    assert not any('MemoryMax' in ' '.join(c) for c in calls)
    assert repair.task_budget_problems(root, cgroup) == []
    calls.clear()
    assert repair.register_boot_service() is False
    assert not any(c[1] in ('daemon-reload', 'set-property') for c in calls)


def test_bootstrap_task_limit_matches_runtime_minimum():
    source = (Path(__file__).resolve().parents[2] / 'scripts/wuying_bootstrap.py').read_text()
    assert source.count(f'TasksMax={repair.ACTION_TASKS_MIN}') == 2
    assert 'TasksMax=512' not in source


def test_legacy_media_dropin_cannot_restore_512_after_reload(tmp_path, monkeypatch):
    root, control, cgroup = (tmp_path / p for p in ['systemd', 'control', 'cgroup'])
    dropins = root / (repair.ACTION_SERVICE + '.d')
    dropins.mkdir(parents=True)
    media = dropins / 'media.conf'
    media.write_text('[Service]\nMemoryMax=6G\nTasksMax=512\n')
    pinned = control / (repair.ACTION_SERVICE + '.d/50-TasksMax.conf')
    pinned.parent.mkdir(parents=True)
    pinned.write_text('[Service]\nTasksMax=512\n')
    cgroup.mkdir()
    live = cgroup / 'pids.max'
    live.write_text('512')
    monkeypatch.setattr(repair, 'SYSTEMD_ROOT', root)
    monkeypatch.setattr(repair, 'SYSTEMD_CONTROL_ROOT', control)
    monkeypatch.setattr(repair, 'ACTION_CGROUP', cgroup)
    monkeypatch.setattr(repair, 'BACKUP_ROOT', tmp_path / 'backups')
    def reload():
        files = [pinned, *dropins.glob('*.conf')]
        for path in sorted(files, key=lambda p: p.name):
            for line in path.read_text().splitlines():
                if line.startswith('TasksMax='):
                    live.write_text(line.split('=', 1)[1])
    def run(command, **kwargs):
        if command[1] == 'show':
            return SimpleNamespace(stdout=f'LoadState=loaded\nMainPID=123\nTasksMax={live.read_text()}\n')
        if command[1] == 'set-property':
            assert '--runtime' not in command
            pinned.write_text('[Service]\n' + command[-1] + '\n')
            live.write_text(command[-1].split('=', 1)[1])
        if command[1] in ('daemon-reload', 'enable'):
            reload()
        assert command[1] not in ('restart', 'stop')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(repair.subprocess, 'run', run)
    repair.register_boot_service()
    assert live.read_text() == '2048'
    assert repair.task_budget_problems(root, cgroup) == []
    assert media.read_text() == '[Service]\nMemoryMax=6G\nTasksMax=512\n'
    saved = list((tmp_path / 'backups').glob('*/50-TasksMax.conf'))
    assert len(saved) == 1 and 'TasksMax=512' in saved[0].read_text()
    assert repair.register_boot_service() is False
    reload()
    assert live.read_text() == '2048'


def test_source_bundle_is_path_safe_and_promoted_atomically(tmp_path):
    skill = tmp_path / 'skill'
    skill.mkdir()
    (skill / 'src').mkdir()
    (skill / 'src' / 'relay.ts').write_text('old')
    bundle = tmp_path / 'sources.json'
    bundle.write_text(json.dumps({'src/relay.ts': 'new', 'src/client.ts': 'client'}))
    backup = tmp_path / 'backup'
    backup.mkdir()

    assert repair.install_sources(skill, bundle, backup) is True
    assert (skill / 'src' / 'relay.ts').read_text() == 'new'
    assert (skill / 'src' / 'client.ts').read_text() == 'client'
    assert (backup / 'sources' / 'src' / 'relay.ts').read_text() == 'old'
    assert repair.source_problems(skill, bundle) == []


def test_source_bundle_rejects_parent_traversal(tmp_path):
    bundle = tmp_path / 'sources.json'
    bundle.write_text(json.dumps({'../escape': 'bad'}))
    assert repair.source_problems(tmp_path / 'skill', bundle) == [
        'dev-browser source bundle contains an unsafe path'
    ]
