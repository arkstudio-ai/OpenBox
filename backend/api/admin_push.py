"""Platform-admin push diagnostics, scoped to the requesting admin's phone."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from audit import record
from api.push import mobile_user, providers
from notifications import testing
from notifications.templates import TEMPLATES

router = APIRouter(prefix='/api/admin/push', tags=['admin-push'])


class TestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    template: str = 'system_test'
    bindingId: str = Field(min_length=1, max_length=64)
    requestId: UUID

    @field_validator('template')
    @classmethod
    def valid_template(cls, value):
        if value not in TEMPLATES: raise ValueError('Unknown notification template')
        return value


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['received', 'opened']


@router.get('')
async def overview(request: Request, locale: Literal['zh-CN', 'en-US'] = Query('zh-CN'),
                   user=Depends(testing.live_admin)):
    return await testing.overview(user, providers(request).enabled, locale)


@router.post('/test', status_code=202)
async def send_test(body: TestRequest, request: Request, user=Depends(testing.live_admin)):
    result = await testing.send_test(user, providers(request).enabled, template=body.template,
                                     binding_id=body.bindingId, request_id=body.requestId)
    await record(user['user_id'], None, 'admin.push_test', 'push_message', result['id'],
                 {'template': body.template}, request)
    return result


@router.post('/messages/{message_id}/receipt')
async def receipt(message_id: str, body: ReceiptRequest, user=Depends(testing.live_admin),
                  mobile=Depends(mobile_user)):
    return await testing.acknowledge(user, message_id, body.kind)
