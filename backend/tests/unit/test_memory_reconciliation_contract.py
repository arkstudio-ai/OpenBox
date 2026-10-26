"""Closed model protocol preserves database identity and source proof indexes."""
from types import SimpleNamespace

import pytest

from core.config import MemoryConfig
from memory.providers.common import MemoryProviderError
from memory.reconciliation import MemoryReconciler, prepare_reconciliation
from wiki_compiler.hashing import canonical_hash


class Adapter:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    async def generate_data(self, **kwargs):
        self.request = kwargs
        return {"outcomes": self.outcomes}, {}


def existing():
    return {"mem_123456789012345678901234567890": {
        "id": "mem_123456789012345678901234567890", "revision": 3, "project_id": None,
        "summary": "周末散步，不逛商场", "asserted_at": "2026-10-24T00:00:00+00:00",
        "status": "ACTIVE", "confirmation_status": "CONFIRMED", "fact_key": "personal.hobby"}}


async def test_short_references_restore_identity_and_merge_joint_evidence():
    change = {"memory_id": "m0", "old": "周末散步", "new": "周末做烘焙"}
    adapter = Adapter([{"proposal_index": i, "action": "revise", "changes": [change]} for i in range(2)])
    result, _ = await MemoryReconciler(MemoryConfig(), adapter=adapter).plan(
        existing=existing(), proposals=[{}, {}], sources=[{"body": "原始话语"}], model="test/model")
    payload = adapter.request["data"]["existing_memories"][0]
    assert payload["id"] == "m0" and "revision" not in payload and "project_id" not in payload
    assert result == {"revisions": [{"memory_id": next(iter(existing())), "edits": [
        {"old": "周末散步", "new": "周末做烘焙", "proposal_indexes": [0, 1]}]}], "separate": []}
    assert adapter.request["schema"]["properties"]["outcomes"]["minItems"] == 2


@pytest.mark.parametrize("outcome", [
    {"proposal_index": True, "action": "unchanged", "changes": []},
    {"proposal_index": 0, "action": "revise", "changes": []},
    {"proposal_index": 0, "action": "separate", "changes": [{"memory_id": "m0", "old": "散步", "new": "烘焙"}]},
    {"proposal_index": 0, "action": "revise", "changes": [{"memory_id": "foreign-id", "old": "散步", "new": "烘焙"}]},
])
async def test_unknown_identity_and_ambiguous_disposition_fail_closed(outcome):
    with pytest.raises(MemoryProviderError):
        await MemoryReconciler(MemoryConfig(), adapter=Adapter([outcome])).plan(
            existing=existing(), proposals=[{}], sources=[], model="test/model")


async def test_unsupported_proposal_never_reaches_planner_and_proof_retains_original_index():
    proposals = [{"summary": "无原文支持的断言", "source_indexes": [0], "fact_key": "personal.hobby"},
                 {"summary": "周末做烘焙", "source_indexes": [0], "fact_key": "personal.hobby"}]
    rows = existing()
    frozen = SimpleNamespace(project_id=None, input_hash="frozen", existing_memories=list(rows.values()),
        sources=[{"body": "以后周末在家做烘焙，不去公园散步了", "occurred_at": "2026-10-25T00:00:00+00:00"}])
    adapter = Adapter([{"proposal_index": 0, "action": "revise", "changes": [
        {"memory_id": "m0", "old": "周末散步", "new": "周末做烘焙"}]}])
    class Verifier:
        async def verify(self, items, **kwargs):
            assert items[0]["claim"] == "周末做烘焙，不逛商场"
            assert items[0]["changes"] == ["周末做烘焙"]
            return [True], {}
    proof, _ = await prepare_reconciliation(frozen, proposals, {"supported": [canonical_hash(proposals[1])]},
        MemoryConfig(extract_model="test/model"), reconciler=MemoryReconciler(MemoryConfig(), adapter=adapter),
        verifier=Verifier())
    assert adapter.request["data"]["proposals"] == [proposals[1]]
    assert proof["plans"][0]["proposal_indexes"] == [1]
    assert proof["plans"][0]["edits"][0]["proposal_indexes"] == [1]
