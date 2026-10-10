"""Credentials and identity, card or contact numbers are never remembered and never cost the rest of a turn."""
import json as jsonlib

import httpx
import pytest

from core import config as runtime_config
from memory import service
from memory.extraction import MemoryExtractionWorker, validate_proposals
from memory.grounding import GroundingVerifier
from memory.jobs import ExtractionInput
from memory.redaction import mask_sensitive, sensitive_kind
from memory.wiki.provider import ConfiguredWikiModel
from tests.unit.test_memory_pipeline import (  # noqa: F401
    _finish_turn,
    _seed,
    pipeline_database,
)
from wiki_compiler.hashing import canonical_hash

SAID = "我对花生过敏，有急事可以打我电话13800138000。"


@pytest.mark.parametrize("text, kind", [
    ("用户的身份证号是110101199003071234。", "id_number"),
    ("用户的工资卡号是6222020200112233445。", "card_number"),
    ("信用卡 4111 1111 1111 1111", "card_number"),
    ("用户的手机号是13800138000。", "phone"),
    ("固话 010-12345678", "phone"),
    ("我的邮箱是test@example.com", "email"),
    ("家住在北京市朝阳区某某路88号3单元502", "address"),
    ("我的wifi密码: abc12345", "credential"),
    ("用户住在北京市朝阳区。", None),
    ("用户家是3室2厅。", None),
    ("用户的公司在某某路88号。", None),
    ("用户对花生过敏。", None),
])
def test_sensitive_kinds(text, kind):
    assert sensitive_kind(text) == kind
    if kind not in {None, "address"}:
        assert "redacted" in mask_sensitive(text)


def test_masking_keeps_the_rest_of_the_text_whole():
    masked = mask_sensitive(SAID * 3)
    assert "13800138000" not in masked and masked.count("我对花生过敏") == 3 and masked.endswith("。")


def candidate(summary, quote):
    return {"type": "CONSTRAINT", "summary": summary, "fact_key": None, "confidence": 90,
            "source_indexes": [0], "quotes": [{"source_index": 0, "quote": quote}]}


def test_only_the_sensitive_candidate_is_dropped():
    frozen = ExtractionInput(job_id="job", user_id="user", workspace_id="ws", project_id=None, session_id="s",
        logical_turn_id="turn", input_hash=canonical_hash(SAID), acl_hash="acl", existing_memories=(),
        base_revisions=(), sources=({"source_kind": "user_statement", "body": SAID,
                                     "occurred_at": "2026-10-03T00:00:00+00:00"},))
    kept = validate_proposals({"candidates": [candidate("用户对花生过敏。", "我对花生过敏"),
        candidate("用户的紧急联系电话是13800138000。", "打我电话13800138000")]}, frozen)
    assert [item["summary"] for item in kept] == ["用户对花生过敏。"]


class VerifyingClient:
    """A provider that sees the masked request and supports every claim in it."""

    def __init__(self):
        self.sent = []

    async def post(self, _url, *, json, headers, timeout):
        content = json["messages"][1]["content"]
        self.sent.append(content)
        items = jsonlib.loads(content)["items"]
        verdicts = [{"index": index, "supported": True} for index in range(len(items))]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": jsonlib.dumps({"verdicts": verdicts})}}], "usage": {}})


def provider(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs",
                        lambda _model: {"api_key": "synthetic-test-key", "api_base": "https://memory.invalid/v1"})
    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _model: False)
    return VerifyingClient()


@pytest.mark.asyncio
async def test_provider_masks_sensitive_values_instead_of_refusing(monkeypatch):
    client = provider(monkeypatch)
    value, _ = await ConfiguredWikiModel(client=client).generate_data(model="test/model",
        data={"purpose": "memory", "items": [{"claim": "用户对花生过敏。", "sources": [SAID]}]},
        sources=[], system="verify")
    assert value == {"verdicts": [{"index": 0, "supported": True}]}
    assert "13800138000" not in client.sent[0] and "花生" in client.sent[0]


@pytest.mark.asyncio
async def test_a_phone_number_no_longer_costs_the_other_facts_in_the_message(monkeypatch):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    client = provider(monkeypatch)
    await _finish_turn(seed, text=SAID)

    def extractor(frozen):
        return {"candidates": [candidate("用户对花生过敏。", "我对花生过敏"),
                               candidate("用户的紧急联系电话是13800138000。", "打我电话13800138000")]}
    verifier = GroundingVerifier(adapter=ConfiguredWikiModel(client=client))
    assert await MemoryExtractionWorker(extractor=extractor, verifier=verifier).run_once() == "SUCCEEDED"
    active = await service.list_active_memories(user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
    assert [item["summary"] for item in active] == ["用户对花生过敏。"]
    assert client.sent and all("13800138000" not in request for request in client.sent)


@pytest.mark.asyncio
async def test_people_cannot_save_or_edit_in_sensitive_details_either(monkeypatch):
    seed = await _seed(monkeypatch)
    identity = {"user_id": seed[0], "workspace_id": seed[1]}
    with pytest.raises(service.MemorySensitiveContent) as caught:
        await service.create_note(**identity, project_id=seed[2], summary="我的身份证号是110101199003071234")
    assert caught.value.kind == "id_number"
    note = await service.create_note(**identity, project_id=seed[2], summary="我住在北京市朝阳区")
    with pytest.raises(service.MemorySensitiveContent):
        await service.edit_note(**identity, memory_id=note["id"], expected_revision=note["revision"],
                                summary="我住在北京市朝阳区某某路88号3单元502")
