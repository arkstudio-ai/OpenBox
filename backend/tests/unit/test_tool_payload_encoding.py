"""Cold, corrupt and partially populated caches never trigger a download."""
import base64
import hashlib

import pytest

from agent import tool_payload
from core import local_tokenizer


@pytest.fixture(autouse=True)
def isolated_encoding(monkeypatch):
    import tiktoken
    import tiktoken.load

    def forbidden(*args, **kwargs):
        raise AssertionError("Runtime must not enter a download-capable loader")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(tiktoken.load, "read_file_cached", forbidden)
    tool_payload._proxy_encoding.cache_clear()
    yield
    tool_payload._proxy_encoding.cache_clear()


def local_fixture(monkeypatch, directory):
    # Complete single-byte alphabet, sufficient to construct a real Encoding
    # without shipping the production vocabulary or using the network in CI.
    data = b"".join(base64.b64encode(bytes([rank])) + f" {rank}\n".encode()
                    for rank in range(256))
    monkeypatch.setattr(local_tokenizer, "VOCABULARY_SHA256", hashlib.sha256(data).hexdigest())
    path = directory / local_tokenizer.VOCABULARY_CACHE_KEY
    path.write_bytes(data)
    return path


def test_encoding_is_skipped_without_a_populated_cache(monkeypatch, tmp_path):
    import tempfile

    tool_payload._proxy_encoding.cache_clear()
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
    # tiktoken's own default cache lives under the temp dir; point it somewhere empty.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "empty-tmp"))
    assert tool_payload._proxy_encoding() is None
    tool_payload._proxy_encoding.cache_clear()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    assert tool_payload._proxy_encoding() is None
    # The estimate still answers.
    assert tool_payload.proxy_token_count("hello world") > 0
    tool_payload._proxy_encoding.cache_clear()


def test_only_unrelated_cache_entries_do_not_trigger_a_download(monkeypatch, tmp_path):
    (tmp_path / "another-vocabulary").write_bytes(b"x")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    assert tool_payload._proxy_encoding() is None


def test_corrupt_vocabulary_is_preserved_and_falls_back(monkeypatch, tmp_path):
    path = tmp_path / local_tokenizer.VOCABULARY_CACHE_KEY
    path.write_bytes(b"damaged vocabulary")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    assert tool_payload._proxy_encoding() is None
    assert tool_payload.proxy_token_count("中文用户约束") >= 6
    assert path.read_bytes() == b"damaged vocabulary"


@pytest.mark.parametrize("configured", ["", "missing"])
def test_explicit_cache_choice_never_falls_through(monkeypatch, tmp_path, configured):
    local_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "" if not configured else str(tmp_path / configured))
    assert tool_payload._proxy_encoding() is None


def test_local_encoding_remains_usable_after_cache_disappears(monkeypatch, tmp_path):
    path = local_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    encoding = tool_payload._proxy_encoding()
    path.unlink()  # Only this test's disposable fixture.
    text = "中文 hello 123 😀"
    assert encoding.decode(encoding.encode(text)) == text
    assert tool_payload.proxy_token_count(text) == len(text.encode())


def test_local_contract_matches_the_locked_vendor_constructor(monkeypatch, tmp_path):
    from tiktoken_ext import openai_public

    local_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    ranks = {bytes([rank]): rank for rank in range(256)}
    calls = []

    def pinned_bytes(url, expected_hash):
        calls.append((url, expected_hash))
        return ranks

    monkeypatch.setattr(openai_public, "load_tiktoken_bpe", pinned_bytes)
    expected = openai_public.o200k_base()
    encoding = tool_payload._proxy_encoding()
    assert encoding._pat_str == expected["pat_str"]
    assert encoding._special_tokens == expected["special_tokens"]
    assert calls == [(local_tokenizer.VOCABULARY_URL,
                      "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d")]
