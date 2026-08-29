"""OSS server-side copy signing: canonical x-oss-* headers enter the signature."""
from core.oss import OssClient


def _client():
    return OssClient("bucket", "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", "ak", "sk")


def test_copy_presign_includes_canonical_header_line(monkeypatch):
    client = _client()
    captured = {}

    def spy_sign(string_to_sign):
        captured["sts"] = string_to_sign
        return "sig"

    monkeypatch.setattr(client, "_sign", spy_sign)
    url = client._presign(
        "PUT", "dest/key.mp4", 120,
        canonical_headers={"x-oss-copy-source": "/bucket/src/key.mp4"},
    )
    lines = captured["sts"].split("\n")
    # VERB, Content-MD5(empty), Content-Type(empty), Expires, header line, resource
    assert lines[0] == "PUT"
    assert lines[4] == "x-oss-copy-source:/bucket/src/key.mp4"
    assert lines[5] == "/bucket/dest/key.mp4"
    assert "Signature=sig" in url


def test_presign_without_headers_is_unchanged(monkeypatch):
    client = _client()
    captured = {}
    monkeypatch.setattr(client, "_sign", lambda sts: captured.setdefault("sts", sts) and "s" or "s")
    client._presign("GET", "a/b.png", 60)
    assert captured["sts"] == f"GET\n\n\n{captured['sts'].split(chr(10))[3]}\n/bucket/a/b.png"
