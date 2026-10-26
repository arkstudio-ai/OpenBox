"""Canonical settings verify the actual write, not an unused generated story."""
from types import SimpleNamespace

import pytest

from memory.grounding import verify_memories
from wiki_compiler.hashing import canonical_hash


@pytest.mark.parametrize("field,value,expected", [
    ("language", "English", "language"), ("language", "", "language"),
    ("length", "brief", "length"), ("tone", "warm", "tone")])
async def test_only_the_exact_canonical_patch_is_sent_for_setting_verification(field, value, expected):
    proposal = {"summary": "UNSUPPORTED GENERATED BIOGRAPHY", "fact_key": "personal.style." + field,
        "setting": value, "source_indexes": [0]}
    frozen = SimpleNamespace(input_hash="original", sources=[{"body": "Original current user instruction"}])
    class Verifier:
        async def verify(self, items, **kwargs):
            assert items[0]["account_setting"] == {expected: value}
            assert "UNSUPPORTED GENERATED BIOGRAPHY" not in str(items)
            assert "Original current user instruction" in str(items)
            return [True], {}
    result, usage = await verify_memories(frozen, [proposal], SimpleNamespace(extract_model="test/model"), Verifier())
    assert result["supported"] == [canonical_hash(proposal)]
    assert usage["setting_verdicts"] == [{"fields": [expected], "supported": True}]


async def test_a_wrong_setting_is_not_rescued_by_a_correct_summary():
    proposal = {"summary": "用户要求讲德语", "fact_key": "personal.style.language", "setting": "English",
        "source_indexes": [0]}
    frozen = SimpleNamespace(input_hash="original", sources=[{"body": "以后请讲德语"}])
    class Verifier:
        async def verify(self, items, **kwargs):
            assert items[0]["account_setting"] == {"language": "English"}
            assert "English" in items[0]["claim"]
            return [False], {}
    result, usage = await verify_memories(frozen, [proposal], SimpleNamespace(extract_model="test/model"), Verifier())
    assert result["supported"] == []
    assert usage["setting_verdicts"][0]["supported"] is False


async def test_temporary_setting_cannot_become_an_expiry_free_account_default():
    proposal = {"summary": "这周用英语", "fact_key": "personal.style.language", "setting": "English",
        "valid_until": "2026-10-31", "source_indexes": [0]}
    frozen = SimpleNamespace(input_hash="original", sources=[{"body": "这周请用英语"}])
    class Verifier:
        async def verify(self, items, **kwargs):
            return [True], {}
    result, _ = await verify_memories(frozen, [proposal], SimpleNamespace(extract_model="test/model"), Verifier())
    assert result["supported"] == []


async def test_ordinary_memory_summary_still_requires_full_independent_verification():
    proposal = {"summary": "用户喜欢水彩", "fact_key": "personal.hobby", "source_indexes": [0]}
    frozen = SimpleNamespace(input_hash="original", sources=[{"body": "我爱画水彩"}])
    class Verifier:
        async def verify(self, items, **kwargs):
            assert items[0]["claim"] == proposal["summary"]
            assert "account_setting" not in items[0]
            return [True], {}
    result, _ = await verify_memories(frozen, [proposal], SimpleNamespace(extract_model="test/model"), Verifier())
    assert result["supported"] == [canonical_hash(proposal)]
