"""Presigned-URL client for the asset-transfer OSS bucket.

Files move browser→OSS and OSS→cloud desktop directly, so bytes never squeeze
through the backend or the sandbox tunnel — the backend only signs URLs and
keeps the upload records. Signing is hand-written OSS V1 (HMAC-SHA1 query
signature), the same ~40 lines the reference integration in bossip uses; a
whole SDK buys nothing here.

Signing trap (inherited from bossip, verified there the hard way): the
Content-Type line of the string-to-sign must match what the client actually
sends. PUT URLs are therefore signed WITH the declared mime and the uploader
must send exactly that header; GET/HEAD sign the line empty.

Server-side operations (put_object … list_objects) serve the trajectory
worker, which moves bytes itself. They sign the Authorization header with the
same V1 algorithm (a Date line where presigned URLs carry Expires), share one
pooled HTTP client that ignores proxy variables, and raise OssError for every
answer outside an operation's documented success cases.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import time
from email.utils import formatdate
from urllib.parse import quote, unquote
from xml.etree import ElementTree

from core.aliyun import AliyunCredentialsError, load_credentials
from core.config import get_config


class OssNotConfigured(Exception):
    pass


class OssError(Exception):
    """An OSS answer outside the operation's success cases.

    ``status`` is the HTTP status, 0 when no answer arrived (connection
    failure, timeout); ``code``, ``request_id`` and ``message`` come from the
    error XML when OSS sent one.
    """

    def __init__(self, status: int, code: str, request_id: str = "", message: str = ""):
        super().__init__(status, code, request_id, message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.message = message

    def __str__(self) -> str:
        text = f"OSS {self.status} {self.code or 'error'}"
        if self.message:
            text += f": {self.message}"
        if self.request_id:
            text += f" (request {self.request_id})"
        return text


#: Query parameters V1 signs as sub-resources of the canonical resource.
#: Everything else in a query string (list-type, prefix, max-keys,
#: encoding-type) travels unsigned.
_SIGNED_SUBRESOURCES = frozenset({
    "acl", "append", "asyncFetch", "bucketInfo", "callback", "callback-var", "cname",
    "comp", "continuation-token", "cors", "delete", "encryption", "endTime", "img",
    "inventory", "inventoryId", "lifecycle", "live", "location", "logging", "metaQuery",
    "objectMeta", "partNumber", "policy", "position", "qos", "qosInfo", "referer",
    "regionList", "replication", "replicationLocation", "replicationProgress",
    "requestPayment", "resourceGroup", "response-cache-control",
    "response-content-disposition", "response-content-encoding",
    "response-content-language", "response-content-type", "response-expires",
    "restore", "security-token", "sequential", "startTime", "stat", "status", "style",
    "styleName", "symlink", "tagging", "transferAcceleration", "uploadId", "uploads",
    "versionId", "versioning", "versions", "vod", "website", "worm", "wormExtend",
    "wormId", "x-oss-process", "x-oss-request-payer", "x-oss-traffic-limit",
})
#: Server-side timeouts in seconds. Transfers get a per-call read/write
#: budget; connecting and waiting for a pooled connection stay short so an
#: unreachable endpoint fails fast.
_CONNECT_TIMEOUT = 10.0
_POOL_TIMEOUT = 30.0
_CONTROL_TIMEOUT = 30.0
#: DeleteMultipleObjects accepts at most this many keys per request.
DELETE_BATCH_LIMIT = 1000
#: Credentials are re-read at most this often: a rotated secret file or STS
#: profile is picked up without a file read per request.
CREDENTIALS_TTL_SECONDS = 600.0
#: Environment that decides what load_credentials() returns (the cache key).
_CREDENTIAL_SELECTORS = (
    "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALICLOUD_ACCESS_KEY_ID",
    "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALICLOUD_ACCESS_KEY_SECRET",
    "ALIYUN_CLI_CONFIG", "ALIBABA_CLOUD_PROFILE", "HOME",
)

_clock = time.monotonic
_credentials: tuple[tuple, float, dict] | None = None
_http = None
_http_loop = None


def cached_credentials() -> dict:
    """load_credentials(), reused for CREDENTIALS_TTL_SECONDS.

    Keyed by the environment that selects the credentials, so a changed key or
    profile applies at once. Failures are not cached; the next call retries.
    """
    global _credentials
    selector = tuple(os.environ.get(name) for name in _CREDENTIAL_SELECTORS)
    now = _clock()
    cached = _credentials
    if cached is not None and cached[0] == selector and now - cached[1] < CREDENTIALS_TTL_SECONDS:
        return cached[2]
    creds = load_credentials()
    _credentials = (selector, now, creds)
    return creds


def clear_credentials_cache() -> None:
    global _credentials
    _credentials = None


def shared_http_client():
    """The pooled httpx.AsyncClient for server-side calls on the running loop.

    trust_env=False: OSS traffic (often the intranet endpoint) must never
    detour through HTTP(S)_PROXY. Connections belong to the loop that opened
    them, so another loop (a CLI's asyncio.run, a test) gets its own client.
    """
    import httpx

    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http.is_closed or _http_loop is not loop:
        _http = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(_CONTROL_TIMEOUT, connect=_CONNECT_TIMEOUT, pool=_POOL_TIMEOUT),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=30.0),
        )
        _http_loop = loop
    return _http


async def close_shared_http_client() -> None:
    """Close the shared client at shutdown; the next call opens a new one."""
    global _http, _http_loop
    client, loop = _http, _http_loop
    _http = _http_loop = None
    if client is not None and loop is asyncio.get_running_loop():
        await client.aclose()


class OssClient:
    def __init__(
        self,
        bucket: str,
        region: str,
        endpoint: str,
        key_id: str,
        key_secret: str,
        *,
        security_token: str | None = None,
        http=None,
    ):
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint
        self._key_id = key_id
        self._key_secret = key_secret
        # STS credentials: server-side calls send it as x-oss-security-token.
        self._security_token = security_token or None
        # An injected httpx.AsyncClient (tests); None uses shared_http_client().
        self._http = http

    @property
    def host(self) -> str:
        return f"{self.bucket}.{self.endpoint}"

    @property
    def internal_host(self) -> str:
        """Alibaba intranet host for cloud sandboxes in the bucket's region.

        OSS V1 signs the canonical bucket/key, not the hostname, so public and
        internal URLs carry the same signature. Custom/non-Alibaba endpoints
        deliberately fall back to their configured public host.
        """
        if self.region and self.endpoint.endswith(".aliyuncs.com"):
            return f"{self.bucket}.oss-{self.region}-internal.aliyuncs.com"
        return self.host

    def _sign(self, string_to_sign: str) -> str:
        digest = hmac.new(self._key_secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    def _presign(
        self,
        method: str,
        key: str,
        expires_sec: int,
        content_type: str = "",
        subresource: dict[str, str] | None = None,
        internal: bool = False,
        canonical_headers: dict[str, str] | None = None,
    ) -> str:
        expires = int(time.time()) + expires_sec
        sub = subresource or {}
        canonical = f"/{self.bucket}/{key}"
        if sub:
            canonical += "?" + "&".join(f"{k}={sub[k]}" for k in sorted(sub))
        # CanonicalizedOSSHeaders: sorted x-oss-* lines between the Expires
        # line and the canonical resource; the request must send them verbatim.
        headers = {k.lower(): v for k, v in (canonical_headers or {}).items()}
        header_lines = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        string_to_sign = f"{method}\n\n{content_type}\n{expires}\n{header_lines}{canonical}"
        signature = self._sign(string_to_sign)
        query = [f"{quote(k)}={quote(sub[k])}" for k in sorted(sub)]
        query += [
            f"OSSAccessKeyId={quote(self._key_id)}",
            f"Expires={expires}",
            f"Signature={quote(signature)}",
        ]
        encoded_key = quote(key, safe="/")
        host = self.internal_host if internal else self.host
        return f"https://{host}/{encoded_key}?{'&'.join(query)}"

    def presign_put(
        self,
        key: str,
        content_type: str,
        expires_sec: int = 1800,
        *,
        internal: bool = False,
    ) -> str:
        """Direct browser upload; the client must send exactly this Content-Type."""
        return self._presign(
            "PUT", key, expires_sec, content_type=content_type, internal=internal
        )

    def presign_get(
        self,
        key: str,
        expires_sec: int = 3600,
        download_name: str | None = None,
        *,
        internal: bool = False,
    ) -> str:
        sub = None
        if download_name:
            sub = {"response-content-disposition": f'attachment; filename="{download_name}"'}
        return self._presign("GET", key, expires_sec, subresource=sub, internal=internal)

    def presign_head(self, key: str, expires_sec: int = 120) -> str:
        return self._presign("HEAD", key, expires_sec)

    def presign_delete(self, key: str, expires_sec: int = 120) -> str:
        return self._presign("DELETE", key, expires_sec)

    async def head(self, key: str) -> dict | None:
        """Existence + size check after a client-side upload. None if absent."""
        import httpx

        url = self.presign_head(key)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.head(url)
        if resp.status_code == 200:
            return {
                "size": int(resp.headers.get("content-length", 0)),
                "mime": resp.headers.get("content-type", ""),
                # Content MD5 for single-PUT objects; multipart uploads get a
                # multipart ETag (still stable per object, so usable as an
                # identity token together with size).
                "etag": resp.headers.get("etag", "").strip('"'),
            }
        return None

    async def copy(self, src_key: str, dest_key: str) -> dict | None:
        """Server-side object copy within the bucket; head(dest) or None.

        Dedupe reuse path: bytes never leave OSS. Failure returns None so
        callers can fall back to a normal paid/streamed path — copy is an
        optimization, never a blocker.
        """
        import httpx

        copy_source = f"/{self.bucket}/{quote(src_key, safe='/')}"
        url = self._presign(
            "PUT", dest_key, 120, canonical_headers={"x-oss-copy-source": copy_source}
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.put(url, headers={"x-oss-copy-source": copy_source})
            if resp.status_code != 200:
                return None
        except Exception:
            return None
        return await self.head(dest_key)

    async def delete(self, key: str) -> bool:
        """Remove the object. OSS answers 204 whether or not it existed."""
        import httpx

        url = self.presign_delete(key)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(url)
        return resp.status_code in (200, 204)

    # -- Server-side operations: header-signed, shared pooled client --

    def _authorization(self, method: str, key: str, params: dict[str, str], headers: dict[str, str]) -> str:
        """``OSS AccessKeyId:Signature`` over VERB, Content-MD5, Content-Type,
        Date, the sorted x-oss-* headers and the canonical resource (with
        signed sub-resources, a bare name when the value is empty)."""
        lowered = {name.lower(): value.strip() for name, value in headers.items()}
        oss_headers = "".join(f"{name}:{lowered[name]}\n" for name in sorted(lowered) if name.startswith("x-oss-"))
        resource = f"/{self.bucket}/{key}"
        signed = sorted(name for name in params if name in _SIGNED_SUBRESOURCES)
        if signed:
            resource += "?" + "&".join(f"{name}={params[name]}" if params[name] else name for name in signed)
        string_to_sign = "\n".join((
            method, lowered.get("content-md5", ""), lowered.get("content-type", ""),
            lowered["date"], oss_headers + resource,
        ))
        return f"OSS {self._key_id}:{self._sign(string_to_sign)}"

    async def _request(
        self,
        method: str,
        key: str = "",
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        internal: bool = False,
        timeout: float = _CONTROL_TIMEOUT,
    ):
        """Send one header-signed request; key "" addresses the bucket itself.

        Failures without an HTTP answer become OssError with status 0.
        """
        import httpx

        params = params or {}
        headers = {**(headers or {}), "Date": formatdate(usegmt=True)}
        if self._security_token:
            headers["x-oss-security-token"] = self._security_token
        headers["Authorization"] = self._authorization(method, key, params, headers)
        host = self.internal_host if internal else self.host
        url = f"https://{host}/{quote(key, safe='/')}"
        if params:
            url += "?" + "&".join(
                f"{quote(name, safe='')}={quote(value, safe='')}" if value else quote(name, safe="")
                for name, value in params.items()
            )
        client = self._http or shared_http_client()
        try:
            return await client.request(
                method, url, content=body or None, headers=headers,
                timeout=httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT, pool=_POOL_TIMEOUT),
            )
        except httpx.HTTPError as exc:
            raise OssError(0, type(exc).__name__, "", str(exc)) from exc

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        forbid_overwrite: bool = False,
        internal: bool = False,
        timeout: float = 120,
    ) -> str:
        """Upload bytes in one PUT (OSS verifies Content-MD5); returns the ETag.

        forbid_overwrite sends x-oss-forbid-overwrite: an existing object is
        kept and the call still succeeds, which makes content-addressed writes
        idempotent. OSS does not report that object's ETag, so the result is "".
        """
        headers = {"Content-Type": content_type, "Content-MD5": _content_md5(data)}
        if forbid_overwrite:
            headers["x-oss-forbid-overwrite"] = "true"
        resp = await self._request(
            "PUT", _object_key(key), body=data, headers=headers, internal=internal, timeout=timeout
        )
        if resp.status_code == 200:
            return resp.headers.get("etag", "").strip('"')
        error = _error(resp)
        if forbid_overwrite and resp.status_code == 409 and error.code == "FileAlreadyExist":
            return ""
        raise error

    async def get_object(self, key: str, *, internal: bool = False, timeout: float = 120) -> bytes:
        """The whole object; FileNotFoundError when OSS answers 404."""
        resp = await self._request("GET", _object_key(key), internal=internal, timeout=timeout)
        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 404:
            raise FileNotFoundError(f"OSS object not found: {key}")
        raise _error(resp)

    async def head_object_info(self, key: str, *, internal: bool = False) -> dict | None:
        """size, mime, etag and last_modified of an object; None when absent."""
        resp = await self._request("HEAD", _object_key(key), internal=internal)
        if resp.status_code == 200:
            return {
                "size": int(resp.headers.get("content-length", 0)),
                "mime": resp.headers.get("content-type", ""),
                "etag": resp.headers.get("etag", "").strip('"'),
                "last_modified": resp.headers.get("last-modified", ""),
            }
        if resp.status_code == 404:
            return None
        raise _error(resp)

    async def delete_object_key(self, key: str, *, internal: bool = False) -> bool:
        """Delete one object. OSS answers 204 whether or not it existed, so
        True means gone; False only when OSS answered 404 (nothing to delete)."""
        resp = await self._request("DELETE", _object_key(key), internal=internal)
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        raise _error(resp)

    async def delete_objects(self, keys: list[str], *, internal: bool = False) -> int:
        """Delete keys with POST ?delete in quiet mode, DELETE_BATCH_LIMIT per
        request; returns how many keys were sent.

        Absent keys count as deleted: OSS deletes idempotently and quiet mode
        reports nothing per key. A failed request raises OssError; batches
        before it stay deleted.
        """
        keys = [_object_key(key) for key in keys]
        deleted = 0
        for start in range(0, len(keys), DELETE_BATCH_LIMIT):
            batch = keys[start:start + DELETE_BATCH_LIMIT]
            body = _delete_request_body(batch)
            headers = {"Content-Type": "application/xml", "Content-MD5": _content_md5(body)}
            resp = await self._request(
                "POST", params={"delete": ""}, body=body, headers=headers, internal=internal
            )
            if resp.status_code != 200:
                raise _error(resp)
            deleted += len(batch)
        return deleted

    async def list_objects(
        self,
        prefix: str,
        *,
        continuation_token: str | None = None,
        max_keys: int = 1000,
        internal: bool = False,
    ) -> tuple[list[dict], str | None]:
        """One ListObjectsV2 page of keys under prefix, in key order.

        Returns ([{key, size, etag, last_modified, storage_class}], token):
        token is None on the last page, else pass it back as continuation_token.
        """
        if not 1 <= max_keys <= 1000:
            raise ValueError("max_keys must be between 1 and 1000")
        params = {"list-type": "2", "max-keys": str(max_keys), "encoding-type": "url"}
        if prefix:
            params["prefix"] = prefix
        if continuation_token:
            params["continuation-token"] = continuation_token
        resp = await self._request("GET", params=params, internal=internal)
        if resp.status_code != 200:
            raise _error(resp)
        return _parse_listing(resp)


def _object_key(key: str) -> str:
    # An empty key addresses the bucket itself (PUT / creates a bucket,
    # DELETE / removes one); OSS object names never start with / or \.
    if not key or key[0] in "/\\":
        raise ValueError(f"Invalid OSS object key: {key!r}")
    return key


def _content_md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node, name: str) -> str:
    for child in node:
        if _local_name(child.tag) == name:
            return child.text or ""
    return ""


def _error(resp) -> OssError:
    """OssError from an error answer: its XML body or, for HEAD (no body),
    the base64 XML OSS repeats in the x-oss-err header."""
    body = resp.content
    if not body and resp.headers.get("x-oss-err"):
        try:
            body = base64.b64decode(resp.headers["x-oss-err"])
        except ValueError:
            body = b""
    try:
        root = ElementTree.fromstring(body) if body else None
    except ElementTree.ParseError:
        root = None
    code = message = request_id = ""
    if root is not None and _local_name(root.tag) == "Error":
        code = _child_text(root, "Code")
        message = _child_text(root, "Message")
        request_id = _child_text(root, "RequestId")
    elif body:
        message = body[:200].decode("utf-8", "replace")
    return OssError(resp.status_code, code, request_id or resp.headers.get("x-oss-request-id", ""), message)


def _delete_request_body(keys: list[str]) -> bytes:
    root = ElementTree.Element("Delete")
    ElementTree.SubElement(root, "Quiet").text = "true"
    for key in keys:
        ElementTree.SubElement(ElementTree.SubElement(root, "Object"), "Key").text = key
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8")


def _parse_listing(resp) -> tuple[list[dict], str | None]:
    request_id = resp.headers.get("x-oss-request-id", "")
    try:
        root = ElementTree.fromstring(resp.content)
        # encoding-type=url: OSS percent-encodes keys and the continuation token.
        decode = unquote if _child_text(root, "EncodingType") == "url" else str
        objects = [
            {
                "key": decode(_child_text(node, "Key")),
                "size": int(_child_text(node, "Size") or 0),
                "etag": _child_text(node, "ETag").strip('"'),
                "last_modified": _child_text(node, "LastModified"),
                "storage_class": _child_text(node, "StorageClass"),
            }
            for node in root
            if _local_name(node.tag) == "Contents"
        ]
    except (ElementTree.ParseError, ValueError) as exc:
        raise OssError(resp.status_code, "InvalidResponse", request_id, "Unparsable ListObjectsV2 answer") from exc
    if _child_text(root, "IsTruncated").strip().lower() != "true":
        return objects, None
    token = decode(_child_text(root, "NextContinuationToken"))
    if not token:
        # Returning None here would silently end the listing early.
        raise OssError(resp.status_code, "InvalidResponse", request_id, "Truncated listing without NextContinuationToken")
    return objects, token


def get_oss() -> OssClient:
    """The configured asset bucket, or raises OssNotConfigured (→ 503)."""
    config = get_config()
    bucket = config.oss_bucket
    if not bucket:
        raise OssNotConfigured("OSS_BUCKET is not set")
    region = config.oss_region
    endpoint = config.oss_endpoint or f"oss-{region}.aliyuncs.com"
    try:
        creds = cached_credentials()
    except AliyunCredentialsError as e:
        raise OssNotConfigured(str(e))
    return OssClient(
        bucket, region, endpoint, creds["access_key_id"], creds["access_key_secret"],
        security_token=creds.get("security_token"),
    )
