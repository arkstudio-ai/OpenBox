"""Legacy root screenshots cannot prevent an unprivileged desktop capture."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.desktop import OBX_SHOT_SCRIPT
from tool.tool import ToolContext


@pytest.mark.parametrize('legacy', ['readonly', 'symlink', 'occupied-fallback'])
def test_capture_preserves_legacy_file_and_reports_its_actual_png(tmp_path, monkeypatch, capsys, legacy):
    image = pytest.importorskip('PIL.Image')
    from PIL import ImageGrab
    import os
    frame = image.new('RGB', (320, 180), 'white')
    frame.paste('black', (40, 40, 100, 100))
    monkeypatch.setattr(ImageGrab, 'grab', lambda: frame.copy())
    original = tmp_path / 'retained.png'
    original.write_bytes(b'retained legacy screenshot')
    output = tmp_path / 'screen.png'
    if legacy == 'symlink':
        output.symlink_to(original)
    else:
        output.write_bytes(original.read_bytes())
        output.chmod(0o400)
    fallback = Path(str(output) + '.' + str(os.geteuid()))
    if legacy == 'occupied-fallback':
        fallback.symlink_to(original)
    monkeypatch.setattr('sys.argv', ['obx-shot', '160', '90', str(output)])
    script = OBX_SHOT_SCRIPT.replace('sampler = ScreenSampler()', 'sampler = FixtureSampler()')
    sampler = SimpleNamespace(pointer=lambda: None, close=lambda: None)
    exec(compile(script, 'obx-shot', 'exec'), {'__name__': '__main__', 'FixtureSampler': lambda: sampler})
    geometry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    captured = Path(geometry['path'])
    assert captured != output and captured.is_file()
    assert output.read_bytes() == original.read_bytes() == b'retained legacy screenshot'
    if legacy == 'occupied-fallback':
        assert fallback.is_symlink() and captured != fallback
    assert geometry['native'] == [320, 180] and geometry['scaled'] == [160, 90]
    assert geometry['sha256'] == hashlib.sha256(captured.read_bytes()).hexdigest()
    assert geometry['bytes'] == captured.stat().st_size


async def test_attachment_reads_actual_capture_instead_of_legacy_path(monkeypatch):
    from tool.computer import _attach_screenshot
    paths = []
    async def attach(ctx, path, *_args, **_kwargs):
        paths.append(path)
        return 'fixture-asset', 123
    async def capture(*_args):
        pass
    monkeypatch.setattr('sandbox.assets.attach_sandbox_image', attach)
    monkeypatch.setattr('assistant.resource_observations.capture', capture)
    geometry = {'scaled': [160, 90], 'bytes': 123, 'path': '/tmp/obx-screen.png.997'}
    assert await _attach_screenshot(ToolContext(), geometry) == '160x90'
    assert paths == [geometry['path']]
