"""Backup uploads (trajectory.ops.backup): OSS V1 presigned requests, streaming and verification."""
import base64
import hashlib
import hmac
import io
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from core.oss import OssClient
from trajectory.ops import backup
from trajectory.ops.oss import bucket_settings

BUCKET = "openbox-bucket"
KEY = "backups/postgres/20260915/openbox-20260914T193000Z.dump"
SECRET = "oss-secret"


def independent_oss_signature(verb: str, content_type: str, expires: str, oss_headers: dict[str, str], resource: str) -> str:
    """OSS V1 as documented: VERB, Content-MD5, Content-Type, Expires, sorted x-oss-* lines, resource."""
    header_lines = "".join(f"{name.lower()}:{value}\n" for name, value in sorted(oss_headers.items()))
    string_to_sign = f"{verb}\n\n{content_type}\n{expires}\n{header_lines}{resource}"
    return base64.b64encode(hmac.new(SECRET.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()


def assert_signed(request: httpx.Request, content_type: str = "", oss_headers: dict[str, str] | None = None) -> None:
    query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
    assert query["OSSAccessKeyId"] == "oss-key-id"
    expected = independent_oss_signature(
        request.method, content_type, query["Expires"], oss_headers or {}, f"/{BUCKET}/{KEY}"
    )
    assert query["Signature"] == expected


def client() -> OssClient:
    return OssClient(BUCKET, "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", "oss-key-id", SECRET)


class FakeBucket:
    """Records requests; PUT stores the body, HEAD reports it, DELETE removes it."""

    def __init__(self, *, put_status: int = 200, head_size: int | None = None):
        self.requests: list[httpx.Request] = []
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self.put_status = put_status
        self.head_size = head_size

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.lstrip("/")
        if request.method == "PUT":
            if self.put_status != 200:
                error = (
                    "<?xml version='1.0' encoding='UTF-8'?><Error><Code>SignatureDoesNotMatch</Code>"
                    "<Message>The request signature we calculated does not match.</Message>"
                    "<RequestId>REQ-1</RequestId></Error>"
                )
                return httpx.Response(self.put_status, content=error.encode())
            self.objects[key] = (request.content, request.headers.get(backup.SHA256_HEADER))
            return httpx.Response(200, headers={"ETag": '"etag-1"'})
        if request.method == "HEAD":
            if key not in self.objects:
                return httpx.Response(404)
            body, digest = self.objects[key]
            headers = {"Content-Length": str(self.head_size if self.head_size is not None else len(body)), "ETag": '"etag-1"'}
            if digest:
                headers[backup.SHA256_HEADER] = digest
            return httpx.Response(200, headers=headers)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


async def run_upload(bucket: FakeBucket, source: bytes, size: int, **options) -> dict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(bucket)) as http:
        return await backup.upload(client(), http, KEY, io.BytesIO(source), size, **options)


async def test_upload_streams_exact_bytes_with_signed_headers_and_verifies():
    data = bytes(range(256)) * 12_000  # about 2.9 MiB, several read chunks
    digest = hashlib.sha256(data).hexdigest()
    bucket = FakeBucket()

    result = await run_upload(bucket, data, len(data), sha256=digest)

    assert result == {"bucket": BUCKET, "key": KEY, "size_bytes": len(data), "sha256": digest, "etag": "etag-1"}
    assert bucket.methods() == ["PUT", "HEAD"]
    put, head = bucket.requests
    assert put.url.host == f"{BUCKET}.oss-cn-shanghai-internal.aliyuncs.com"
    assert put.url.path == f"/{KEY}"
    assert put.headers["content-type"] == "application/octet-stream"
    assert put.headers["content-length"] == str(len(data))
    assert "transfer-encoding" not in put.headers
    assert put.headers[backup.SHA256_HEADER] == digest
    assert put.content == data
    assert_signed(put, "application/octet-stream", {backup.SHA256_HEADER: digest})
    assert head.url.host == put.url.host
    assert_signed(head)


async def test_upload_without_digest_signs_only_the_content_type():
    bucket = FakeBucket()
    result = await run_upload(bucket, b"dump", 4, content_type="application/vnd.postgresql.dump")
    assert result["sha256"] is None
    put = bucket.requests[0]
    assert backup.SHA256_HEADER not in put.headers
    assert_signed(put, "application/vnd.postgresql.dump")


@pytest.mark.parametrize(
    ("source", "size", "digest", "problem"),
    [
        (b"short", 10, None, "shorter"),
        (b"longer than declared", 6, None, "longer"),
        (b"exact", 5, "0" * 64, "sha256"),
    ],
)
async def test_mismatched_input_deletes_the_uploaded_object(source, size, digest, problem):
    bucket = FakeBucket()
    with pytest.raises(backup.BackupError, match=problem):
        await run_upload(bucket, source, size, sha256=digest)
    assert bucket.methods() == ["PUT", "DELETE"]
    assert len(bucket.requests[0].content) == min(len(source), size)
    assert_signed(bucket.requests[1])
    assert bucket.objects == {}


async def test_oss_errors_name_code_and_request_without_the_signed_url():
    bucket = FakeBucket(put_status=403)
    with pytest.raises(backup.BackupError) as raised:
        await run_upload(bucket, b"dump", 4)
    message = str(raised.value)
    assert "HTTP 403 SignatureDoesNotMatch (request REQ-1)" in message
    assert "Signature=" not in message and SECRET not in message


async def test_dumps_above_the_single_put_limit_are_refused_before_any_request():
    bucket = FakeBucket()
    with pytest.raises(backup.BackupError, match="one OSS PUT stores"):
        await run_upload(bucket, b"", backup.MAX_PUT_BYTES + 1)
    assert bucket.requests == []
    assert backup.MAX_PUT_BYTES == 5 * 1024**3


async def test_verify_rejects_a_stored_size_that_differs():
    bucket = FakeBucket(head_size=3)
    with pytest.raises(backup.BackupError, match="has 3 bytes, expected 4"):
        await run_upload(bucket, b"dump", 4)


async def test_verify_reports_a_missing_object():
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeBucket())) as http:
        with pytest.raises(backup.BackupError, match="does not exist"):
            await backup.verify(client(), http, KEY, 4)


@pytest.mark.parametrize(
    "key",
    ["assets/user/file.png", "trajectories/trj_1/blobs/x", "backups/../assets/x", "backups//x", "backups/x/", "/backups/x",
     "backups/./x", "backups\\x"],
)
def test_keys_outside_backups_are_refused(key):
    with pytest.raises(backup.BackupError):
        backup.check_key(key)


def test_bucket_settings_follow_the_trajectory_variables():
    assert bucket_settings({"OSS_BUCKET": "assets", "OSS_REGION": "cn-shanghai", "OSS_ENDPOINT": "cdn.example.com"}) == {
        "bucket": "assets", "region": "cn-shanghai", "endpoint": "oss-cn-shanghai.aliyuncs.com", "internal": True,
    }
    assert bucket_settings({
        "OSS_BUCKET": "assets", "TRAJECTORY_OSS_BUCKET": "traces", "TRAJECTORY_OSS_REGION": "cn-hangzhou",
        "TRAJECTORY_OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com", "TRAJECTORY_OSS_INTERNAL": "false",
    }) == {"bucket": "traces", "region": "cn-hangzhou", "endpoint": "oss-cn-hangzhou.aliyuncs.com", "internal": False}


@pytest.fixture
def bucket_environment(monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "oss-key-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", SECRET)
    monkeypatch.setenv("OSS_BUCKET", BUCKET)
    monkeypatch.setenv("OSS_REGION", "cn-shanghai")
    for name in ("TRAJECTORY_OSS_BUCKET", "TRAJECTORY_OSS_REGION", "TRAJECTORY_OSS_ENDPOINT", "TRAJECTORY_OSS_INTERNAL"):
        monkeypatch.delenv(name, raising=False)


def test_cli_upload_reads_stdin_and_prints_the_verified_object(bucket_environment):
    data = b"PGDMP custom archive"
    bucket = FakeBucket()
    stdout = io.StringIO()
    code = backup.main(
        ["upload", "--key", KEY, "--size", str(len(data)), "--sha256", hashlib.sha256(data).hexdigest()],
        stdin=io.BytesIO(data), stdout=stdout, transport=httpx.MockTransport(bucket),
    )
    assert code == 0
    assert json.loads(stdout.getvalue())["size_bytes"] == len(data)
    assert bucket.objects[KEY][0] == data


def test_cli_upload_from_a_file_over_the_public_host(bucket_environment, tmp_path):
    dump = tmp_path / "openbox.dump"
    dump.write_bytes(b"x" * 100)
    bucket = FakeBucket()
    code = backup.main(
        ["upload", "--key", KEY, "--size", "100", "--file", str(dump), "--public"],
        stdout=io.StringIO(), transport=httpx.MockTransport(bucket),
    )
    assert code == 0
    assert bucket.requests[0].url.host == f"{BUCKET}.oss-cn-shanghai.aliyuncs.com"


def test_cli_failures_exit_1(bucket_environment, monkeypatch, capsys):
    assert backup.main(["verify", "--key", KEY, "--size", "4"], transport=httpx.MockTransport(FakeBucket())) == 1
    assert "does not exist" in capsys.readouterr().err
    assert backup.main(["upload", "--key", "assets/x", "--size", "1"], stdin=io.BytesIO(b"x")) == 1
    monkeypatch.delenv("OSS_BUCKET")
    assert backup.main(["verify", "--key", KEY, "--size", "4"]) == 1
    assert "OSS_BUCKET" in capsys.readouterr().err


def test_cli_presign_put_prints_an_internal_url(bucket_environment):
    stdout = io.StringIO()
    assert backup.main(["presign-put", "--key", KEY, "--expires", "600"], stdout=stdout) == 0
    url = httpx.URL(stdout.getvalue().strip())
    assert url.host == f"{BUCKET}.oss-cn-shanghai-internal.aliyuncs.com" and url.path == f"/{KEY}"
