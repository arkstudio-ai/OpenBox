"""The narrow stream patch must preserve the surrounding guest runtime."""
import base64
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend/scripts'))
try:
    import wuying_patch_stream_output as patch
finally:
    sys.path.pop(0)


def test_only_reader_and_final_receipt_change_and_repeat_patch_is_stable():
    original = patch.LOCAL.read_text()
    reader, receipt = [patch.fragment(original, n) for n in patch.stream_nodes(original)]
    old = original.replace('process.stdout.read(16384)', 'process.stdout.readline()')
    old = old.replace('"output_complete": stdout_done and stderr_done,', '"legacy_marker": True,')
    fixed = patch.patched_source(old, reader, receipt)
    assert fixed == original
    assert patch.patched_source(fixed, reader, receipt) == fixed


def test_retained_patch_is_bounded_and_does_not_publish_other_modules():
    source = patch.LOCAL.read_text()
    reader, receipt = [patch.fragment(source, n) for n in patch.stream_nodes(source)]
    script = patch.publication_script('0' * 64, reader, receipt, 'test_release')
    assert len(base64.b64encode(script.encode())) <= 16 * 1024
    assert 'original-action_server.py' in script and 'changed since inspection' in script
    assert 'rm ' not in script and 'unlink' not in script and 'rmtree' not in script
    assert '/workspace' not in script and 'apt-get' not in script and 'resource_gate.py' not in script


def test_unknown_guest_layout_fails_before_source_is_changed():
    import pytest
    with pytest.raises(ValueError):
        patch.patched_source('async def execute_stream(req, request):\n    return None\n', '', '')
