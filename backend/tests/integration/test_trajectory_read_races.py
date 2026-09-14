"""Real auth and app routing revalidate authority after slow content reads."""
from urllib.parse import quote

import pytest
from sqlalchemy import select

from auth.jwt import create_access_token, decode_access_token
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.trajectory import SessionTrajectory
from db.models.user import User
from tests.integration.test_trajectory_storage import tracedb
from tests.integration.test_trajectory_boundaries import app_client
from trajectory import TraceContext, delete_trajectory_in_tx, ensure_trajectory_in_tx, record
from trajectory.payload import delete_for_asset, drain_payloads, store_bytes
from trajectory.types import now


async def attachment(factory):
    context=TraceContext('a','session_a_1')
    async with factory.begin() as db:
        db.add(FileAsset(id='race_asset',user_id='a',workspace_id='ws_a',session_id='session_a_1',
            name='fixture.png',oss_key='local-fixture-never-fetched',mime='image/png',
            size=21,status='ready',created_at=now()))
        await db.flush()
        trajectory=await ensure_trajectory_in_tx(db,context)
        reference=await store_bytes(db,trajectory.id,b'PRIVATE_MEDIA_CONTENT',first_seq=trajectory.next_seq,
            media_type='image/png',source_asset_id='race_asset')
        await record('artifact.recorded',{'artifact_id':'race_asset','payload':reference},context=context,db=db)
    await drain_payloads()
    return reference


@pytest.mark.parametrize('resource',['payload','export'])
@pytest.mark.parametrize('change,status',[
    ('role',403),('revoked',401),('asset_deleted',410),('payload_deleted',410),('root_deleted',404),
])
async def test_download_rechecks_during_blob_read(app_client,tracedb,monkeypatch,resource,change,status):
    _,client,cache,token=app_client
    factory,blob=tracedb
    reference=await attachment(factory)
    prefix='/api/admin/trajectories/sessions/session_a_1'
    path=prefix+'/payloads/'+reference['payload_id']
    if resource=='export':
        created=await client.post(prefix+'/export',json={})
        info=await client.get(prefix+'/exports/'+created.json()['export_id'])
        path=info.json()['download_url']
    original=blob.download
    async def interrupted(key):
        content=await original(key)
        if change=='revoked':
            await cache.set('jwt_bl:'+decode_access_token(token)['jti'],True,ttl=60)
        else:
            async with factory.begin() as db:
                if change=='role':
                    (await db.get(User,'admin')).role='user'
                elif change=='asset_deleted':
                    asset=await db.get(FileAsset,'race_asset')
                    asset.is_deleted=True
                    asset.deleted_at=now()
                elif change=='payload_deleted':
                    await delete_for_asset(db,'race_asset')
                else:
                    await delete_trajectory_in_tx(db,'session_a_1','a')
                    (await db.get(Session,'session_a_1')).is_deleted=True
        return content
    monkeypatch.setattr(blob,'download',interrupted)
    response=await client.get(path)
    assert response.status_code==status,response.text
    assert b'PRIVATE_MEDIA_CONTENT' not in response.content
    assert response.headers['cache-control']=='no-store'


async def test_admin_json_and_ticket_are_not_cacheable(app_client,tracedb):
    _,client,_,_=app_client
    factory,_=tracedb
    await attachment(factory)
    prefix='/api/admin/trajectories/sessions/session_a_1'
    paths=['/api/admin/trajectories/sessions',prefix,prefix+'/events',prefix+'/records',
           prefix+'/records/artifact:race_asset',prefix+'/checkpoint',prefix+'/search?q=race',
           prefix+'/events?until_seq=invalid',prefix+'/records?limit=invalid']
    for path in paths:
        response=await client.get(path)
        assert response.status_code in {200,400,422},response.text
        assert response.headers['cache-control']=='no-store'
    response=await client.post('/api/admin/trajectories/ticket')
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    token=create_access_token('a','admin')
    response=await client.get(prefix,headers={'Authorization':f'Bearer {token}'})
    assert response.status_code==403 and response.headers['cache-control']=='no-store'


@pytest.mark.parametrize('artifact_id',[
    'file:/workspace/report.md',
    'file:/workspace/100%/literal%2Fname.md',
    'file:/workspace/报告 ?draft#1.md',
])
async def test_record_detail_accepts_encoded_path_identity(app_client,tracedb,artifact_id):
    _,client,_,_=app_client
    await record('artifact.recorded',{'artifact_id':artifact_id,'text':'stored fixture'},
        context=TraceContext('a','session_a_1'))
    prefix='/api/admin/trajectories/sessions/session_a_1'
    header=(await client.get(prefix)).json()
    record_id='artifact:'+artifact_id
    response=await client.get(prefix+'/records/'+quote(record_id,safe=''),
        params={'through_seq':header['through_seq']})
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    assert response.json()['record']['record_id']==record_id
    assert response.json()['record']['data']['text']=='stored fixture'
    assert response.json()['through_seq']==header['through_seq']
