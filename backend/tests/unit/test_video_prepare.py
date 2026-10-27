"""Exercise the shipped preparation CLI and its on-disk recovery boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tool.video_production import VideoGenerateArgs


SCRIPT = Path(__file__).resolve().parents[2] / '.openbox/skills/video-production/scripts/prepare.py'


def plan():
    shots = []
    for index, line in enumerate(['女：你还记得这里吗？男：当然记得。', '女：那就再走一段。男：这次我陪你。'], 1):
        shots.append({'index': index, 'script': line, 'planned_seconds': 20,
                      'input_assets': [{'asset_id': 'fixture-person', 'role': 'reference_image'}],
                      'prompt': '全片一致的画面基底：同一男女主角，雨后公园\n'
                      '画面中不出现任何文字、字幕、标题、水印；字幕只能后期合成。\n'
                      '参考图片1用于人物身份。镜头设计：镜头跟随两人走动，近景与特写。\n'
                      '自然肢体动作：对视微笑。语气：轻松自然。\n口播台词：' + line})
    return {'slug': 'romance-40s', 'title': '雨后公园', 'script': '\n'.join(s['script'] for s in shots),
            'model': 'wan3.0-video', 'resolution': '720p', 'ratio': '9:16',
            'visual_anchor': '同一男女主角，雨后公园', 'shots': shots}


def run(tmp_path, value):
    source = tmp_path / 'plan.json'
    source.write_text(json.dumps(value, ensure_ascii=False))
    result = subprocess.run([sys.executable, str(SCRIPT), '--plan', str(source)],
                            env={**os.environ, 'VIDEO_STATE_ROOT': str(tmp_path / 'videos')},
                            capture_output=True, text=True, timeout=10)
    assert not result.stderr, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_two_shot_plan_saves_once_and_returns_real_tool_arguments_without_approval(tmp_path):
    draft = plan()
    code, result = run(tmp_path, draft)
    assert code == 0 and result['ok'] and not result['approval_recorded']
    state = json.loads(Path(result['state_path']).read_text())
    assert state['confirmations'] == {}
    assert [shot['prompt'] for shot in state['shots']] == [shot['prompt'] for shot in draft['shots']]
    assert [VideoGenerateArgs.model_validate(r).duration for r in result['validation_requests']] == [20, 20]
    assert 'quote_plan_request' not in result and 'estimate_requests' not in result
    before = Path(result['state_path']).stat().st_mtime_ns
    assert run(tmp_path, draft)[0] == 0
    assert Path(result['state_path']).stat().st_mtime_ns == before


def test_invalid_second_shot_does_not_partially_update_existing_draft(tmp_path):
    draft = plan()
    _, result = run(tmp_path, draft)
    path = Path(result['state_path'])
    before = path.read_bytes()
    draft['title'] = 'should not save'
    draft['shots'][1]['prompt'] = draft['shots'][1]['prompt'].replace('镜头设计：镜头跟随两人走动，', '')
    code, result = run(tmp_path, draft)
    assert code == 2 and not result['saved']
    issue = next(i for i in result['shots'][1]['issues'] if i['code'] == 'fixed_camera')
    assert '镜头设计：跟随人物走进门店' in issue['accepted_examples']
    assert path.read_bytes() == before


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(slug='../other'),
    lambda p: p.update(confirmations={'shots': {'approved': True}}),
    lambda p: p['shots'][0].update(job='already-paid'),
    lambda p: p['shots'][0].update(planned_seconds=-1),
    lambda p: p['shots'][0].update(planned_seconds=True),
    lambda p: p['shots'][0].update(index=2),
    lambda p: p['shots'][0]['input_assets'][0].update(role='guess'),
])
def test_invalid_or_approval_bearing_input_creates_no_notebook(tmp_path, mutate):
    draft = plan()
    mutate(draft)
    code, result = run(tmp_path, draft)
    assert code == 2 and not result['saved']
    assert not (tmp_path / 'videos').exists()


def test_generated_takes_are_preserved_and_plan_changes_require_explicit_revision(tmp_path):
    draft = plan()
    _, result = run(tmp_path, draft)
    path = Path(result['state_path'])
    data = json.loads(path.read_text())
    data['shots'][0].update(job='paid-job', asset='rendered-asset')
    data['confirmations']['shots'] = {'hash': 'existing-approval', 'note': 'real prior decision'}
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    assert run(tmp_path, draft)[0] == 0
    assert path.read_bytes() == original
    draft['shots'][0]['prompt'] += '\n天空变暗。'
    code, result = run(tmp_path, draft)
    assert code == 2 and 'generated takes' in result['error']
    assert path.read_bytes() == original


def test_silent_shots_validate_without_pricing_requests(tmp_path):
    draft = plan()
    draft['shots'][1].update(speech=False, script='')
    code, result = run(tmp_path, draft)
    assert code == 0
    assert [r['action'] for r in result['validation_requests']] == ['validate', 'validate']
    assert 'quote_plan_request' not in result


@pytest.mark.parametrize('new_ratio', ['16:9', None])
def test_ratio_changes_cannot_reuse_a_generated_take(tmp_path, new_ratio):
    draft = plan()
    _, result = run(tmp_path, draft)
    path = Path(result['state_path'])
    data = json.loads(path.read_text())
    data['shots'][0]['job'] = 'paid-job'
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    if new_ratio is None:
        draft.pop('ratio')
    else:
        draft['ratio'] = new_ratio
    code, result = run(tmp_path, draft)
    assert code == 2 and 'generated takes' in result['error']
    assert path.read_bytes() == before
