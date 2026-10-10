"""Install, route, verify, and revoke one cloud desktop execution channel."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import get_config
from core.log import create_logger
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox.client import SandboxClient
from sandbox import wuying_ecd
from sandbox.browser_runtime import (
    BrowserRuntimeUnavailable,
    ensure_browser_runtime,
    ensure_desktop_browser_runtime,
)

log = create_logger("sandbox.wuying_channel")

_CIPHERTEXT_PREFIX = "v1:"
_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")


class ChannelConfigError(RuntimeError):
    pass


class ChannelNotReady(RuntimeError):
    pass


class ChannelVerificationStopped(ChannelNotReady):
    """The original binding lost authority; callers must stop old recovery."""


class _VerificationStopped(BaseException):
    """Abort nested recovery without being swallowed as a runtime failure.

    The public verify boundary converts this internal control signal to
    ChannelNotReady; it must never escape to lifecycle workers as cancellation.
    """


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
    response = await wuying_ecd.cloud_call(lambda: client.run_command_async(
        ecd_models.RunCommandRequest(
            region_id=config.wuying_region_id,
            desktop_id=[desktop_id],
            type="RunShellScript",
            timeout=timeout,
            content_encoding="Base64",
            command_content=base64.b64encode(script.encode()).decode(),
        )
    ))
    invoke_id = getattr(response.body, "invoke_id", "")
    if not invoke_id:
        raise RuntimeError("RunCommand returned no invocation id")

    deadline = asyncio.get_running_loop().time() + timeout + 30
    while asyncio.get_running_loop().time() < deadline:
        result = await wuying_ecd.cloud_call(lambda: client.describe_invocations_async(
            ecd_models.DescribeInvocationsRequest(
                region_id=config.wuying_region_id,
                invoke_id=invoke_id,
                include_invoke_desktops=True,
                include_output=True,
            )
        ))
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


class ChannelAttempt:
    """A durable local lifecycle owner, never a remote exclusivity claim."""

    def __init__(self, record: dict, authority_check=None, *, maintenance=False):
        self.record = dict(record)
        self.authority_check = authority_check
        self.maintenance = maintenance
        self.states = ("pending", "up", "down", "revoked") if maintenance else ("pending", "up", "down")

    async def current(self, *, states=None):
        if self.authority_check is not None:
            await self.authority_check()
        if not await cloud_desktop_repo.channel_attempt_current(
            self.record, states=states or self.states, maintenance=self.maintenance,
        ):
            raise ChannelVerificationStopped("Channel attempt stopped: original binding is no longer current")

    async def write(self, *, states=None, session=None, **fields):
        if self.authority_check is not None and session is None:
            await self.authority_check()
        updated = await cloud_desktop_repo.write_channel_attempt(
            self.record, fields, states=states or self.states, session=session, maintenance=self.maintenance)
        if updated is None:
            raise ChannelVerificationStopped("Channel attempt stopped: original binding is no longer current")
        self.record = updated
        return dict(updated)

    async def call(self, operation, *args, states=None, **kwargs):
        await self.current(states=states)
        try:
            with wuying_ecd.operation_authority(lambda: self.current(states=states)):
                result = await operation(*args, **kwargs)
        except wuying_ecd.CloudAuthorityStopped as stopped:
            raise stopped.cause from stopped
        except Exception:
            await self.current(states=states)
            raise
        await self.current(states=states)
        return result

    async def install(self, *, rotate_key=False):
        if self.maintenance:
            raise ChannelVerificationStopped("Maintenance does not authorize channel installation")
        installed = await wuying_channel.install(self.record, rotate_key=rotate_key, attempt=self)
        self.record = dict(installed)
        return dict(installed)

    async def verify(self):
        if self.maintenance:
            raise ChannelVerificationStopped("Maintenance does not authorize channel verification")
        result = await wuying_channel.verify(self.record, authority_check=self.authority_check)
        # verify writes only health, with the same durable attempt in its CAS.
        # Do not follow a fresh row by id after remote IO.
        self.record = {**self.record, "tunnel_state": "up"}
        await self.current()
        return result

    async def revoke(self):
        await self.write(tunnel_state="revoked")
        await wuying_channel._stop_revoked(self)


class WuyingChannel:
    async def maintain(self, record: dict, *, authority_check=None, revoke=False, fields=None, reuse=False):
        if authority_check is not None:
            await authority_check()
        claimed = await cloud_desktop_repo.claim_channel_maintenance(
            dict(record), revoke=revoke, fields=fields, reuse=reuse)
        if claimed is None:
            raise ChannelVerificationStopped("Maintenance stopped: original binding is no longer current")
        return ChannelAttempt(claimed, authority_check, maintenance=True)

    async def begin(self, record: dict, *, authority_check=None) -> ChannelAttempt:
        if authority_check is not None:
            await authority_check()
        claimed = await cloud_desktop_repo.claim_channel_attempt(dict(record))
        if claimed is None:
            raise ChannelVerificationStopped("Channel attempt stopped: original binding is no longer current")
        return ChannelAttempt(claimed, authority_check)

    async def install(self, record: dict, *, rotate_key: bool = False, attempt: ChannelAttempt | None = None) -> dict:
        attempt = attempt or await self.begin(record)
        if attempt.maintenance:
            raise ChannelVerificationStopped("Maintenance does not authorize channel installation")
        await attempt.current()
        record = dict(attempt.record)
        desktop_id = record.get("desktop_id")
        if not desktop_id:
            raise ChannelNotReady("cannot install a channel before desktop creation")
        config = get_config()
        kind = config.wuying_channel
        if kind not in ("direct", "ssh"):
            raise ChannelConfigError("WUYING_CHANNEL must be direct or ssh")

        # Repair new/pooled guests before starting their application channel.
        await ensure_desktop_browser_runtime(desktop_id, authority_check=attempt.current)

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
            info = await attempt.call(wuying_ecd.describe_desktop, desktop_id)
            private_ip = (info or {}).get("private_ip")
            if not private_ip:
                raise ChannelNotReady(f"desktop {desktop_id} has no private IP")
            await attempt.write(private_ip=private_ip, **common)
            action_env = _b64_file(f"SESSION_API_KEY={api_key}\n")
            await attempt.call(run_desktop_command,
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
            reserved = await cloud_desktop_repo.reserve_attempt_port(attempt.record, low, high)
            if reserved is None:
                raise ChannelVerificationStopped("Channel attempt stopped while reserving its port")
            attempt.record = reserved
            port = reserved["tunnel_port"]
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
            await attempt.write(tunnel_bind=config.wuying_tunnel_bind, **common)
            output = await attempt.call(run_desktop_command,
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
            await attempt.write(
                tunnel_pubkey=" ".join(pub_line.split()[:2]),
                tunnel_fingerprint=fingerprint_match.group(0),
            )

        await attempt.current()
        return dict(attempt.record)

    async def verify(self, record: dict, timeout_sec: int = 180, *, authority_check=None) -> dict:
        """Require execution/browser readiness; validate a display when present."""
        snapshot = dict(record)
        try:
            return await self._verify_bound(snapshot, timeout_sec, authority_check=authority_check)
        except _VerificationStopped as exc:
            from sandbox import events
            await events.emit(
                "channel.verify", status="fail", desktop_id=snapshot.get("desktop_id") or "",
                session_id=f"channel-verify:{snapshot['id']}", summary=str(exc),
            )
            raise ChannelVerificationStopped(str(exc)) from None

    async def _verify_bound(self, record: dict, timeout_sec: int, *, authority_check=None) -> dict:
        from sandbox import events

        async def current():
            if authority_check is not None:
                try:
                    await authority_check()
                except Exception as exc:
                    raise _VerificationStopped("Channel verification caller lost authority") from exc
            if not await cloud_desktop_repo.channel_binding_current(record):
                raise _VerificationStopped("Channel verification stopped: original binding is no longer current")

        async def write_health(**fields):
            if not await cloud_desktop_repo.record_channel_verification(record, **fields):
                raise _VerificationStopped("Channel verification stopped: original binding is no longer current")

        class BoundClient(SandboxClient):
            async def _authorize_request(self, request):
                # Releasing our existing lease is cleanup, not new authority.
                if request.url.path != "/desktop/lease/release":
                    await current()
                await super()._authorize_request(request)

            async def _observe_resource_response(self, response):
                await super()._observe_resource_response(response)
                if response.request.url.path not in ("/desktop/lease/acquire", "/desktop/lease/release"):
                    await current()

            @asynccontextmanager
            async def desktop_lease(self, **kwargs):
                # Let the real lease context retain its returned token first,
                # so a stop during acquire still releases that exact lease.
                async with super().desktop_lease(**kwargs) as lease:
                    await current()
                    yield lease

        await current()
        started = asyncio.get_running_loop().time()
        deadline = started + timeout_sec
        last_error = "channel did not answer"
        boot_recovery_attempted = False
        sandbox = None
        attempts = 0
        desktop_id = record.get("desktop_id") or ""

        async def _outcome(status: str, summary: str, **detail) -> None:
            await events.emit(
                "channel.verify", status=status, desktop_id=desktop_id,
                session_id=f"channel-verify:{record['id']}", summary=summary,
                duration_ms=round((asyncio.get_running_loop().time() - started) * 1000),
                detail={"attempts": attempts, "boot_recovery": boot_recovery_attempted, **detail},
                diag_id=detail.get("diag_id", ""),
            )

        while asyncio.get_running_loop().time() < deadline:
            attempts += 1
            try:
                await current()
                provisional = {**record, "tunnel_state": "up"}
                host, port, api_key = route_for_record(provisional)
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    alive = await client.get(f"http://{host}:{port}/alive")
                    alive.raise_for_status()
                await current()
                sandbox = BoundClient(host=host, port=port, api_key=api_key, desktop_id=desktop_id)
                # obx-display touches the live desktop session and therefore
                # must obey the same action-server lease as real computer
                # turns.  A raw execute is correctly rejected with HTTP 423.
                async with sandbox.desktop_lease(
                    session_id=f"channel-verify:{record['id']}",
                    tool_call_id="channel-verify",
                    wait_timeout=20,
                    ttl_seconds=60,
                ):
                    await current()
                    result = await sandbox.execute(
                        "set -eu; hostname; "
                        "if obx-x true >/dev/null 2>&1; then obx-x obx-display; "
                        "obx-x sh -c \"xrandr --current | grep -qE '1920x1080[^0-9]'\"; "
                        "else echo OPENBOX_NO_DISPLAY; fi",
                        timeout=20,
                    )
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr.strip() or "desktop is not 1920x1080")
                await current()
                try:
                    await ensure_browser_runtime(sandbox, repair=False)
                except BrowserRuntimeUnavailable:
                    # Execution children deliberately have no root authority.
                    # Repair system packages through the fenced cloud channel,
                    # then require the real execution identity to pass again.
                    # A root-only check may miss a 0700/0600 package tree.
                    await current()
                    await ensure_desktop_browser_runtime(record["desktop_id"],
                        authority_check=current, force_repair=True)
                    await current()
                    await ensure_browser_runtime(sandbox, repair=False)
                from sandbox.browser import ChromeUnavailable, RelayUnavailable, ensure_browser, is_headless
                # Runtime presence alone is insufficient: require live CDP
                # and the local relay before the activation worker says Ready.
                try:
                    await current()
                    browser = await ensure_browser(sandbox, record["desktop_id"], "local")
                except (ChromeUnavailable, RelayUnavailable) as exc:
                    raise BrowserRuntimeUnavailable("Desktop browser could not start safely") from exc
                if (
                    not (browser.get("chrome") or {}).get("webSocketDebuggerUrl")
                    or not (browser.get("relay") or {}).get("chromeAvailable")
                ):
                    raise BrowserRuntimeUnavailable("Desktop browser CDP/relay did not pass readiness")
                now = datetime.now(timezone.utc)
                await write_health(state="up", seen_at=now)
                verified = {
                    "hostname": result.stdout.splitlines()[0].strip(),
                    "last_seen_at": now,
                    "display_ready": "OPENBOX_NO_DISPLAY" not in result.stdout,
                    "browser_presentation": "headless" if is_headless(browser["chrome"]) else "headed",
                }
                await _outcome(
                    "ok",
                    f"channel up ({verified['browser_presentation']}, display "
                    f"{'ready' if verified['display_ready'] else 'absent'}) after {attempts} attempt(s)",
                    hostname=verified["hostname"], display_ready=verified["display_ready"],
                    browser_presentation=verified["browser_presentation"],
                )
                return verified
            except BrowserRuntimeUnavailable as exc:
                await current()
                # Let durable activation retry this desktop, not buy another.
                # The browser layer already snapshotted the desktop for its
                # own failures; do the same for the readiness checks here.
                from sandbox.diag import capture_failure
                diag_id = getattr(exc, "diag_id", "") or getattr(exc.__cause__, "diag_id", "")
                if not diag_id and sandbox is not None:
                    diag_id = await capture_failure(
                        sandbox, container_key=record["desktop_id"], desktop_id=record["desktop_id"],
                        session_id=f"channel-verify:{record['id']}",
                        reason="channel.verify", error=exc,
                    )
                log.warning("channel verify failed for %s (diag=%s): %s", record["desktop_id"], diag_id, exc)
                await write_health(error=f"{str(exc)[:1900]} [diag:{diag_id}]" if diag_id else str(exc))
                await _outcome(
                    "fail", f"browser not ready: {str(exc).splitlines()[0][:200]}",
                    diag_id=diag_id, problems=getattr(exc, "problems", []),
                )
                raise
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                await current()
                if not boot_recovery_attempted:
                    boot_recovery_attempted = True
                    # A failed boot dependency can keep the action server
                    # offline. Cloud Assistant can repair it without a live
                    # application tunnel or any desktop purchase/rebuild.
                    log.warning("channel for %s unreachable (%s); attempting boot recovery via Cloud Assistant",
                                record["desktop_id"], exc)
                    await ensure_desktop_browser_runtime(record["desktop_id"], authority_check=current)
                last_error = f"{type(exc).__name__}: {exc}"[:2000]
                log.info("channel verify retry for %s: %s", record["desktop_id"], last_error[:300])
                await asyncio.sleep(3)
            except Exception as exc:
                await current()
                last_error = f"{type(exc).__name__}: {exc}"[:2000]
                log.warning("channel verify attempt failed for %s: %s", record["desktop_id"], last_error[:500])
                await asyncio.sleep(3)
        await write_health(state="down", error=last_error)
        await _outcome("timeout", f"no answer in {timeout_sec}s: {last_error[:200]}", last_error=last_error)
        raise ChannelNotReady(last_error)

    async def probe(self, record: dict) -> bool:
        # Do not let a caller mutate the target/credential while IO is pending.
        record = dict(record)
        if record.get("tunnel_state") == "revoked":
            return False
        try:
            provisional = {**record, "tunnel_state": "up"}
            host, port, api_key = route_for_record(provisional)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    f"http://{host}:{port}/system_info",
                    headers={"X-API-Key": api_key},
                )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            return await cloud_desktop_repo.record_channel_probe(record, healthy=True)
        except Exception as exc:
            if record.get("tunnel_state") == "up":
                await cloud_desktop_repo.record_channel_probe(record, healthy=False, error=str(exc)[:2000])
            return False

    async def revoke(self, record: dict) -> dict:
        """Cut application routing first, then best-effort stop the guest tunnel."""
        revoked = await cloud_desktop_repo.revoke_channel_assignment(dict(record))
        if revoked is None:
            raise ChannelVerificationStopped("Channel revocation stopped: original assignment is no longer current")
        attempt = ChannelAttempt(revoked)
        await self._stop_revoked(attempt)
        return dict(attempt.record)

    async def _stop_revoked(self, attempt: ChannelAttempt) -> None:
        record = attempt.record
        if record.get("desktop_id") and record.get("channel_kind") == "ssh":
            try:
                await attempt.call(run_desktop_command,
                    record["desktop_id"], "systemctl disable --now openbox-tunnel", timeout=60,
                    states=("revoked",),
                )
            except ChannelVerificationStopped:
                raise
            except Exception as exc:
                log.warning("Could not stop revoked tunnel on %s: %s", record["desktop_id"], exc)
            else:
                # The database uniqueness constraint also covers historical
                # soft-deleted rows. Release the reservation only after the
                # guest tunnel has stopped, so a future desktop can safely
                # reuse the relay port without racing a stale listener.
                await attempt.write(states=("revoked",), tunnel_port=None, tunnel_bind=None)


wuying_channel = WuyingChannel()
