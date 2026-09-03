"""ECD (无影云电脑) per-user provisioning primitives.

Ported from the reference integration in bossip (wuying-bridge/desktop_service.py),
keeping the behaviours that were learned the hard way there:

* A freshly created EndUser must be polled back via DescribeUsers (up to ~60s)
  before CreateDesktops accepts it — otherwise ECD raises InvalidParameter.
* Desktop ownership lives in tags, and tags MUST be read through the dedicated
  ListTagResources API: DescribeDesktops leaves ``tags`` empty on current ECD
  versions, so reading it there misdiagnoses "no tags" and breaks the
  ownership check.
* GetConnectionTicket can report NotFound for a desktop DescribeDesktops still
  lists ("ghost" desktop, inconsistent ECD backends). The only recovery is a
  hard delete + recreate; ``delete_desktop`` also removes the obx-* convenience
  EndUsers created alongside so accounts do not leak.
* Every desktop carries an environment tag so prod and dev sharing one Alibaba
  Cloud account never treat each other's desktops as orphans.

Identity model: each OpenBox user maps to one ECD convenience EndUser whose id
and password are derived deterministically (sha256) from the user id — user ids
here are arbitrary 64-char strings and the EndUser id has a 32-char/charset
limit, so hashing is mandatory, not cosmetic.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from core.aliyun import load_credentials
from core.config import get_config
from core.log import create_logger

log = create_logger("sandbox.wuying_ecd")

# ECD desktop resource type for the tag APIs.
_TAG_RESOURCE_TYPE = "ALIYUN::GWS::INSTANCE"

TAG_USER = "openbox-user"
TAG_EU = "openbox-eu-id"
TAG_ENV = "openbox-env"


class DesktopOwnershipError(Exception):
    """The desktop exists but is not tagged to the requesting user."""


class ProvisioningConfigError(Exception):
    """per_user mode is enabled but required config is missing."""


def _open_api_config(endpoint: str):
    from alibabacloud_tea_openapi import models as open_api_models

    creds = load_credentials()
    config = get_config()
    return open_api_models.Config(
        access_key_id=creds["access_key_id"],
        access_key_secret=creds["access_key_secret"],
        security_token=creds.get("security_token"),
        endpoint=endpoint,
        region_id=config.wuying_region_id,
    )


def ecd_client():
    from alibabacloud_ecd20200930.client import Client

    region = get_config().wuying_region_id
    return Client(_open_api_config(f"ecd.{region}.aliyuncs.com"))


def eds_user_client():
    from alibabacloud_eds_user20210308.client import Client

    # EDS User (convenience accounts) is a global service pinned to Shanghai
    # regardless of where the desktops live.
    return Client(_open_api_config("eds-user.cn-shanghai.aliyuncs.com"))


# ---------------------------------------------------------------------------
# Derived identity
# ---------------------------------------------------------------------------

def eu_id_for(user_id: str) -> str:
    """OpenBox user id -> ECD EndUser id: obx-<sha256[:16]>, 20 ascii chars.

    Display names are mutable / non-unique / possibly CJK, so they never feed
    the id; they only surface as the EndUser's real_nick_name.
    """
    return f"obx-{hashlib.sha256(user_id.encode()).hexdigest()[:16]}"


def password_for(user_id: str) -> str:
    """Stable password derived from user id + deployment salt.

    Wuying requires 8-30 chars with >=3 character classes; "Ob!" + 20 hex
    always satisfies that.
    """
    salt = get_config().wuying_password_salt
    if not salt:
        raise ProvisioningConfigError(
            "wuying_password_salt (WUYING_PASSWORD_SALT) is required in per_user mode"
        )
    digest = hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:20]
    return f"Ob!{digest}"


# ---------------------------------------------------------------------------
# EndUser lifecycle
# ---------------------------------------------------------------------------

async def ensure_end_user(user_id: str, display_name: str | None = None) -> tuple[str, str]:
    """Make sure this user's convenience EndUser exists; return (eu_id, password).

    After creating one, poll DescribeUsers until it shows up (<=60s): calling
    CreateDesktops before ECD finishes syncing the account fails with
    InvalidParameter.
    """
    from alibabacloud_eds_user20210308 import models as eds_models

    eu_id = eu_id_for(user_id)
    password = password_for(user_id)
    client = eds_user_client()

    existed = False
    try:
        resp = await client.describe_users_async(
            eds_models.DescribeUsersRequest(max_results=100, filter=eu_id)
        )
        existed = any(u.end_user_id == eu_id for u in (resp.body.users or []))
    except Exception as e:
        log.warning(f"DescribeUsers failed, proceeding to create: {e}")

    if existed:
        return eu_id, password

    # Alibaba requires a real-format email (.local is rejected); use a legal
    # placeholder with a hash suffix against collisions.
    email = f"{eu_id}-{hashlib.sha256(user_id.encode()).hexdigest()[:8]}@openbox.example.com"
    try:
        resp = await client.create_users_async(
            eds_models.CreateUsersRequest(
                users=[
                    eds_models.CreateUsersRequestUsers(
                        end_user_id=eu_id,
                        email=email,
                        password=password,
                        owner_type="CreateFromManager",
                        real_nick_name=display_name or eu_id,
                    )
                ]
            )
        )
        # CreateUsers reports failures in the body instead of raising.
        result = resp.body.create_result
        if result and result.failed_users:
            failed = result.failed_users[0]
            raise RuntimeError(f"CreateUsers failed: {failed.error_code}: {failed.error_message}")
        log.info(f"ECD EndUser created: {eu_id} (user={user_id})")
    except Exception as e:
        if any(word in str(e) for word in ("Exist", "exist", "Duplicate")):
            log.info(f"ECD EndUser already exists (concurrent create): {eu_id}")
        else:
            raise

    for attempt in range(30):
        await asyncio.sleep(2)
        try:
            check = await client.describe_users_async(
                eds_models.DescribeUsersRequest(max_results=10, filter=eu_id)
            )
            if any(u.end_user_id == eu_id for u in (check.body.users or [])):
                log.info(f"ECD EndUser {eu_id} synced after {(attempt + 1) * 2}s")
                break
        except Exception:
            pass
    else:
        log.warning(f"ECD EndUser {eu_id} sync timed out (60s); trying CreateDesktops anyway")

    return eu_id, password


async def remove_openbox_end_users(end_user_ids: list[str]) -> list[str]:
    """Remove obx-* convenience accounts created alongside a desktop.

    Only obx-prefixed accounts are touched so a manually created or legacy
    shared EndUser can never be deleted by desktop teardown.
    """
    from alibabacloud_eds_user20210308 import models as eds_models

    targets = sorted({u for u in end_user_ids if isinstance(u, str) and u.startswith("obx-")})
    if not targets:
        return []
    client = eds_user_client()
    resp = await client.remove_users_async(eds_models.RemoveUsersRequest(users=targets))
    result = getattr(resp.body, "remove_users_result", None)
    failed = list(getattr(result, "failed_users", []) or []) if result else []
    if failed:
        detail = "; ".join(
            f"{getattr(f, 'end_user_id', '-')}: {getattr(f, 'error_message', '-')}" for f in failed
        )
        raise RuntimeError(f"RemoveUsers failed: {detail}")
    removed = list(getattr(result, "removed_users", []) or targets) if result else targets
    log.info(f"ECD EndUsers removed: {', '.join(removed)}")
    return removed


# ---------------------------------------------------------------------------
# Desktop lifecycle
# ---------------------------------------------------------------------------

async def create_desktop(user_id: str, display_name: str | None = None) -> str:
    """Create a desktop owned by this user's own EndUser; return desktop id."""
    from alibabacloud_ecd20200930 import models as ecd_models

    config = get_config()
    if not config.wuying_image_id:
        raise ProvisioningConfigError(
            "wuying_image_id (WUYING_IMAGE_ID) is required in per_user mode; "
            "refusing to fall back to a community image"
        )
    if not config.wuying_office_site_id:
        raise ProvisioningConfigError(
            "wuying_office_site_id (WUYING_OFFICE_SITE_ID) is required in per_user mode"
        )
    if not config.wuying_policy_group_id:
        raise ProvisioningConfigError(
            "wuying_policy_group_id (WUYING_POLICY_GROUP_ID) is required in per_user "
            "mode; the policy group pins the 1920x1080 session resolution, and "
            "Alibaba's default policy is resolution-adaptive — refusing to create "
            "a desktop that would drift from what the agent screenshots"
        )

    eu_id, _ = await ensure_end_user(user_id, display_name)
    client = ecd_client()

    # DesktopName must stay ascii-safe -> reuse eu_id; the human-readable name
    # lives on the EndUser's real_nick_name. Ownership/lookup always goes
    # through the openbox-user tag.
    request = ecd_models.CreateDesktopsRequest(
        region_id=config.wuying_region_id,
        office_site_id=config.wuying_office_site_id,
        policy_group_id=config.wuying_policy_group_id,
        charge_type=config.wuying_charge_type,
        desktop_name=eu_id[:32],
        amount=1,
        end_user_id=[eu_id],
        tag=[
            ecd_models.CreateDesktopsRequestTag(key=TAG_USER, value=user_id),
            ecd_models.CreateDesktopsRequestTag(key=TAG_EU, value=eu_id),
            ecd_models.CreateDesktopsRequestTag(key=TAG_ENV, value=config.wuying_env_tag),
        ],
        desktop_attachment=ecd_models.CreateDesktopsRequestDesktopAttachment(
            image_id=config.wuying_image_id,
            desktop_type=config.wuying_desktop_type,
            system_disk_size=config.wuying_system_disk_size,
        ),
    )
    resp = await client.create_desktops_async(request)
    desktop_ids = resp.body.desktop_id
    if not desktop_ids:
        raise RuntimeError("CreateDesktops returned no desktop id")
    desktop_id = desktop_ids[0]
    log.info(f"Desktop created: {desktop_id} (user={user_id} -> eu={eu_id})")
    return desktop_id


async def describe_desktop(desktop_id: str) -> dict[str, Any] | None:
    from alibabacloud_ecd20200930 import models as ecd_models

    config = get_config()
    client = ecd_client()
    resp = await client.describe_desktops_async(
        ecd_models.DescribeDesktopsRequest(region_id=config.wuying_region_id, desktop_id=[desktop_id])
    )
    desktops = resp.body.desktops or []
    if not desktops:
        return None
    d = desktops[0]
    return {
        "desktop_id": d.desktop_id,
        "name": d.desktop_name,
        "status": d.desktop_status,
        "progress": getattr(d, "progress", None),
        "hostname": getattr(d, "host_name", None),
        "private_ip": getattr(d, "network_interface_ip", None),
        "end_user_ids": list(getattr(d, "end_user_ids", []) or []),
    }


async def wait_desktop_ready(desktop_id: str, timeout_sec: int = 360, poll_interval: int = 5) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        info = await describe_desktop(desktop_id)
        if info is None:
            raise RuntimeError(f"Desktop {desktop_id} does not exist")
        log.info(f"Desktop {desktop_id}: {info['status']} ({info.get('progress')})")
        if info["status"] == "Running":
            return
        if info["status"] in ("Failed", "Error"):
            raise RuntimeError(f"Desktop provisioning failed: {info['status']}")
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"Desktop {desktop_id} not Running within {timeout_sec}s")


async def start_desktop(desktop_id: str) -> None:
    from alibabacloud_ecd20200930 import models as ecd_models

    client = ecd_client()
    await client.start_desktops_async(
        ecd_models.StartDesktopsRequest(
            region_id=get_config().wuying_region_id, desktop_id=[desktop_id]
        )
    )
    log.info(f"Desktop start requested: {desktop_id}")


async def stop_desktop(desktop_id: str) -> None:
    from alibabacloud_ecd20200930 import models as ecd_models

    client = ecd_client()
    await client.stop_desktops_async(
        ecd_models.StopDesktopsRequest(
            region_id=get_config().wuying_region_id,
            desktop_id=[desktop_id],
            stopped_mode="KeepCharging",
        )
    )
    log.info(f"Desktop stopped: {desktop_id}")


async def delete_desktop(desktop_id: str) -> None:
    """Hard-delete a desktop and its obx-* EndUsers (ghost-desktop recovery)."""
    from alibabacloud_ecd20200930 import models as ecd_models

    info = await describe_desktop(desktop_id)
    end_user_ids = list((info or {}).get("end_user_ids") or [])
    client = ecd_client()
    await client.delete_desktops_async(
        ecd_models.DeleteDesktopsRequest(
            region_id=get_config().wuying_region_id, desktop_id=[desktop_id]
        )
    )
    log.info(f"Desktop deleted: {desktop_id}")
    if end_user_ids:
        # Removing the EndUser right after DeleteDesktops fails with "Used in
        # some region" until the desktop teardown propagates (observed ~a
        # minute). Retry in the background so callers (ghost release on the
        # ticket path) are not blocked on it.
        asyncio.get_event_loop().create_task(_remove_end_users_with_retry(end_user_ids))


async def _remove_end_users_with_retry(
    end_user_ids: list[str], attempts: int = 6, delay_sec: float = 20.0
) -> None:
    for attempt in range(attempts):
        try:
            await remove_openbox_end_users(end_user_ids)
            return
        except Exception as e:
            if attempt == attempts - 1:
                log.warning(
                    f"EndUser cleanup gave up after {attempts} attempts "
                    f"({', '.join(end_user_ids)}): {e}"
                )
                return
            log.info(f"EndUser cleanup not ready yet (attempt {attempt + 1}): {e}")
            await asyncio.sleep(delay_sec)


async def desktop_tags(desktop_id: str) -> dict[str, str]:
    """Read a desktop's tags through ListTagResources (the only reliable way)."""
    tags = await list_desktop_tags([desktop_id])
    return tags.get(desktop_id, {})


async def list_desktop_tags(desktop_ids: list[str]) -> dict[str, dict[str, str]]:
    """Batch tag read: desktop_id -> {key: value}.

    ECD accepts at most 50 resource ids per call and may paginate. Failures
    propagate: tags are the ownership boundary, so an unknown owner must never
    be waved through.
    """
    from alibabacloud_ecd20200930 import models as ecd_models

    config = get_config()
    client = ecd_client()
    ids = list(dict.fromkeys(d for d in desktop_ids if d))
    result: dict[str, dict[str, str]] = {d: {} for d in ids}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        next_token: str | None = None
        while True:
            resp = await client.list_tag_resources_async(
                ecd_models.ListTagResourcesRequest(
                    region_id=config.wuying_region_id,
                    resource_type=_TAG_RESOURCE_TYPE,
                    resource_id=batch,
                    next_token=next_token,
                    max_results=100,
                )
            )
            body = resp.body
            for row in getattr(body, "tag_resources", None) or []:
                resource_id = getattr(row, "resource_id", "")
                key = getattr(row, "tag_key", "")
                if resource_id in result and key:
                    result[resource_id][key] = getattr(row, "tag_value", "")
            next_token = getattr(body, "next_token", "") or None
            if not next_token:
                break
    return result


async def list_desktops(user_id: str | None = None) -> list[dict[str, Any]]:
    """List OpenBox-created desktops in this environment, optionally per user."""
    from alibabacloud_ecd20200930 import models as ecd_models

    config = get_config()
    client = ecd_client()
    request = ecd_models.DescribeDesktopsRequest(
        region_id=config.wuying_region_id, max_results=100
    )
    tag_filters = [
        ecd_models.DescribeDesktopsRequestTag(key=TAG_ENV, value=config.wuying_env_tag)
    ]
    if user_id:
        tag_filters.append(ecd_models.DescribeDesktopsRequestTag(key=TAG_USER, value=user_id))
    request.tag = tag_filters
    resp = await client.describe_desktops_async(request)
    desktops = resp.body.desktops or []
    tags = await list_desktop_tags([d.desktop_id for d in desktops])
    return [
        {
            "desktop_id": d.desktop_id,
            "name": d.desktop_name,
            "status": d.desktop_status,
            "hostname": getattr(d, "host_name", None),
            "private_ip": getattr(d, "network_interface_ip", None),
            "end_user_ids": list(getattr(d, "end_user_ids", []) or []),
            "tags": tags.get(d.desktop_id, {}),
        }
        for d in desktops
    ]


async def verify_ownership(desktop_id: str, user_id: str) -> str:
    """Assert the desktop is tagged to this user; return the eu_id to use.

    Prevents user A presenting user B's desktop id to mint B's ticket.
    """
    tags = await desktop_tags(desktop_id)
    owner = tags.get(TAG_USER)
    if owner is None:
        raise DesktopOwnershipError(
            f"Desktop {desktop_id} carries no {TAG_USER} tag; refusing (ownership unknown)"
        )
    if owner != user_id:
        raise DesktopOwnershipError(
            f"Desktop {desktop_id} belongs to another user; refusing"
        )
    return tags.get(TAG_EU) or eu_id_for(user_id)
