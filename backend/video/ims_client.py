"""Aliyun IMS (智能媒体服务) cloud-editing calls, over the generic RPC client.

Two operations are all composition needs: ``SubmitMediaProducingJob`` and
``GetMediaProducingJob``. They go through ``alibabacloud_tea_openapi``'s
generic ``call_api`` rather than the generated ICE SDK, because that SDK
requires ``tea-openapi>=0.4.5`` and this deployment pins ``<0.4.0`` for the
Wuying integration (see pyproject). The wire shape is what the generated SDK
sends: RPC style, parameters in the query string, JSON back.

Credentials come from ``core.aliyun.load_credentials`` — the same chain the
desktop fleet uses. Tests replace ``_rpc``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.log import create_logger

log = create_logger("video.ims")

API_VERSION = "2020-11-09"

#: IMS job states that mean "still working". Anything else that is not
#: Success/Failed is treated as running too, so a new intermediate state
#: cannot strand a job as failed.
_RUNNING = {"Init", "Queuing", "Processing"}


class ImsError(Exception):
    """A refusal or failure IMS reported; safe to show (no URLs, no keys)."""

    public_message = True

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"IMS {code}: {message[:300]}")


class ImsNotActivated(ImsError):
    """403 Forbidden from IMS = the service is not activated for this account."""


@dataclass(frozen=True)
class ImsJobState:
    job_id: str
    status: str            # OpenBox status: in_progress | completed | failed
    raw_status: str        # IMS Status as returned
    media_url: str | None
    duration_sec: float | None
    code: str | None
    message: str | None


def _endpoint(region: str) -> str:
    from core.config import get_config

    override = (get_config().video_compose.endpoint or "").strip()
    return override or f"ice.{region}.aliyuncs.com"


def _region() -> str:
    from core.config import get_config

    config = get_config()
    return (config.video_compose.region or config.oss_region or "").strip()


async def _rpc(action: str, params: dict[str, str]) -> dict[str, Any]:
    """One RPC-style call. Replaced in tests."""
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_openapi.client import Client
    from alibabacloud_tea_util import models as util_models

    from core.aliyun import load_credentials

    creds = load_credentials()
    region = _region()
    if not region:
        raise ImsError("NotConfigured", "no IMS region: set video_compose.region in openbox.json or OSS_REGION")
    client = Client(open_api_models.Config(
        access_key_id=creds["access_key_id"],
        access_key_secret=creds["access_key_secret"],
        security_token=creds.get("security_token"),
        endpoint=_endpoint(region),
        region_id=region,
    ))
    call = open_api_models.Params(
        action=action, version=API_VERSION, protocol="HTTPS", pathname="/", method="POST",
        auth_type="AK", style="RPC", req_body_type="formData", body_type="json",
    )
    request = open_api_models.OpenApiRequest(query={k: v for k, v in params.items() if v is not None})
    runtime = util_models.RuntimeOptions(read_timeout=30_000, connect_timeout=10_000)
    try:
        from agent.trajectory import capture_service_dispatch, observe_service_response
        if action == "SubmitMediaProducingJob":
            async with capture_service_dispatch(purpose="media_composition", provider="aliyun_ims", model="ims",
                    operation=action, body=request.query, profile="media_composition",
                    capture_level="adapter_input") as capture:
                response = await client.call_api_async(call, request, runtime)
                body = response.get("body") if isinstance(response, dict) else None
                await capture.chunk({"response": {"output": body if isinstance(body, dict) else response}})
        else:
            response = await client.call_api_async(call, request, runtime)
            body = response.get("body") if isinstance(response, dict) else None
            await observe_service_response(body if isinstance(body, dict) else response, operation=action)
    except Exception as exc:  # TeaException carries code/message/statusCode
        from trajectory.types import TrajectoryError
        if isinstance(exc, TrajectoryError):
            raise
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        message = str(getattr(exc, "message", "") or exc)
        status_code = getattr(exc, "statusCode", None) or getattr(getattr(exc, "data", None) or {}, "get", lambda *_: None)("statusCode")
        log.warning(f"IMS {action} failed: {code} (http {status_code})")
        if code == "Forbidden" or status_code == 403:
            raise ImsNotActivated("Forbidden", "IMS is not activated or not authorised for this account") from exc
        raise ImsError(code, message) from exc
    body = response.get("body") if isinstance(response, dict) else None
    return body if isinstance(body, dict) else (response or {})


async def submit_media_producing_job(
    *, timeline: str, output_media_config: str, client_token: str, user_data: str | None = None,
) -> str:
    """Submit a compiled job; returns the IMS JobId."""
    params = {
        "Timeline": timeline,
        "OutputMediaTarget": "oss-object",
        "OutputMediaConfig": output_media_config,
        "ClientToken": client_token,
        "Source": "OpenAPI",
        "UserData": user_data,
    }
    body = await _rpc("SubmitMediaProducingJob", params)
    job_id = str(body.get("JobId") or "")
    if not job_id:
        raise ImsError("NoJobId", "submit returned no JobId")
    return job_id


async def get_media_producing_job(job_id: str) -> ImsJobState:
    body = await _rpc("GetMediaProducingJob", {"JobId": job_id})
    job = body.get("MediaProducingJob") if isinstance(body, dict) else None
    if not isinstance(job, dict):
        raise ImsError("BadResponse", "GetMediaProducingJob returned no job")
    raw = str(job.get("Status") or "")
    if raw == "Success":
        status = "completed"
    elif raw == "Failed":
        status = "failed"
    else:
        status = "in_progress"
    duration = job.get("Duration")
    return ImsJobState(
        job_id=job_id,
        status=status,
        raw_status=raw,
        media_url=job.get("MediaURL") or None,
        duration_sec=float(duration) if isinstance(duration, (int, float)) else None,
        code=(job.get("Code") or None),
        message=(job.get("Message") or None),
    )
