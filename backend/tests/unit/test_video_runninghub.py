"""RunningHub Turbo contract: separate endpoints, protected references and polling."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from core.config import OpenBoxConfig
from tool import video_production as vp, video_providers as providers
from tool.video_runninghub import MODEL, PREFIX


@pytest.fixture
def config(monkeypatch):
    cfg = OpenBoxConfig.model_validate({
        'provider': {'runninghub': {'api_key': 'test-only', 'base_url': 'https://video.invalid'}},
        'video_generation': {
            'model': MODEL, 'default_resolution': '768p', 'default_duration': 5,
            'models': [{
                'id': MODEL, 'channel': 'runninghub', 'provider': 'runninghub',
                'resolutions': ['480p', '768p'], 'duration_range': [5, 15],
                'supports_smart_duration': False, 'supports_first_last_frame': True,
                'supports_reference_video': False, 'supports_reference_audio': False,
            }],
        },
    })
    monkeypatch.setattr('core.config.get_config', lambda: cfg)
    return cfg


@pytest.fixture
def route(config):
    return providers.resolve_route(MODEL, config)


def payload(route, **overrides):
    args = dict(prompt='小狗玩球', refs=[], resolution='768p', ratio='16:9',
                duration=5, generate_audio=True, watermark=False)
    args.update(overrides)
    return providers.build_payload(route, **args)


def test_route_has_dedicated_protocol_and_bearer_auth(route):
    assert route.channel == 'runninghub' and route.wire_format == 'runninghub_v2'
    assert providers.auth_header(route) == 'Bearer test-only'


def test_text_payload_matches_official_contract(route):
    path, body = payload(route)
    assert path == PREFIX + '/text-to-video'
    assert body == {'prompt': '小狗玩球', 'resolution': '768p', 'duration': '5', 'aspectRatio': '16:9'}


@pytest.mark.parametrize('roles', [('first_frame',), ('last_frame', 'first_frame')])
def test_image_payload_routes_frames_without_text_aspect_ratio(route, roles):
    refs = [{'kind': 'image', 'role': role, 'url': f'https://asset.invalid/{role}.png'} for role in roles]
    path, body = payload(route, refs=refs, ratio='adaptive')
    assert path == PREFIX + '/image-to-video'
    assert body['firstFrameUrl'] == refs[-1]['url']
    assert body['duration'] == '5' and body['resolution'] == '768p'
    assert 'aspectRatio' not in body and 'ratio' not in body and 'model' not in body
    assert ('lastFrameUrl' in body) == (len(roles) == 2)
    if len(roles) == 2:
        assert body['lastFrameUrl'] == refs[0]['url']


@pytest.mark.parametrize('changes', [
    {'duration': 4}, {'duration': 16}, {'duration': -1}, {'resolution': '720p'},
    {'prompt': 'a' * 2049}, {'prompt': ''}, {'watermark': True}, {'generate_audio': False},
    {'ratio': 'adaptive'},
    {'refs': [{'kind': 'video', 'url': 'https://a.invalid/x.mp4'}]},
    {'refs': [{'kind': 'audio', 'url': 'https://a.invalid/x.mp3'}]},
    {'refs': [{'kind': 'image', 'role': 'last_frame', 'url': 'https://a.invalid/x.png'}], 'ratio': 'adaptive'},
    {'refs': [{'kind': 'image', 'role': 'reference_image', 'url': 'https://a.invalid/x.png'}], 'ratio': 'adaptive'},
    {'refs': [{'kind': 'image', 'url': 'https://a.invalid/x.png'}], 'ratio': 'adaptive'},
    {'refs': [{'kind': 'image', 'url': 'https://a.invalid/x.png'}] * 2, 'ratio': 'adaptive'},
    {'refs': [{'kind': 'image', 'url': 'https://a.invalid/x.png'}] * 3, 'ratio': 'adaptive'},
    {'refs': [{'kind': 'image', 'url': 'https://a.invalid/x.png'}], 'ratio': '16:9'},
])
def test_unsupported_inputs_fail_before_paid_submit(route, changes):
    with pytest.raises(providers.VideoRequestError):
        payload(route, **changes)


@pytest.mark.parametrize('mime', ['image/gif', 'image/svg+xml', 'image/heic'])
def test_unsupported_image_formats_are_not_sent_as_png(route, mime):
    with pytest.raises(providers.VideoRequestError, match='JPEG/PNG/WEBP'):
        providers.validate_request(route, resolution='768p', ratio='adaptive', duration=5,
                                   generate_audio=True, input_mimes=[mime])


@pytest.mark.parametrize('images,ratio', [(False, '9:16'), (True, 'adaptive')])
async def test_default_selection_and_image_ratio_follow_conversation_inputs(config, monkeypatch, images, ratio):
    monkeypatch.setattr(vp, '_session_video_model_id', AsyncMock(return_value=None))
    monkeypatch.setattr(vp, '_session_video_resolution', AsyncMock(return_value=None))
    args = vp.VideoGenerateArgs(action='estimate', prompt='小狗玩球',
                                input_assets=[{'asset_id': 'image-one'}] if images else [])
    approved = await vp._resolve_open_submission(args, object())
    assert approved['ratio'] == ratio and approved['resolution'] == '768p'
    assert approved['duration'] == 5
    # A real explicit ratio is not silently discarded for image generation.
    approved = await vp._resolve_open_submission(args.model_copy(update={'ratio': '1:1'}), object())
    assert approved['ratio'] == '1:1'


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


async def test_submit_and_query_use_different_post_paths_and_string_id(route, monkeypatch):
    task_id = '2102958721100820482'
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'POST' and request.headers['Authorization'] == 'Bearer test-only'
        body = json.loads(request.content)
        if request.url.path == PREFIX + '/text-to-video':
            assert body['duration'] == '5'
            return httpx.Response(200, json={'taskId': task_id, 'status': 'QUEUED', 'errorCode': ''})
        assert request.url.path == '/openapi/v2/query' and body == {'taskId': task_id}
        return httpx.Response(200, json={'taskId': task_id, 'status': 'SUCCESS', 'errorCode': '',
                             'results': [{'outputType': 'mp4', 'url': 'https://asset.invalid/out.mp4'}]})
    mock_http(monkeypatch, handler)
    path, body = payload(route)
    accepted = await providers.submit(route, path, body)
    assert providers.extract_task_id(route, accepted) == task_id
    done = await providers.status(route, task_id)
    assert providers.normalize_state(route, done) == 'completed'
    assert providers.result_video_url(route, done) == 'https://asset.invalid/out.mp4'
    assert len(calls) == 2


async def test_http_200_business_error_is_a_safe_rejection_without_secret_echo(route, monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={
        'taskId': None, 'errorCode': '605', 'errorMessage': 'SECRET provider internals',
    }))
    with pytest.raises(providers.VideoRequestError) as exc:
        await providers.submit(route, *payload(route))
    detail = providers.submission_rejection(exc.value)
    assert detail['code'] == 'runninghub_605' and detail['submission_outcome'] == 'rejected'
    assert detail['retryable'] is False and 'SECRET' not in str(detail)


async def test_error_with_accepted_task_is_kept_for_recovery(route, monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={
        'taskId': '2102958721100820482', 'status': 'QUEUED', 'errorCode': '999',
    }))
    data = await providers.submit(route, *payload(route))
    assert providers.extract_task_id(route, data) == '2102958721100820482'
    assert providers.normalize_state(route, data) == 'queued'


async def test_query_auth_error_is_not_terminal_task_failure(route, monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={'errorCode': '401', 'errorMessage': 'SECRET'}))
    with pytest.raises(providers.VideoRequestError) as exc:
        await providers.status(route, 'task-one')
    assert 'SECRET' not in str(exc.value)


@pytest.mark.parametrize('data,expected', [
    ({'status': 'QUEUED'}, 'queued'), ({'status': 'RUNNING'}, 'in_progress'),
    ({'status': 'FAILED', 'errorCode': '605'}, 'failed'), ({'status': 'CANCELLED'}, 'cancelled'),
    ({'status': 'SUCCESS', 'results': None}, 'in_progress'),
    ({'status': 'SUCCESS', 'results': [{'outputType': 'png', 'url': 'https://a.invalid/x.png'}]}, 'in_progress'),
])
def test_status_contract_never_finishes_without_a_video(route, data, expected):
    assert providers.normalize_state(route, data) == expected


@pytest.mark.parametrize('roles,valid', [
    ([None], False), (['reference_image'], False),
    (['reference_image', 'reference_image'], False),
    (['first_frame', 'last_frame', 'reference_image'], False),
    (['first_frame'], True), (['last_frame', 'first_frame'], True),
])
async def test_estimate_checks_explicit_intent_and_reports_asset_bindings(config, route, monkeypatch, roles, valid):
    from types import SimpleNamespace
    from tool.tool import ToolContext
    # Actual asset IDs are resolved before routing; list order does not decide
    # which of the pictures becomes the start or end of the shot.
    rows = {f'asset-{i}': SimpleNamespace(id=f'asset-{i}', mime='image/png') for i in range(len(roles))}
    async def owned(asset_id, _ctx):
        return rows[asset_id]
    monkeypatch.setattr(vp, '_find_owned_asset', owned)
    monkeypatch.setattr(vp, '_session_video_model_id', AsyncMock(return_value=None))
    monkeypatch.setattr(vp, '_session_video_resolution', AsyncMock(return_value=None))
    monkeypatch.setattr(vp, '_daily_submit_count', AsyncMock(return_value=0))
    monkeypatch.setattr(vp, '_configured_target', lambda _model: (route, config.video_generation))
    submit = AsyncMock(side_effect=AssertionError('an estimate must never pay'))
    monkeypatch.setattr(providers, 'submit', submit)
    args = vp.VideoGenerateArgs(action='estimate', prompt='按指定素材生成', input_assets=[
        {'asset_id': f'asset-{i}', 'role': role} for i, role in enumerate(roles)
    ])
    result = await vp.execute_generate(args, ToolContext(user_id='u', session_id='s'))
    assert result.metadata['valid'] is valid, result.output
    if valid:
        assert result.metadata['input_bindings'] == [
            {'asset_id': f'asset-{i}', 'role': role} for i, role in enumerate(roles)
        ]
        assert 'operation=image-to-video' in result.output
        assert result.metadata['estimated_credits'] == '1.7'
    else:
        assert 'estimated_credits' not in result.metadata
    submit.assert_not_awaited()


async def test_reusing_an_asset_as_both_endpoints_does_not_silently_drop_its_last_frame(monkeypatch):
    from types import SimpleNamespace
    row = SimpleNamespace(id='loop-frame', mime='image/png')
    monkeypatch.setattr(vp, '_find_owned_asset', AsyncMock(return_value=row))
    refs = [vp.VideoInputRef(asset_id=row.id, role=role) for role in ['first_frame', 'last_frame', 'first_frame']]
    rows, roles = await vp._resolve_open_inputs(refs, object())
    assert [r.id for r in rows] == [row.id, row.id]
    assert roles == ['first_frame', 'last_frame']
