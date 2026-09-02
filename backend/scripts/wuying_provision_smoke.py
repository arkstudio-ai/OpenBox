"""Smoke-test the per-user ECD provisioning chain (WUYING_MODE=per_user).

Three tiers, escalating in side effects:

  check    Read-only, free. Verifies the AK can talk to ECD/EDS in the target
           region and lists office sites, OpenBox-usable images and existing
           OpenBox desktops — the data needed to pick WUYING_OFFICE_SITE_ID
           and WUYING_IMAGE_ID.
  enduser  Free, reversible. Runs the real EndUser flow for a throwaway user
           id (create -> sync-poll -> verify -> remove) without touching any
           desktop. Exercises the CreateUsers/DescribeUsers/RemoveUsers path
           end to end.
  full     BILLABLE. Provisions a real desktop for a throwaway user through
           WuyingDesktopService (the same code path the API uses), waits for
           Running, mints a connection ticket, then deletes the desktop and
           its EndUser. PostPaid billing charges for the minutes it runs.
           Requires --yes.

Usage:
  uv run python scripts/wuying_provision_smoke.py check --region cn-hangzhou
  uv run python scripts/wuying_provision_smoke.py enduser --region cn-hangzhou --salt test-salt
  uv run python scripts/wuying_provision_smoke.py full --region cn-hangzhou \
      --office-site 'cn-hangzhou+dir-...' --image-id m-... --salt test-salt --yes

Credentials come from ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET or the aliyun CLI
profile, same as the backend.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _apply_env(args: argparse.Namespace) -> None:
    """Config must be seeded before core.config is imported anywhere."""
    os.environ["WUYING_REGION_ID"] = args.region
    os.environ["WUYING_MODE"] = "per_user"
    if args.salt:
        os.environ["WUYING_PASSWORD_SALT"] = args.salt
    if args.office_site:
        os.environ["WUYING_OFFICE_SITE_ID"] = args.office_site
    if args.image_id:
        os.environ["WUYING_IMAGE_ID"] = args.image_id
    if args.env_tag:
        os.environ["WUYING_ENV_TAG"] = args.env_tag
    if args.desktop_type:
        os.environ["WUYING_DESKTOP_TYPE"] = args.desktop_type
    if args.disk:
        os.environ["WUYING_SYSTEM_DISK_SIZE"] = str(args.disk)


def _throwaway_user() -> str:
    return f"smoke-{uuid.uuid4().hex[:12]}"


async def tier_check(args: argparse.Namespace) -> int:
    from alibabacloud_ecd20200930 import models as ecd_models

    from sandbox import wuying_ecd

    client = wuying_ecd.ecd_client()

    print(f"== office sites ({args.region}) ==")
    resp = await client.describe_office_sites_async(
        ecd_models.DescribeOfficeSitesRequest(region_id=args.region, max_results=50)
    )
    sites = resp.body.office_sites or []
    for s in sites:
        print(f"  {s.office_site_id}  type={s.office_site_type}  status={s.status}  name={s.name}")
    if not sites:
        print("  (none — desktops cannot be created in this region)")

    print("== images ==")
    resp = await client.describe_images_async(
        ecd_models.DescribeImagesRequest(region_id=args.region, max_results=100)
    )
    images = resp.body.images or []
    for i in images:
        print(f"  {i.image_id}  type={i.image_type}  os={i.os_type}  status={i.status}  name={i.name}")
    if not images:
        print("  (no images visible — build the golden image first)")

    print(f"== existing OpenBox desktops (env tag: {os.environ.get('WUYING_ENV_TAG', 'default')}) ==")
    desktops = await wuying_ecd.list_desktops()
    for d in desktops:
        print(f"  {d['desktop_id']}  status={d['status']}  user={d['tags'].get(wuying_ecd.TAG_USER, '-')}")
    if not desktops:
        print("  (none)")

    print("== EDS user API ==")
    from alibabacloud_eds_user20210308 import models as eds_models

    eds = wuying_ecd.eds_user_client()
    resp = await eds.describe_users_async(eds_models.DescribeUsersRequest(max_results=10, filter="obx-"))
    users = resp.body.users or []
    print(f"  reachable; {len(users)} obx-* EndUser(s) visible")
    print("\ncheck tier passed — the AK can drive both APIs.")
    return 0


async def tier_enduser(args: argparse.Namespace) -> int:
    from alibabacloud_eds_user20210308 import models as eds_models

    from sandbox import wuying_ecd

    user_id = _throwaway_user()
    print(f"throwaway user: {user_id}")
    started = time.monotonic()
    eu_id, password = await wuying_ecd.ensure_end_user(user_id, "smoke test")
    print(f"created + synced: {eu_id} in {time.monotonic() - started:.0f}s (password derived, {len(password)} chars)")

    eds = wuying_ecd.eds_user_client()
    resp = await eds.describe_users_async(eds_models.DescribeUsersRequest(max_results=10, filter=eu_id))
    assert any(u.end_user_id == eu_id for u in (resp.body.users or [])), "EndUser not visible after sync"
    print("verified via DescribeUsers")

    removed = await wuying_ecd.remove_openbox_end_users([eu_id])
    print(f"cleaned up: {removed}")
    print("\nenduser tier passed.")
    return 0


async def tier_full(args: argparse.Namespace) -> int:
    if not args.yes:
        print("full tier creates a BILLABLE desktop; re-run with --yes to confirm.", file=sys.stderr)
        return 2
    if not (args.office_site and args.image_id and args.salt):
        print("full tier needs --office-site, --image-id and --salt.", file=sys.stderr)
        return 2

    # The service persists through the cloud_desktops repo; give this one-shot
    # script a throwaway in-memory DB instead of touching a real deployment's.
    from db.base import Base, init_engine

    engine = init_engine("sqlite+aiosqlite:///:memory:")
    import db.models  # noqa: F401  (registers all tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sandbox import wuying_ecd
    from sandbox.wuying_desktop_service import WuyingDesktopService

    service = WuyingDesktopService()
    user_id = _throwaway_user()
    print(f"throwaway user: {user_id}")

    desktop_id: str | None = None
    try:
        started = time.monotonic()
        state = await service.provision(user_id, "smoke test")
        print(f"provision kicked: {state}")
        while True:
            state = await service.status(user_id)
            elapsed = time.monotonic() - started
            print(f"  [{elapsed:5.0f}s] {state}")
            if state["state"] in ("running", "failed"):
                break
            await asyncio.sleep(10)
        desktop_id = state.get("desktopId")
        if state["state"] != "running":
            print(f"provisioning FAILED: {state.get('error')}", file=sys.stderr)
            return 1
        print(f"desktop Running in {time.monotonic() - started:.0f}s: {desktop_id}")

        target = await service.resolve_ticket_target(user_id)
        print(f"ownership verified, ticket target: desktop={target[0]} eu={target[1]}")

        from alibabacloud_ecd20200930 import models as ecd_models

        client = wuying_ecd.ecd_client()
        task_id: str | None = None
        ticket_started = time.monotonic()
        while time.monotonic() - ticket_started < 120:
            resp = await client.get_connection_ticket_async(
                ecd_models.GetConnectionTicketRequest(
                    desktop_id=target[0], end_user_id=target[1],
                    region_id=args.region, task_id=task_id,
                )
            )
            body = resp.body
            if body and (body.ticket or "").strip():
                print(f"connection ticket minted ({len(body.ticket)} chars) — the web view would connect now")
                break
            task_id = (body.task_id or "").strip() or task_id if body else task_id
            status = (body.task_status or "") if body else ""
            if status == "FAILED":
                print(f"ticket task FAILED: {body.task_message}", file=sys.stderr)
                return 1
            print(f"  ticket pending ({status or 'no task yet'})...")
            await asyncio.sleep(3)
        else:
            print("ticket never arrived within 120s", file=sys.stderr)
            return 1
        print("\nfull tier passed.")
        return 0
    finally:
        if args.keep:
            print(f"--keep: desktop {desktop_id} left running (delete it yourself!)")
        elif desktop_id:
            print(f"cleaning up: deleting desktop {desktop_id} + its EndUser...")
            try:
                await wuying_ecd.delete_desktop(desktop_id)
                print("cleanup done")
            except Exception as e:
                print(f"CLEANUP FAILED — delete {desktop_id} manually in the ECD console: {e}", file=sys.stderr)
        else:
            # CreateDesktops may have succeeded after the record was written;
            # sweep by tag so a half-provisioned desktop cannot leak billing.
            # The EndUser is created before the desktop, so remove it too.
            try:
                leaked = await wuying_ecd.list_desktops(user_id=user_id)
                for d in leaked:
                    print(f"cleaning up leaked desktop {d['desktop_id']}...")
                    await wuying_ecd.delete_desktop(d["desktop_id"])
                if not leaked:
                    await wuying_ecd.remove_openbox_end_users([wuying_ecd.eu_id_for(user_id)])
                    print("cleaned up the throwaway EndUser")
            except Exception as e:
                print(f"leak sweep failed: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tier", choices=["check", "enduser", "full"])
    parser.add_argument("--region", default=os.environ.get("WUYING_REGION_ID", "cn-hangzhou"))
    parser.add_argument("--office-site", default=os.environ.get("WUYING_OFFICE_SITE_ID", ""))
    parser.add_argument("--image-id", default=os.environ.get("WUYING_IMAGE_ID", ""))
    parser.add_argument("--salt", default=os.environ.get("WUYING_PASSWORD_SALT", ""))
    parser.add_argument("--env-tag", default=os.environ.get("WUYING_ENV_TAG", "smoke"))
    parser.add_argument("--desktop-type", default="")
    parser.add_argument("--disk", type=int, default=0, help="system disk GiB (must cover the image size)")
    parser.add_argument("--yes", action="store_true", help="confirm the billable full tier")
    parser.add_argument("--keep", action="store_true", help="full tier: keep the desktop instead of deleting")
    args = parser.parse_args()

    _apply_env(args)
    tier = {"check": tier_check, "enduser": tier_enduser, "full": tier_full}[args.tier]
    return asyncio.run(tier(args)) or 0


if __name__ == "__main__":
    sys.exit(main())
