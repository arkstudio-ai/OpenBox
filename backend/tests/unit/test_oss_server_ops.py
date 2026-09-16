"""OSS server-side operations: V1 header signatures, request shapes, errors.

All traffic goes through httpx.MockTransport. Signatures are recomputed by
reference_signature, an independent implementation of the documented OSS V1
algorithm that works only from the request as it was sent.
"""
import asyncio
import base64
import hashlib
import hmac
import pickle
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, unquote
from xml.etree import ElementTree

import httpx
import pytest

import core.oss as oss
from core.aliyun import AliyunCredentialsError
from core.oss import OssClient, OssError, OssNotConfigured

KEY_ID = "test-key-id"
SECRET = "test-key-secret"
FIXED_DATE = "Mon, 14 Sep 2026 08:00:00 GMT"
PUBLIC_HOST = "bucket.oss-cn-shanghai.aliyuncs.com"
INTERNAL_HOST = "bucket.oss-cn-shanghai-internal.aliyuncs.com"

# Sub-resources that the OSS V1 signature documentation puts into the
# CanonicalizedResource (the object and bucket data subset). Every other
# query parameter stays out of the signature.
DOCUMENTED_SUBRESOURCES = {
    "acl", "uploads", "location", "cors", "logging", "website", "referer", "lifecycle",
    "delete", "append", "tagging", "objectMeta", "uploadId", "partNumber", "security-token",
    "position", "restore", "symlink", "versions", "versioning", "versionId",
    "continuation-token", "response-content-type", "response-content-language",
    "response-expires", "response-cache-control", "response-content-disposition",
    "response-content-encoding", "x-oss-process",
}


def reference_signature(request: httpx.Request, secret: str = SECRET) -> str:
    """Signature = base64(HMAC-SHA1(secret, VERB\\nContent-MD5\\nContent-Type\\nDate\\n
    CanonicalizedOSSHeaders + CanonicalizedResource))."""
    bucket = request.url.host.split(".", 1)[0]
    key = unquote(request.url.raw_path.decode("ascii").split("?", 1)[0])[1:]
    pairs = parse_qsl(request.url.query.decode("ascii"), keep_blank_values=True)
    signed = sorted((name, value) for name, value in pairs if name in DOCUMENTED_SUBRESOURCES)
    resource = f"/{bucket}/{key}"
    if signed:
        resource += "?" + "&".join(name if value == "" else f"{name}={value}" for name, value in signed)
    oss_headers = sorted(
        (name.lower(), value.strip()) for name, value in request.headers.items()
        if name.lower().startswith("x-oss-")
    )
    string_to_sign = "\n".join([
        request.method,
        request.headers.get("content-md5", ""),
        request.headers.get("content-type", ""),
        request.headers["date"],
        "".join(f"{name}:{value}\n" for name, value in oss_headers) + resource,
    ])
    return base64.b64encode(hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()


def assert_signed(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"OSS {KEY_ID}:{reference_signature(request)}"
    sent = parsedate_to_datetime(request.headers["date"])
    assert abs((datetime.now(timezone.utc) - sent).total_seconds()) < 300


class FakeOss:
    """MockTransport handler: records requests and replays queued answers."""

    def __init__(self, *answers):
        self.requests: list[httpx.Request] = []
        self.answers = list(answers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        return answer(request) if callable(answer) else answer


def client_for(handler, *, endpoint: str = "oss-cn-shanghai.aliyuncs.com", security_token: str | None = None) -> OssClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OssClient("bucket", "cn-shanghai", endpoint, KEY_ID, SECRET, security_token=security_token, http=http)


def error_body(code: str, message: str = "Denied by test", request_id: str = "req-body") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Error><Code>{code}</Code><Message>{message}</Message><RequestId>{request_id}</RequestId>"
        "<HostId>bucket.oss-cn-shanghai.aliyuncs.com</HostId></Error>"
    ).encode()


def listing(keys: list[str], *, token: str | None = None, encoded: bool = True) -> bytes:
    contents = "".join(
        f"<Contents><Key>{quote(key) if encoded else key}</Key>"
        f"<LastModified>2026-09-14T08:00:00.000Z</LastModified><ETag>\"ETAG-{index}\"</ETag>"
        f"<Type>Normal</Type><Size>{index * 10}</Size><StorageClass>Standard</StorageClass></Contents>"
        for index, key in enumerate(keys, 1)
    )
    next_token = ""
    if token:
        next_token = f"<NextContinuationToken>{quote(token, safe='') if encoded else token}</NextContinuationToken>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult><Name>bucket</Name><Prefix></Prefix>'
        f"<MaxKeys>1000</MaxKeys>{'<EncodingType>url</EncodingType>' if encoded else ''}"
        f"<IsTruncated>{'true' if token else 'false'}</IsTruncated>{next_token}"
        f"<KeyCount>{len(keys)}</KeyCount>{contents}</ListBucketResult>"
    ).encode()


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def deleted_keys(request: httpx.Request) -> list[str]:
    root = ElementTree.fromstring(request.content)
    assert root.tag == "Delete" and root.findtext("Quiet") == "true"
    return [node.findtext("Key") for node in root.findall("Object")]


# -- Signatures --

async def test_put_object_sends_md5_type_and_forbid_overwrite_under_a_valid_signature():
    fake = FakeOss(httpx.Response(200, headers={"ETag": '"5B3C1A2E"'}))
    etag = await client_for(fake).put_object(
        "trajectories/trj_1/blobs/abc", b'{"a":1}', content_type="application/json", forbid_overwrite=True
    )
    assert etag == "5B3C1A2E"
    [request] = fake.requests
    assert request.method == "PUT"
    assert request.url.scheme == "https" and request.url.host == PUBLIC_HOST
    assert request.url.raw_path == b"/trajectories/trj_1/blobs/abc"
    assert request.content == b'{"a":1}'
    assert request.headers["content-type"] == "application/json"
    assert request.headers["content-md5"] == md5_b64(b'{"a":1}')
    assert request.headers["x-oss-forbid-overwrite"] == "true"
    assert "x-oss-security-token" not in request.headers
    assert_signed(request)


async def test_string_to_sign_layout_of_each_request_kind(monkeypatch):
    monkeypatch.setattr(oss, "formatdate", lambda *args, **kwargs: FIXED_DATE)
    fake = FakeOss(httpx.Response(200), httpx.Response(200, content=listing([])), httpx.Response(200))
    client = client_for(fake, security_token="sts-token")
    signed = []
    real_sign = client._sign
    monkeypatch.setattr(client, "_sign", lambda text: signed.append(text) or real_sign(text))

    await client.put_object("dir/obj.json", b"{}", content_type="application/json", forbid_overwrite=True)
    await client.list_objects("dir/", continuation_token="tok+/=", max_keys=10)
    await client.delete_objects(["dir/obj.json"])

    delete_body = fake.requests[2].content
    assert signed == [
        f"PUT\n{md5_b64(b'{}')}\napplication/json\n{FIXED_DATE}\n"
        "x-oss-forbid-overwrite:true\nx-oss-security-token:sts-token\n/bucket/dir/obj.json",
        # list-type, prefix, max-keys and encoding-type are not sub-resources.
        f"GET\n\n\n{FIXED_DATE}\nx-oss-security-token:sts-token\n/bucket/?continuation-token=tok+/=",
        f"POST\n{md5_b64(delete_body)}\napplication/xml\n{FIXED_DATE}\n"
        "x-oss-security-token:sts-token\n/bucket/?delete",
    ]
    for request in fake.requests:
        assert request.headers["date"] == FIXED_DATE
        assert request.headers["x-oss-security-token"] == "sts-token"
        assert request.headers["authorization"] == f"OSS {KEY_ID}:{reference_signature(request)}"


async def test_signatures_match_vectors_computed_by_the_oss2_sdk(monkeypatch):
    # Authorization values produced by the V1 signer of the oss2 2.19.1 SDK
    # (ProviderAuth) for exactly these requests with Date fixed to FIXED_DATE:
    # a signer this module did not write. They also pin the request shapes
    # (headers, Content-MD5 of the delete body, query, key encoding).
    monkeypatch.setattr(oss, "formatdate", lambda *args, **kwargs: FIXED_DATE)
    fake = FakeOss(
        httpx.Response(200), httpx.Response(200, content=listing([])), httpx.Response(200), httpx.Response(200, content=b"x"),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    endpoint, key_id, secret = "oss-cn-shanghai.aliyuncs.com", "LTAI5tTestAccessKey", "TestAccessKeySecret0123456789abcd"
    plain = OssClient("openbox-traces", "cn-shanghai", endpoint, key_id, secret, http=http)
    sts = OssClient("openbox-traces", "cn-shanghai", endpoint, key_id, secret, security_token="CAIS+sts/token==", http=http)
    blob = "trajectories/trj_1/blobs/" + "a" * 64

    await plain.put_object(blob, b'{"a":1}', content_type="application/json", forbid_overwrite=True)
    await sts.list_objects("trajectories/trj_1/", continuation_token="ChR0+/=", max_keys=100)
    await plain.delete_objects([blob])
    await plain.get_object("dir/中文 key+%")

    assert [request.headers["authorization"] for request in fake.requests] == [
        "OSS LTAI5tTestAccessKey:Tp57c8MHTkjmetprUFvaRBSXtI8=",
        "OSS LTAI5tTestAccessKey:q9raD+YBairUX0R4HBlQG5PGvt4=",
        "OSS LTAI5tTestAccessKey:uZm4ciB+uoh3UqMji7uOl4vIN+E=",
        "OSS LTAI5tTestAccessKey:T0P/UpKQdbdk9U4QegwmnRDDXso=",
    ]
    assert fake.requests[1].url.raw_path == (
        b"/?list-type=2&max-keys=100&encoding-type=url&prefix=trajectories%2Ftrj_1%2F&continuation-token=ChR0%2B%2F%3D"
    )
    assert fake.requests[3].url.raw_path == b"/dir/%E4%B8%AD%E6%96%87%20key%2B%25"
    for request in fake.requests:
        assert request.url.host == "openbox-traces.oss-cn-shanghai.aliyuncs.com"


# -- put_object --

@pytest.mark.parametrize("wrap", [bytearray, memoryview])
async def test_put_object_accepts_bytes_like_data(wrap):
    fake = FakeOss(httpx.Response(200, headers={"ETag": '"E"'}))
    assert await client_for(fake).put_object("a/b.bin", wrap(b"\x00\x01payload"), internal=True) == "E"
    [request] = fake.requests
    assert request.method == "PUT" and request.url.host == INTERNAL_HOST and request.url.raw_path == b"/a/b.bin"
    assert request.content == b"\x00\x01payload"
    assert request.headers["content-md5"] == md5_b64(b"\x00\x01payload")
    assert_signed(request)


@pytest.mark.parametrize("data", [5, "text", None])
async def test_put_object_rejects_data_that_is_not_bytes_like(data):
    fake = FakeOss()
    with pytest.raises(TypeError):
        await client_for(fake).put_object("a/b", data)
    assert fake.requests == []


async def test_put_object_plain_write_uses_the_per_call_timeout():
    fake = FakeOss(httpx.Response(200, headers={"ETag": '"E"'}))
    assert await client_for(fake).put_object("a/b.bin", b"\x00\x01", timeout=5) == "E"
    [request] = fake.requests
    assert "x-oss-forbid-overwrite" not in request.headers
    assert request.headers["content-type"] == "application/octet-stream"
    assert request.extensions["timeout"] == {"connect": 10.0, "read": 5.0, "write": 5.0, "pool": 30.0}
    assert_signed(request)


async def test_forbid_overwrite_conflict_counts_as_success():
    fake = FakeOss(httpx.Response(409, content=error_body("FileAlreadyExists", "The object you specified already exists")))
    assert await client_for(fake).put_object("a/b", b"x", forbid_overwrite=True) == ""


@pytest.mark.parametrize("forbid_overwrite,code", [(False, "FileAlreadyExists"), (True, "OperationAborted")])
async def test_other_conflicts_raise(forbid_overwrite, code):
    fake = FakeOss(httpx.Response(409, content=error_body(code, request_id="req-409")))
    with pytest.raises(OssError) as caught:
        await client_for(fake).put_object("a/b", b"x", forbid_overwrite=forbid_overwrite)
    assert (caught.value.status, caught.value.code, caught.value.request_id) == (409, code, "req-409")


# -- put_object_file --

class StreamingOss(httpx.AsyncBaseTransport):
    """Receives each request body chunk by chunk, as a network transport does, then replays queued answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.chunks: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        async for chunk in request.stream:
            self.chunks.append(bytes(chunk))
        return self.answers.pop(0)


async def test_put_object_file_streams_the_file_under_its_md5_and_length(tmp_path):
    content = b"".join(index.to_bytes(4, "big") for index in range(70_000))
    path = tmp_path / "archive.zip"
    path.write_bytes(content)
    transport = StreamingOss(httpx.Response(200, headers={"ETag": '"F1LE"'}))
    client = OssClient("bucket", "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", KEY_ID, SECRET,
                       http=httpx.AsyncClient(transport=transport))
    etag = await client.put_object_file("trajectories/_exports/exp_1/archive.zip", path, content_type="application/zip",
                                        forbid_overwrite=True, internal=True, chunk_bytes=64 * 1024)
    assert etag == "F1LE"
    [request] = transport.requests
    assert request.method == "PUT" and request.url.host == INTERNAL_HOST
    assert request.url.raw_path == b"/trajectories/_exports/exp_1/archive.zip"
    assert b"".join(transport.chunks) == content
    assert len(transport.chunks) > 1 and max(len(chunk) for chunk in transport.chunks) <= 64 * 1024
    assert request.headers["content-md5"] == md5_b64(content)
    assert request.headers["content-length"] == str(len(content)) and "transfer-encoding" not in request.headers
    assert (request.headers["content-type"], request.headers["x-oss-forbid-overwrite"]) == ("application/zip", "true")
    assert_signed(request)


async def test_put_object_file_keeps_the_outcomes_of_put_object(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"zip")
    fake = FakeOss(httpx.Response(409, content=error_body("FileAlreadyExists")),
                   httpx.Response(403, content=error_body("AccessDenied", request_id="req-file")))
    client = client_for(fake)
    assert await client.put_object_file("a/b.zip", path, forbid_overwrite=True) == ""
    with pytest.raises(OssError) as caught:
        await client.put_object_file("a/b.zip", str(path))
    assert (caught.value.status, caught.value.code, caught.value.request_id) == (403, "AccessDenied", "req-file")
    assert [request.content for request in fake.requests] == [b"zip", b"zip"]
    assert fake.requests[1].headers["content-type"] == "application/octet-stream"
    assert "x-oss-forbid-overwrite" not in fake.requests[1].headers
    with pytest.raises(FileNotFoundError):
        await client.put_object_file("a/c.zip", tmp_path / "missing.zip")
    with pytest.raises(ValueError):
        await client.put_object_file("", path)
    assert len(fake.requests) == 2


# -- get_object --

async def test_get_object_encodes_the_key_and_uses_the_internal_host():
    fake = FakeOss(httpx.Response(200, content=b"stored bytes"))
    key = "trajectories/trj 1/blobs/a+b%c~d"
    assert await client_for(fake).get_object(key, internal=True) == b"stored bytes"
    [request] = fake.requests
    assert request.method == "GET" and request.url.host == INTERNAL_HOST
    assert request.url.raw_path == b"/trajectories/trj%201/blobs/a%2Bb%25c~d"
    assert request.content == b""
    assert "content-type" not in request.headers and "content-md5" not in request.headers
    assert request.extensions["timeout"]["read"] == 120
    assert_signed(request)


async def test_custom_endpoint_keeps_its_own_host_for_internal_calls():
    fake = FakeOss(httpx.Response(200, content=b"x"))
    await client_for(fake, endpoint="oss.example.test").get_object("a", internal=True)
    assert fake.requests[0].url.host == "bucket.oss.example.test"
    assert_signed(fake.requests[0])


async def test_get_object_missing_raises_file_not_found():
    fake = FakeOss(httpx.Response(404, content=error_body("NoSuchKey", "The specified key does not exist.")))
    with pytest.raises(FileNotFoundError, match="a/missing"):
        await client_for(fake).get_object("a/missing")


async def test_error_answers_carry_code_request_id_and_message():
    fake = FakeOss(httpx.Response(403, content=error_body("SignatureDoesNotMatch", "Signature mismatch", "req-403")))
    with pytest.raises(OssError) as caught:
        await client_for(fake).get_object("a/b")
    error = caught.value
    assert (error.status, error.code, error.request_id, error.message) == (
        403, "SignatureDoesNotMatch", "req-403", "Signature mismatch"
    )
    assert str(error) == "OSS 403 SignatureDoesNotMatch: Signature mismatch (request req-403)"
    copy = pickle.loads(pickle.dumps(error))
    assert (copy.status, copy.code, copy.request_id, copy.message) == (403, "SignatureDoesNotMatch", "req-403", "Signature mismatch")


async def test_non_xml_error_body_keeps_status_text_and_header_request_id():
    fake = FakeOss(httpx.Response(502, content=b"upstream connect error", headers={"x-oss-request-id": "req-hdr"}))
    with pytest.raises(OssError) as caught:
        await client_for(fake).get_object("a/b")
    assert (caught.value.status, caught.value.code, caught.value.request_id, caught.value.message) == (
        502, "", "req-hdr", "upstream connect error"
    )


# -- head_object_info / delete_object_key --

async def test_head_object_info_reports_metadata_or_none():
    fake = FakeOss(
        httpx.Response(200, headers={
            "Content-Length": "42", "Content-Type": "application/json", "ETag": '"ABC"',
            "Last-Modified": "Mon, 14 Sep 2026 08:00:00 GMT",
        }),
        httpx.Response(404),
    )
    client = client_for(fake)
    assert await client.head_object_info("a/b") == {
        "size": 42, "mime": "application/json", "etag": "ABC", "last_modified": "Mon, 14 Sep 2026 08:00:00 GMT",
    }
    assert await client.head_object_info("a/missing") is None
    assert [request.method for request in fake.requests] == ["HEAD", "HEAD"]
    for request in fake.requests:
        assert_signed(request)


async def test_head_error_is_decoded_from_the_x_oss_err_header():
    encoded = base64.b64encode(error_body("AccessDenied", "No permission", "")).decode()
    fake = FakeOss(httpx.Response(403, headers={"x-oss-err": encoded, "x-oss-request-id": "req-head"}))
    with pytest.raises(OssError) as caught:
        await client_for(fake).head_object_info("a/b")
    assert (caught.value.status, caught.value.code, caught.value.message, caught.value.request_id) == (
        403, "AccessDenied", "No permission", "req-head"
    )


async def test_delete_object_key_outcomes():
    fake = FakeOss(httpx.Response(204), httpx.Response(404), httpx.Response(403, content=error_body("AccessDenied")))
    client = client_for(fake)
    assert await client.delete_object_key("a/b") is True
    assert await client.delete_object_key("a/c") is False
    with pytest.raises(OssError, match="AccessDenied"):
        await client.delete_object_key("a/d", internal=True)
    assert [request.method for request in fake.requests] == ["DELETE"] * 3
    assert fake.requests[2].url.host == INTERNAL_HOST
    for request in fake.requests:
        assert_signed(request)


async def test_a_missing_bucket_is_an_error_not_a_missing_object():
    # OSS answers 404 NoSuchBucket for a wrong bucket name. Read as "no such
    # object" it would look like deleted content, a free key or a delete that
    # worked; it must fail loudly instead.
    missing = error_body("NoSuchBucket", "The specified bucket does not exist.", "req-nb")
    fake = FakeOss(
        httpx.Response(404, content=missing),
        httpx.Response(404, headers={"x-oss-err": base64.b64encode(missing).decode()}),
        httpx.Response(404, content=missing),
    )
    client = client_for(fake)
    for call in (
        lambda: client.get_object("a/b"),
        lambda: client.head_object_info("a/b"),
        lambda: client.delete_object_key("a/b"),
    ):
        with pytest.raises(OssError) as caught:
            await call()
        assert (caught.value.status, caught.value.code, caught.value.request_id) == (404, "NoSuchBucket", "req-nb")
    assert [(request.method, request.url.raw_path) for request in fake.requests] == [
        ("GET", b"/a/b"), ("HEAD", b"/a/b"), ("DELETE", b"/a/b"),
    ]
    for request in fake.requests:
        assert_signed(request)


async def test_a_coded_missing_object_keeps_the_documented_outcomes():
    no_key = error_body("NoSuchKey", "The specified key does not exist.")
    fake = FakeOss(
        httpx.Response(404, content=no_key),
        httpx.Response(404, headers={"x-oss-err": base64.b64encode(no_key).decode()}),
        httpx.Response(404, content=no_key),
    )
    client = client_for(fake)
    with pytest.raises(FileNotFoundError):
        await client.get_object("a/b")
    assert await client.head_object_info("a/b") is None
    assert await client.delete_object_key("a/b") is False


@pytest.mark.parametrize("key", ["", "/absolute", "\\windows"])
async def test_object_operations_refuse_bucket_level_keys(key):
    fake = FakeOss()
    client = client_for(fake)
    for call in (
        lambda: client.put_object(key, b"x"),
        lambda: client.get_object(key),
        lambda: client.head_object_info(key),
        lambda: client.delete_object_key(key),
    ):
        with pytest.raises(ValueError):
            await call()
    assert fake.requests == []


# -- delete_objects --

async def test_delete_objects_sends_quiet_batches_of_1000():
    keys = [f"trajectories/trj_1/blobs/{index:064x}" for index in range(2500)]
    fake = FakeOss(httpx.Response(200), httpx.Response(200), httpx.Response(200))
    assert await client_for(fake).delete_objects(keys, internal=True) == 2500
    assert [len(deleted_keys(request)) for request in fake.requests] == [1000, 1000, 500]
    assert [key for request in fake.requests for key in deleted_keys(request)] == keys
    for request in fake.requests:
        assert request.method == "POST" and request.url.host == INTERNAL_HOST
        assert request.url.raw_path == b"/?delete"
        assert request.headers["content-type"] == "application/xml"
        assert request.headers["content-md5"] == md5_b64(request.content)
        assert request.content.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
        assert_signed(request)


async def test_delete_objects_escapes_keys_and_skips_empty_input():
    fake = FakeOss(httpx.Response(200))
    client = client_for(fake)
    assert await client.delete_objects([]) == 0
    assert fake.requests == []
    odd = ["a/<b>&'c\"", "a/中文 key"]
    assert await client.delete_objects(odd) == 2
    assert deleted_keys(fake.requests[0]) == odd


async def test_delete_objects_stops_at_a_failed_batch_and_validates_keys_first():
    fake = FakeOss(httpx.Response(200), httpx.Response(403, content=error_body("AccessDenied")))
    client = client_for(fake)
    with pytest.raises(OssError) as caught:
        await client.delete_objects([f"k/{index}" for index in range(1500)])
    assert caught.value.code == "AccessDenied" and len(fake.requests) == 2
    with pytest.raises(ValueError):
        await client.delete_objects(["k/ok", ""])
    assert len(fake.requests) == 2


# -- list_objects --

async def test_list_objects_pages_with_a_signed_continuation_token():
    fake = FakeOss(
        httpx.Response(200, content=listing(["trajectories/trj_1/blobs/a b", "trajectories/trj_1/blobs/c"], token="tok+1/=")),
        httpx.Response(200, content=listing(["trajectories/trj_1/segments/x"])),
    )
    client = client_for(fake)
    objects, token = await client.list_objects("trajectories/trj_1/", max_keys=2)
    assert token == "tok+1/="
    assert objects == [
        {"key": "trajectories/trj_1/blobs/a b", "size": 10, "etag": "ETAG-1",
         "last_modified": "2026-09-14T08:00:00.000Z", "storage_class": "Standard"},
        {"key": "trajectories/trj_1/blobs/c", "size": 20, "etag": "ETAG-2",
         "last_modified": "2026-09-14T08:00:00.000Z", "storage_class": "Standard"},
    ]
    objects, token = await client.list_objects("trajectories/trj_1/", continuation_token=token, max_keys=2)
    assert token is None and [item["key"] for item in objects] == ["trajectories/trj_1/segments/x"]

    first, second = fake.requests
    assert first.method == "GET" and first.url.host == PUBLIC_HOST and first.url.path == "/"
    assert dict(parse_qsl(first.url.query.decode())) == {
        "list-type": "2", "max-keys": "2", "encoding-type": "url", "prefix": "trajectories/trj_1/",
    }
    assert b"continuation-token=tok%2B1%2F%3D" in second.url.query
    assert dict(parse_qsl(second.url.query.decode()))["continuation-token"] == "tok+1/="
    for request in fake.requests:
        assert_signed(request)


async def test_list_objects_without_url_encoding_and_with_an_empty_prefix():
    fake = FakeOss(httpx.Response(200, content=listing(["plain%41key"], encoded=False)))
    objects, token = await client_for(fake).list_objects("", internal=True)
    assert [item["key"] for item in objects] == ["plain%41key"] and token is None
    [request] = fake.requests
    query = dict(parse_qsl(request.url.query.decode()))
    assert "prefix" not in query and query["max-keys"] == "1000"
    assert request.url.host == INTERNAL_HOST
    assert_signed(request)


async def test_list_objects_tolerates_an_xml_namespace():
    body = listing(["a/b"]).replace(b"<ListBucketResult>", b'<ListBucketResult xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">')
    objects, token = await client_for(FakeOss(httpx.Response(200, content=body))).list_objects("a/")
    assert [item["key"] for item in objects] == ["a/b"] and token is None


@pytest.mark.parametrize("max_keys", [0, 1001])
async def test_list_objects_rejects_out_of_range_page_sizes(max_keys):
    fake = FakeOss()
    with pytest.raises(ValueError):
        await client_for(fake).list_objects("a/", max_keys=max_keys)
    assert fake.requests == []


@pytest.mark.parametrize("body,message", [
    (b"not xml", "Unparsable"),
    (b"<ListBucketResult><Contents><Key>a</Key><Size>big</Size></Contents></ListBucketResult>", "Unparsable"),
    (b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>", "NextContinuationToken"),
])
async def test_list_objects_rejects_malformed_answers(body, message):
    fake = FakeOss(httpx.Response(200, content=body, headers={"x-oss-request-id": "req-list"}))
    with pytest.raises(OssError, match=message) as caught:
        await client_for(fake).list_objects("a/")
    assert (caught.value.status, caught.value.code, caught.value.request_id) == (200, "InvalidResponse", "req-list")


async def test_list_objects_error_answer_raises():
    fake = FakeOss(httpx.Response(403, content=error_body("AccessDenied", request_id="req-l")))
    with pytest.raises(OssError) as caught:
        await client_for(fake).list_objects("a/")
    assert (caught.value.status, caught.value.code, caught.value.request_id) == (403, "AccessDenied", "req-l")


async def test_list_objects_starts_after_a_key_that_stays_out_of_the_signature():
    fake = FakeOss(httpx.Response(200, content=listing(["trajectories/trj_1/blobs/c"])))
    objects, token = await client_for(fake).list_objects(
        "trajectories/", start_after="trajectories/trj_1/blobs/a b", max_keys=5)
    assert [item["key"] for item in objects] == ["trajectories/trj_1/blobs/c"] and token is None
    [request] = fake.requests
    assert dict(parse_qsl(request.url.query.decode())) == {
        "list-type": "2", "max-keys": "5", "encoding-type": "url", "prefix": "trajectories/",
        "start-after": "trajectories/trj_1/blobs/a b",
    }
    assert b"start-after=trajectories%2Ftrj_1%2Fblobs%2Fa%20b" in request.url.query
    # Not a sub-resource: the reference signature leaves it out of the canonical resource.
    assert_signed(request)


# -- Transport --

@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failures_become_status_zero_errors(failure):
    def handler(request):
        raise failure("network down", request=request)

    with pytest.raises(OssError) as caught:
        await client_for(handler).get_object("a/b")
    assert (caught.value.status, caught.value.code) == (0, failure.__name__)
    assert isinstance(caught.value.__cause__, failure)


async def test_server_operations_default_to_the_shared_client(monkeypatch):
    fake = FakeOss(httpx.Response(204))
    shared = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(oss, "shared_http_client", lambda: shared)
    client = OssClient("bucket", "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", KEY_ID, SECRET)
    assert await client.delete_object_key("a/b") is True
    assert len(fake.requests) == 1
    assert_signed(fake.requests[0])


async def test_shared_http_client_pools_and_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    await oss.close_shared_http_client()
    client = oss.shared_http_client()
    try:
        assert oss.shared_http_client() is client
        assert client.trust_env is False and client.follow_redirects is False
        assert client.timeout == httpx.Timeout(30.0, connect=10.0, pool=30.0)
        pool = client._transport._pool
        assert (pool._max_connections, pool._max_keepalive_connections, pool._keepalive_expiry) == (64, 32, 30.0)
        assert client._mounts == {}
    finally:
        await oss.close_shared_http_client()
    assert client.is_closed
    replacement = oss.shared_http_client()
    assert replacement is not client
    await oss.close_shared_http_client()


def test_shared_http_client_is_bound_to_its_event_loop():
    async def current():
        return oss.shared_http_client()

    assert asyncio.run(current()) is not asyncio.run(current())


# -- Credentials --

@pytest.fixture
def credential_clock(monkeypatch):
    oss.clear_credentials_cache()
    now = [1000.0]
    monkeypatch.setattr(oss, "_clock", lambda: now[0])
    yield now
    oss.clear_credentials_cache()


def asset_config():
    return SimpleNamespace(oss_bucket="assets", oss_region="cn-shanghai", oss_endpoint="")


def test_credentials_are_reused_for_ten_minutes(monkeypatch, credential_clock):
    loads = []

    def load():
        loads.append(True)
        return {"access_key_id": f"ak-{len(loads)}", "access_key_secret": "sk"}

    monkeypatch.setattr(oss, "load_credentials", load)
    assert oss.cached_credentials()["access_key_id"] == "ak-1"
    credential_clock[0] += 599
    assert oss.cached_credentials()["access_key_id"] == "ak-1"
    credential_clock[0] += 1
    assert oss.cached_credentials()["access_key_id"] == "ak-2"
    monkeypatch.setenv("ALIBABA_CLOUD_PROFILE", "rotated")
    assert oss.cached_credentials()["access_key_id"] == "ak-3"
    assert len(loads) == 3


def test_cached_credentials_hand_out_copies(monkeypatch, credential_clock):
    loads = []

    def load():
        loads.append(True)
        return {"access_key_id": "ak", "access_key_secret": "sk", "security_token": "sts"}

    monkeypatch.setattr(oss, "load_credentials", load)
    handed_out = oss.cached_credentials()
    handed_out["access_key_secret"] = "tampered"
    handed_out.pop("security_token")
    assert oss.cached_credentials() == {"access_key_id": "ak", "access_key_secret": "sk", "security_token": "sts"}
    assert len(loads) == 1


def test_credential_failures_are_not_cached(monkeypatch, credential_clock):
    answers = [AliyunCredentialsError("no profile"), {"access_key_id": "ak", "access_key_secret": "sk"}]

    def load():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(oss, "load_credentials", load)
    with pytest.raises(AliyunCredentialsError):
        oss.cached_credentials()
    assert oss.cached_credentials()["access_key_id"] == "ak"


def test_get_oss_uses_cached_credentials_and_keeps_presigned_urls_unchanged(monkeypatch, credential_clock):
    monkeypatch.setattr(oss, "get_config", asset_config)
    loads = []

    def load():
        loads.append(True)
        return {"access_key_id": "ak", "access_key_secret": "sk", "security_token": "sts"}

    monkeypatch.setattr(oss, "load_credentials", load)
    first, second = oss.get_oss(), oss.get_oss()
    assert len(loads) == 1
    assert first.host == second.host == "assets.oss-cn-shanghai.aliyuncs.com"
    assert first._security_token == "sts"
    assert "security-token" not in first.presign_get("a.png")


def test_get_oss_maps_missing_credentials_to_not_configured(monkeypatch, credential_clock):
    monkeypatch.setattr(oss, "get_config", asset_config)

    def missing():
        raise AliyunCredentialsError("No Alibaba Cloud credentials")

    monkeypatch.setattr(oss, "load_credentials", missing)
    with pytest.raises(OssNotConfigured):
        oss.get_oss()
