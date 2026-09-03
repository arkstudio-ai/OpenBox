"""Install, route, verify, and revoke one cloud desktop execution channel."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import get_config
from core.log import create_logger
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox.client import SandboxClient
from sandbox import wuying_ecd

log = create_logger("sandbox.wuying_channel")

_CIPHERTEXT_PREFIX = "v1:"
_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")


class ChannelConfigError(RuntimeError):
    pass


class ChannelNotReady(RuntimeError):
    pass


def _master_key(value: str | None = None) -> bytes:
    raw = (value if value is not None else get_config().wuying_channel_key).strip()
    if not raw:
        raise ChannelConfigError("WUYING_CHANNEL_KEY is required for per-desktop routing")
    try:
        key = bytes.fromhex(raw) if len(raw) == 64 else base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise ChannelConfigError("WUYING_CHANNEL_KEY must be 32 bytes encoded as hex or base64") from exc
    if len(key) != 32:
        raise ChannelConfigError("WUYING_CHANNEL_KEY must decode to exactly 32 bytes")
    return key


def encrypt_action_key(plaintext: str, master_key: str | None = None) -> str:
    nonce = os.urandom(12)
    sealed = AESGCM(_master_key(master_key)).encrypt(nonce, plaintext.encode(), b"openbox:wuying:action:v1")
    return _CIPHERTEXT_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")


def decrypt_action_key(ciphertext: str, master_key: str | None = None) -> str:
    if not ciphertext.startswith(_CIPHERTEXT_PREFIX):
        raise ChannelConfigError("unsupported WUYING action-key ciphertext version")
    try:
        raw = base64.urlsafe_b64decode(ciphertext[len(_CIPHERTEXT_PREFIX):] + "===")
        clear = AESGCM(_master_key(master_key)).decrypt(
            raw[:12], raw[12:], b"openbox:wuying:action:v1"
        )
        return clear.decode()
    except ChannelConfigError:
        raise
    except Exception as exc:
        raise ChannelConfigError("cannot decrypt desktop action key") from exc


def action_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def parse_port_range(value: str) -> tuple[int, int]:
    try:
        low_text, high_text = value.split("-", 1)
        low, high = int(low_text), int(high_text)
    except (TypeError, ValueError) as exc:
        raise ChannelConfigError("WUYING_TUNNEL_PORT_RANGE must look like 18100-18999") from exc
    if not 1024 <= low <= high <= 65535:
        raise ChannelConfigError("WUYING_TUNNEL_PORT_RANGE must contain valid non-privileged ports")
    return low, high


def _b64_file(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


async def run_desktop_command(desktop_id: str, script: str, timeout: int = 300) -> str:
    """Run a root shell command through ECD Cloud Assistant and return output."""
    from alibabacloud_ecd20200930 import models as ecd_models

    config = get_config()
    client = wuying_ecd.ecd_client()
    response = await client.run_command_async(
        ecd_models.RunCommandRequest(
            region_id=config.wuying_region_id,
            desktop_id=[desktop_id],
            type="RunShellScript",
            timeout=timeout,
            content_encoding="Base64",
            command_content=base64.b64encode(script.encode()).decode(),
        )
    )
    invoke_id = getattr(response.body, "invoke_id", "")
    if not invoke_id:
        raise RuntimeError("RunCommand returned no invocation id")

    deadline = asyncio.get_running_loop().time() + timeout + 30
    while asyncio.get_running_loop().time() < deadline:
        result = await client.describe_invocations_async(
            ecd_models.DescribeInvocationsRequest(
                region_id=config.wuying_region_id,
                invoke_id=invoke_id,
                include_invoke_desktops=True,
                include_output=True,
            )
        )
        invocations = getattr(result.body, "invocations", None) or []
        targets = getattr(invocations[0], "invoke_desktops", None) or [] if invocations else []
        target = targets[0] if targets else None
        state = getattr(target, "invocation_status", "") if target else ""
        if state in ("Success", "Failed", "Timeout", "Stopped"):
            encoded = getattr(target, "output", "") or ""
            output = base64.b64decode(encoded).decode("utf-8", "replace") if encoded else ""
            exit_code = getattr(target, "exit_code", None)
            if state != "Success" or exit_code not in (None, 0, "0"):
                raise RuntimeError(
                    f"desktop command {state or 'failed'} (exit {exit_code}): {output[-2000:]}"
                )
            return output
        await asyncio.sleep(3)
    raise TimeoutError(f"desktop invocation {invoke_id} did not finish")


def route_for_record(record: dict) -> tuple[str, int, str]:
    """Return host, port, API key for a verified live record."""
    if record.get("tunnel_state") != "up":
        raise ChannelNotReady(f"desktop channel is {record.get('tunnel_state') or 'pending'}")
    kind = record.get("channel_kind")
    if kind == "direct":
        host, port = record.get("private_ip"), 8000
    elif kind == "ssh":
        host, port = record.get("tunnel_bind"), record.get("tunnel_port")
    else:
        raise ChannelNotReady("desktop channel kind is not configured")
    if not host or not port or not record.get("action_api_key_ciphertext"):
        raise ChannelNotReady("desktop channel route is incomplete")
    return host, int(port), decrypt_action_key(record["action_api_key_ciphertext"])


class WuyingChannel:
    async def install(self, record: dict, *, rotate_key: bool = False) -> dict:
        desktop_id = record.get("desktop_id")
        if not desktop_id:
            raise ChannelNotReady("cannot install a channel before desktop creation")
        config = get_config()
        kind = config.wuying_channel
        if kind not in ("direct", "ssh"):
            raise ChannelConfigError("WUYING_CHANNEL must be direct or ssh")

        api_key = (
            decrypt_action_key(record["action_api_key_ciphertext"])
            if record.get("action_api_key_ciphertext") and not rotate_key
            else secrets.token_urlsafe(36)
        )
        common = {
            "channel_kind": kind,
            "action_api_key_hash": action_key_hash(api_key),
            "action_api_key_ciphertext": encrypt_action_key(api_key),
            "tunnel_state": "pending",
            "channel_error": None,
        }

        if kind == "direct":
            info = await wuying_ecd.describe_desktop(desktop_id)
            private_ip = (info or {}).get("private_ip")
            if not private_ip:
                raise ChannelNotReady(f"desktop {desktop_id} has no private IP")
            await cloud_desktop_repo.update(record["id"], private_ip=private_ip, **common)
            action_env = _b64_file(f"SESSION_API_KEY={api_key}\n")
            await run_desktop_command(
                desktop_id,
                f"""set -eu
install -d -m 700 /etc/openbox
printf '%s' '{action_env}' | base64 -d > /etc/openbox/action.env
chmod 600 /etc/openbox/action.env
systemctl daemon-reload
systemctl enable --now openbox-action-server
systemctl restart openbox-action-server
""",
            )
        else:
            for name in ("wuying_relay_host", "wuying_relay_user", "wuying_relay_hostkey"):
                if not getattr(config, name):
                    raise ChannelConfigError(f"{name.upper()} is required for the ssh channel")
            low, high = parse_port_range(config.wuying_tunnel_port_range)
            port = await cloud_desktop_repo.reserve_tunnel_port(record["id"], low, high)
            tunnel_env = "\n".join(
                [
                    f"RELAY_HOST={config.wuying_relay_host}",
                    f"RELAY_PORT={config.wuying_relay_port}",
                    f"RELAY_USER={config.wuying_relay_user}",
                    f"TUNNEL_BIND={config.wuying_tunnel_bind}",
                    f"TUNNEL_PORT={port}",
                    "",
                ]
            )
            await cloud_desktop_repo.update(
                record["id"], tunnel_port=port, tunnel_bind=config.wuying_tunnel_bind, **common
            )
            output = await run_desktop_command(
                desktop_id,
                f"""set -eu
install -d -m 700 /etc/openbox
printf '%s' '{_b64_file(f'SESSION_API_KEY={api_key}\n')}' | base64 -d > /etc/openbox/action.env
printf '%s' '{_b64_file(tunnel_env)}' | base64 -d > /etc/openbox/tunnel.env
printf '%s' '{_b64_file(config.wuying_relay_hostkey.strip() + chr(10))}' | base64 -d > /etc/openbox/known_hosts
chmod 600 /etc/openbox/action.env /etc/openbox/tunnel.env /etc/openbox/known_hosts
[ -s /etc/openbox/tunnel_key ] || ssh-keygen -q -t ed25519 -N '' -C openbox-tunnel-{desktop_id} -f /etc/openbox/tunnel_key
chmod 600 /etc/openbox/tunnel_key
systemctl daemon-reload
systemctl enable --now openbox-action-server openbox-tunnel
systemctl restart openbox-action-server openbox-tunnel
echo OPENBOX_PUBKEY="$(cat /etc/openbox/tunnel_key.pub)"
echo OPENBOX_FINGERPRINT="$(ssh-keygen -lf /etc/openbox/tunnel_key.pub -E sha256 | awk '{{print $2}}')"
""",
            )
            pub_line = next(
                (line.removeprefix("OPENBOX_PUBKEY=") for line in output.splitlines() if line.startswith("OPENBOX_PUBKEY=")),
                "",
            )
            fingerprint_line = next(
                (line.removeprefix("OPENBOX_FINGERPRINT=") for line in output.splitlines() if line.startswith("OPENBOX_FINGERPRINT=")),
                "",
            )
            fingerprint_match = _FINGERPRINT_RE.search(fingerprint_line)
            if not pub_line.startswith("ssh-ed25519 ") or not fingerprint_match:
                raise RuntimeError("desktop returned an invalid tunnel public key or fingerprint")
            await cloud_desktop_repo.update(
                record["id"],
                tunnel_pubkey=" ".join(pub_line.split()[:2]),
                tunnel_fingerprint=fingerprint_match.group(0),
            )

        installed = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
        if installed is None:
            raise ChannelNotReady("desktop record disappeared during channel installation")
        return installed

    async def verify(self, record: dict, timeout_sec: int = 180) -> dict:
        """Require authenticated execution and the fixed 1920x1080 display."""
        deadline = asyncio.get_running_loop().time() + timeout_sec
        last_error = "channel did not answer"
        while asyncio.get_running_loop().time() < deadline:
            try:
                provisional = {**record, "tunnel_state": "up"}
                host, port, api_key = route_for_record(provisional)
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    alive = await client.get(f"http://{host}:{port}/alive")
                    alive.raise_for_status()
                sandbox = SandboxClient(host=host, port=port, api_key=api_key)
                # obx-display touches the live desktop session and therefore
                # must obey the same action-server lease as real computer
                # turns.  A raw execute is correctly rejected with HTTP 423.
                async with sandbox.desktop_lease(
                    session_id=f"channel-verify:{record['id']}",
                    tool_call_id="channel-verify",
                    wait_timeout=20,
                    ttl_seconds=60,
                ):
                    result = await sandbox.execute(
                        "hostname; obx-x obx-display; "
                        "obx-x sh -c \"xrandr --current | grep -qE '1920x1080[^0-9]'\"",
                        timeout=20,
                    )
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr.strip() or "desktop is not 1920x1080")
                now = datetime.now(timezone.utc)
                await cloud_desktop_repo.update(
                    record["id"], tunnel_state="up", last_seen_at=now, channel_error=None
                )
                return {"hostname": result.stdout.splitlines()[0].strip(), "last_seen_at": now}
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:2000]
                await asyncio.sleep(3)
        await cloud_desktop_repo.update(
            record["id"], tunnel_state="down", channel_error=last_error
        )
        raise ChannelNotReady(last_error)

    async def probe(self, record: dict) -> bool:
        if record.get("tunnel_state") == "revoked":
            return False
        try:
            provisional = {**record, "tunnel_state": "up"}
            host, port, api_key = route_for_record(provisional)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    f"http://{host}:{port}/system",
                    headers={"X-API-Key": api_key},
                )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            await cloud_desktop_repo.update(
                record["id"], tunnel_state="up", last_seen_at=datetime.now(timezone.utc), channel_error=None
            )
            return True
        except Exception as exc:
            if record.get("tunnel_state") == "up":
                await cloud_desktop_repo.update(
                    record["id"], tunnel_state="down", channel_error=str(exc)[:2000]
                )
            return False

    async def revoke(self, record: dict) -> None:
        """Cut application routing first, then best-effort stop the guest tunnel."""
        await cloud_desktop_repo.update(record["id"], tunnel_state="revoked")
        if record.get("desktop_id") and record.get("channel_kind") == "ssh":
            try:
                await run_desktop_command(
                    record["desktop_id"], "systemctl disable --now openbox-tunnel || true", timeout=60
                )
            except Exception as exc:
                log.warning("Could not stop revoked tunnel on %s: %s", record["desktop_id"], exc)


wuying_channel = WuyingChannel()
