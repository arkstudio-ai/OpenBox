"""Source speech acts must be eligible as well as factually grounded."""
import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.memory import UserMemory
from memory.grounding import GroundingVerifier, validate_verdicts
from memory.providers.common import MemoryProviderError


@pytest.mark.parametrize("intent", ["one_off_instruction", "quoted_material", "hypothetical", "question", "uncertain"])
def test_truth_alone_does_not_admit_one_off_or_quoted_content(intent):
    assert validate_verdicts({"verdicts": [{"index": 0, "supported": True, "intent": intent}]}, 1, memory=True) == [False]


@pytest.mark.parametrize("intent", ["enduring_assertion", "standing_instruction", "durable_project_decision"])
def test_enduring_claims_still_require_factual_support(intent):
    value = {"verdicts": [{"index": 0, "supported": True, "intent": intent}]}
    assert validate_verdicts(value, 1, memory=True) == [True]
    value["verdicts"][0]["supported"] = False
    assert validate_verdicts(value, 1, memory=True) == [False]


def test_missing_or_unknown_intent_cannot_admit_a_fact():
    for entry in ({"index": 0, "supported": True}, {"index": 0, "supported": True, "intent": "probably"}):
        with pytest.raises(MemoryProviderError):
            validate_verdicts({"verdicts": [entry]}, 1, memory=True)


@pytest.mark.parametrize("intent,expected", [
    ("one_off_instruction", False), ("uncertain", False),
    ("standing_instruction", True), ("durable_project_decision", True),
])
async def test_revision_rechecks_source_lifetime_not_just_truth(intent, expected):
    class Adapter:
        async def generate_data(self, **kwargs):
            assert kwargs["data"]["purpose"] == "memory_revision"
            return {"verdicts": [{"index": 0, "supported": True, "intent": intent}]}, {}
    verifier = GroundingVerifier(adapter=Adapter())
    result, _ = await verifier.verify([{"claim": "本条视频使用字幕", "previous_memory": "视频使用口播",
        "sources": ["这个视频不要口播，只留字幕"]}], purpose="memory_revision", model="test/model")
    assert result == [expected]


async def test_revision_missing_lifetime_verdict_fails_closed():
    class Adapter:
        async def generate_data(self, **kwargs):
            return {"verdicts": [{"index": 0, "supported": True}]}, {}
    with pytest.raises(MemoryProviderError, match="grounding_invalid_response"):
        await GroundingVerifier(adapter=Adapter()).verify([{"claim": "修改", "sources": ["仅本次"]}],
            purpose="memory_revision", model="test/model")


async def test_grounding_diagnostics_preserve_usage_and_separate_scope_rejections():
    class Adapter:
        async def generate_data(self, **kwargs):
            return {"verdicts": [
                {"index": 0, "supported": True, "intent": "durable_project_decision"},
                {"index": 1, "supported": True, "intent": "one_off_instruction"},
                {"index": 2, "supported": False, "intent": "durable_project_decision"},
            ]}, {"input_tokens": 110, "duration_ms": 40}
    result, usage = await GroundingVerifier(adapter=Adapter()).verify(
        [{"claim": "private claim", "sources": ["private evidence"]}] * 3,
        purpose="memory", model="test/model")
    assert result == [True, False, False]
    assert usage == {"input_tokens": 110, "duration_ms": 40,
        "grounding_policy_version": "source-scope-v4", "unsupported_count": 2,
        "intent_counts": {"durable_project_decision": 2, "one_off_instruction": 1}}


from tests.unit.test_memory_pipeline import pipeline_database, _seed, _finish_turn, _proposal  # noqa: E402,F401


async def test_independently_rejected_proposal_creates_no_pending_fact(monkeypatch):
    from core import config as runtime_config
    from memory.extraction import MemoryExtractionWorker
    from tests.unit.test_automatic_knowledge import Verifier
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    await _finish_turn(seed)
    assert await MemoryExtractionWorker(extractor=_proposal, verifier=Verifier(False)).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        assert not (await db.scalars(select(UserMemory).where(UserMemory.user_id == seed[0]))).all()


async def test_one_off_correction_cannot_overwrite_a_standing_fact(monkeypatch):
    from memory.extraction import MemoryExtractionWorker
    from tests.unit.test_memory_reconciliation import seed_correction, Planner, BEFORE
    from tests.unit.test_memory_pipeline import _job
    words = "本周五晚上临时加班，不能在家吃饭。"
    data, note, _ = await seed_correction(monkeypatch, text=words)
    def extract(frozen):
        return {"candidates": [{"type": "CONSTRAINT", "summary": "本周五晚上临时加班，不能在家吃饭。",
            "fact_key": "personal.weekly_schedule", "confidence": 95, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": words}]}]}
    class Adapter:
        async def generate_data(self, **kwargs):
            # Inject an erroneous first-stage admission. The independent
            # revision check must still keep a temporary exception out.
            intent = "one_off_instruction" if kwargs["data"]["purpose"] == "memory_revision" else "standing_instruction"
            return {"verdicts": [{"index": 0, "supported": True, "intent": intent}]}, {}
    worker = MemoryExtractionWorker(extractor=extract, verifier=GroundingVerifier(adapter=Adapter()),
        reconciler=Planner(note["id"]))
    assert await worker.run_once() == "RETRY"
    assert (await _job(data)).last_error == "memory_revision_not_verified"
    async with get_db_session() as db:
        row = await db.get(UserMemory, note["id"])
        assert row.revision == note["revision"] and row.value["summary"] == BEFORE
