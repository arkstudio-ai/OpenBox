"""PostgreSQL backup uploads for deploy/gw2/scripts/pg-backup.sh (SPEC §12).

The script dumps each database inside the postgres container onto the host and
streams the file into this helper in the worker container, which already has
the mounted aliyun credentials:

    docker compose exec -T trajectory-worker python -m trajectory.ops.backup upload \\
        --key backups/postgres/20260915/openbox-20260914T193000Z.dump \\
        --size 170123456 --sha256 <hex> < openbox-20260914T193000Z.dump

The PUT goes to a core.oss presigned URL (internal host unless
TRAJECTORY_OSS_INTERNAL is false) with an exact Content-Length and the sha256 as
x-oss-meta-sha256. The helper hashes what it actually sent, deletes the object
when that differs from the declared size or digest, and finally checks size and
digest with a signed HEAD. The dump never touches the worker's disk.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
from typing import BinaryIO

import httpx

from core.aliyun import AliyunCredentialsError
from core.oss import OssClient
from trajectory.ops.oss import OpsStorageError, describe_error, http_client, oss_client

BACKUP_PREFIX = "backups/"
SHA256_HEADER = "x-oss-meta-sha256"
CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BackupError(Exception):
    pass


def check_key(key: str) -> str:
    """Backups only: a wrong key must not be able to overwrite assets or trajectory data."""
    parts = key.split("/")
    if (
        not key.startswith(BACKUP_PREFIX)
        or len(key.encode()) > 1023
        or "\\" in key
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise BackupError(f"backup keys must be plain object names under {BACKUP_PREFIX}: {key!r}")
    return key


def _check_digest(sha256: str | None) -> None:
    if sha256 is not None and not _SHA256.match(sha256):
        raise BackupError("--sha256 must be 64 lowercase hex digits")


async def delete(oss: OssClient, http: httpx.AsyncClient, key: str, *, internal: bool = True) -> None:
    url = oss._presign("DELETE", check_key(key), 120, internal=internal)
    try:
        response = await http.delete(url)
    except httpx.HTTPError as exc:
        raise BackupError(f"DELETE {key} failed: {type(exc).__name__}") from None
    if response.status_code not in (200, 204, 404):
        raise BackupError(f"DELETE {key} failed: {describe_error(response)}")


async def verify(
    oss: OssClient, http: httpx.AsyncClient, key: str, size: int, *, sha256: str | None = None, internal: bool = True
) -> dict:
    """Size and recorded sha256 of a stored backup, checked with a signed HEAD."""
    _check_digest(sha256)
    url = oss._presign("HEAD", check_key(key), 300, internal=internal)
    try:
        response = await http.head(url)
    except httpx.HTTPError as exc:
        raise BackupError(f"HEAD {key} failed: {type(exc).__name__}") from None
    if response.status_code == 404:
        raise BackupError(f"{key} does not exist")
    if response.status_code != 200:
        raise BackupError(f"HEAD {key} failed: {describe_error(response)}")
    try:
        stored_size = int(response.headers.get("content-length", ""))
    except ValueError:
        raise BackupError(f"HEAD {key} returned no usable Content-Length") from None
    stored_sha256 = response.headers.get(SHA256_HEADER) or None
    if stored_size != size:
        raise BackupError(f"{key} has {stored_size} bytes, expected {size}")
    if sha256 and stored_sha256 != sha256:
        raise BackupError(f"{key} records sha256 {stored_sha256}, expected {sha256}")
    return {
        "bucket": oss.bucket,
        "key": key,
        "size_bytes": stored_size,
        "sha256": stored_sha256,
        "etag": response.headers.get("etag", "").strip('"'),
    }


async def upload(
    oss: OssClient,
    http: httpx.AsyncClient,
    key: str,
    source: BinaryIO,
    size: int,
    *,
    sha256: str | None = None,
    content_type: str = "application/octet-stream",
    internal: bool = True,
    expires_sec: int = 3600,
) -> dict:
    """Stream exactly ``size`` bytes of ``source`` to ``key``, then verify the stored object."""
    check_key(key)
    _check_digest(sha256)
    if size < 0:
        raise BackupError("--size must not be negative")
    signed_headers = {SHA256_HEADER: sha256} if sha256 else {}
    # Content-Type and x-oss-* headers are part of the V1 signature; the request
    # must send exactly these values.
    url = oss._presign(
        "PUT", key, expires_sec, content_type=content_type, internal=internal, canonical_headers=signed_headers
    )
    digest = hashlib.sha256()
    sent = 0

    async def body():
        nonlocal sent
        while sent < size:
            chunk = await asyncio.to_thread(source.read, min(CHUNK_BYTES, size - sent))
            if not chunk:
                return
            digest.update(chunk)
            sent += len(chunk)
            yield chunk

    # An explicit Content-Length keeps httpx from switching to chunked encoding.
    headers = {"Content-Type": content_type, "Content-Length": str(size), **signed_headers}
    try:
        response = await http.put(url, content=body(), headers=headers)
    except httpx.HTTPError as exc:
        raise BackupError(f"PUT {key} failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise BackupError(f"PUT {key} failed: {describe_error(response)}")
    trailing = await asyncio.to_thread(source.read, 1)
    problem = None
    if sent != size or trailing:
        problem = f"the input is {'longer' if trailing else 'shorter'} than the declared {size} bytes"
    elif sha256 and digest.hexdigest() != sha256:
        problem = f"the input has sha256 {digest.hexdigest()}, not the declared {sha256}"
    if problem:
        await delete(oss, http, key, internal=internal)
        raise BackupError(f"{key}: {problem}; the uploaded object was deleted")
    return await verify(oss, http, key, size, sha256=sha256, internal=internal)


def _parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--key", required=True, help=f"object key under {BACKUP_PREFIX}")
    shared.add_argument("--public", action="store_true", help="use the public host even if TRAJECTORY_OSS_INTERNAL is true")
    parser = argparse.ArgumentParser(prog="python -m trajectory.ops.backup", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    upload_command = commands.add_parser("upload", parents=[shared], help="stream stdin (or --file) to --key and verify it")
    upload_command.add_argument("--size", type=int, required=True)
    upload_command.add_argument("--sha256")
    upload_command.add_argument("--file", help="read this file instead of stdin")
    upload_command.add_argument("--content-type", default="application/octet-stream")
    verify_command = commands.add_parser("verify", parents=[shared], help="check the size (and sha256) of a stored backup")
    verify_command.add_argument("--size", type=int, required=True)
    verify_command.add_argument("--sha256")
    presign_command = commands.add_parser("presign-put", parents=[shared], help="print a presigned PUT URL for a manual upload")
    presign_command.add_argument("--content-type", default="application/octet-stream")
    presign_command.add_argument("--expires", type=int, default=3600)
    return parser


async def _run(args, oss: OssClient, internal: bool, stdin, transport) -> dict:
    async with http_client(transport, timeout=600.0) as http:
        if args.command == "verify":
            return await verify(oss, http, args.key, args.size, sha256=args.sha256, internal=internal)
        options = {"sha256": args.sha256, "content_type": args.content_type, "internal": internal}
        if args.file:
            with open(args.file, "rb") as source:
                return await upload(oss, http, args.key, source, args.size, **options)
        source = stdin if stdin is not None else sys.stdin.buffer
        return await upload(oss, http, args.key, source, args.size, **options)


def main(argv: list[str] | None = None, *, stdin=None, stdout=None, transport=None) -> int:
    args = _parser().parse_args(argv)
    stdout = sys.stdout if stdout is None else stdout
    try:
        oss, internal = oss_client()
        internal = internal and not args.public
        if args.command == "presign-put":
            url = oss.presign_put(check_key(args.key), args.content_type, args.expires, internal=internal)
            print(url, file=stdout)
            return 0
        result = asyncio.run(_run(args, oss, internal, stdin, transport))
    except (BackupError, OpsStorageError, AliyunCredentialsError, OSError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
