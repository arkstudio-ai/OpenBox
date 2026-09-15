"""Trajectory bucket access for the host-driven ops tools (SPEC §12).

Backups and the rebuild drill run as one-off commands in the worker image with
the worker's environment, so they resolve the bucket the way SPEC §13 defines
it for the worker: TRAJECTORY_OSS_BUCKET/_REGION fall back to OSS_BUCKET/
OSS_REGION, the endpoint is derived from the region (never OSS_ENDPOINT, which
may be a browser-facing host), and the internal VPC host is used unless
TRAJECTORY_OSS_INTERNAL is false. Requests are presigned with core.oss.
"""
import os
from pathlib import Path
from xml.etree import ElementTree

import httpx

from core.aliyun import load_credentials
from core.oss import OssClient

BACKEND_DIR = Path(__file__).resolve().parents[2]


class OpsStorageError(Exception):
    pass


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bucket_settings(environ=None) -> dict:
    env = os.environ if environ is None else environ
    region = env.get("TRAJECTORY_OSS_REGION") or env.get("OSS_REGION") or "cn-hangzhou"
    return {
        "bucket": env.get("TRAJECTORY_OSS_BUCKET") or env.get("OSS_BUCKET") or "",
        "region": region,
        "endpoint": env.get("TRAJECTORY_OSS_ENDPOINT") or f"oss-{region}.aliyuncs.com",
        "internal": _flag(env.get("TRAJECTORY_OSS_INTERNAL"), True),
    }


def oss_client(environ=None) -> tuple[OssClient, bool]:
    """The trajectory bucket client and whether requests should use its internal host."""
    settings = bucket_settings(environ)
    if not settings["bucket"]:
        raise OpsStorageError("TRAJECTORY_OSS_BUCKET (or OSS_BUCKET) is not set")
    credentials = load_credentials()
    client = OssClient(
        settings["bucket"], settings["region"], settings["endpoint"],
        credentials["access_key_id"], credentials["access_key_secret"],
    )
    return client, settings["internal"]


def blob_provider(environ=None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("TRAJECTORY_BLOB_PROVIDER") or "local").strip().lower()


def local_blob_root(environ=None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("TRAJECTORY_BLOB_LOCAL_PATH") or BACKEND_DIR / ".openbox" / "trajectory-blobs")


def http_client(transport: httpx.AsyncBaseTransport | None = None, timeout: float = 120.0) -> httpx.AsyncClient:
    # trust_env=False: proxy variables in a container environment must not
    # route signed bucket traffic (same rule as trajectory/artifacts.py).
    return httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=15.0),
    )


def describe_error(response: httpx.Response) -> str:
    """Status, OSS error code and request id of a failed call; never the signed URL."""
    code = request_id = ""
    try:
        root = ElementTree.fromstring(response.content)
        code = root.findtext("Code") or ""
        request_id = root.findtext("RequestId") or ""
    except ElementTree.ParseError:
        pass
    request_id = request_id or response.headers.get("x-oss-request-id", "")
    text = f"HTTP {response.status_code}"
    if code:
        text += f" {code}"
    if request_id:
        text += f" (request {request_id})"
    return text
