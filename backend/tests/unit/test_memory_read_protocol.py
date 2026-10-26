"""Turn privacy, evidence freshness and whole-item budgets over real SQL."""
import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace
import json

import pytest

from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.session import Session
from memory import packet, readers, service
from memory.policy import MemoryAccessDenied
from memory.read_contract import check
from tests.unit.test_memory_write_protocol import setup
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


def allowed():
    return {"model": "jev-test", "usage": {}, "answers": {
        "read_policy": {"choice": "allowed", "confidence": 1, "probabilities": {"allowed": 1}},
        "memory_needed": {"choice": "retrieve", "confidence": 1},
        "task_needed": {"choice": "skip", "confidence": 1},
        **{name: {"noul": 0.0} for name in ("history_useful", "knowledge_useful", "assets_useful", "experience_useful")}}}


async def context(monkeypatch):
    owner, _, workspace, main, _ = await setup(monkeypatch)
    config = OpenBoxConfig(model="test/model", memory={"semantic_recall": True,
        "v2_write": True, "retrieval_v2": True, "route_jev": True, "automatic_knowledge": False,
        "recall_deadline_seconds": .15})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    turn = packet.TurnContext(owner, workspace, "按我的习惯安排一下", session_id=main.id,
        include_all_projects=True, entry="assistant")
    return config.memory, turn


async def test_asset_supplement_is_json_safe_and_revalidated_against_sql(monkeypatch):
    from datetime import datetime, timezone
    from core.identifier import ascending
    from db.models.file_asset import FileAsset

    config, original = await context(monkeypatch)
    turn = replace(original, read_policy="allowed")
    identity = ascending("asset")
    async with get_db_session() as db:
        db.add(FileAsset(id=identity, user_id=turn.user_id, workspace_id=turn.workspace_id,
            session_id=turn.session_id, name="店内照片.jpg", mime="image/jpeg",
            oss_key=f"fixture/{identity}", size=5, status="ready", source="user",
            transient=False, is_deleted=False, description="水果陈列台", description_state="ready",
            created_at=datetime.now(timezone.utc), description_updated_at=datetime.now(timezone.utc)))

    request = packet.ReadRequest(kind="assets", query="店内")
    result = await readers.read_request(request, turn, config)
    # This is the real serialization boundary used between model calls.
    stored = json.loads(json.dumps({"status": "complete", "results": [result]}))
    assert [item["id"] for item in stored["results"][0]["items"]] == [identity]
    assert stored["results"][0]["items"][0]["created_at"].endswith("+00:00")
    args = {"reads": [asdict(request)]}
    assert await readers.revalidate_result(args, stored, turn, config) == stored

    async with get_db_session() as db:
        (await db.get(FileAsset, identity)).description = "用户补充：橱窗外景"
    assert await readers.revalidate_result(args, stored, turn, config) == {
        "status": "unavailable", "reason": "evidence_changed", "items": []}


@pytest.mark.parametrize("entry", ["chat", "voice"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_packet_state_batches_fresh_settings_clock_and_revocations(monkeypatch, entry, authorized):
    from sqlalchemy import event
    from assistant.profile import save
    from memory.versions import version, revocation_epoch
    _, original = await context(monkeypatch)
    turn = replace(original, entry=entry)
    async with get_db_session() as db:
        first = await packet._packet_state(db, turn)
    note = await service.create_note(user_id=turn.user_id, workspace_id=turn.workspace_id,
                                    summary="用户喜欢在安静的地方画画。")
    await save(turn.user_id, {"language": "English"})
    await service.forget_memory(user_id=turn.user_id, workspace_id=turn.workspace_id,
                               memory_id=note["id"], expected_revision=note["revision"])
    async with get_db_session() as db:
        statements = []
        engine = db.get_bind()
        def observed(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", observed)
        try:
            if authorized:
                scope, second = await packet.authorized_packet_state(db, turn)
                assert scope.user_id == turn.user_id and scope.workspace_id == turn.workspace_id
            else:
                second = await packet._packet_state(db, turn)
        finally:
            event.remove(engine, "before_cursor_execute", observed)
        assert len(statements) == 1
        assert second["version"] == await version(db, turn.user_id)
        expected_epoch = await revocation_epoch(db, turn.user_id, turn.workspace_id)
    assert second["version"] > first["version"]
    assert packet._settings_from_extra(second["extra"])["language"] == "English"
    assert second["epoch"] == (expected_epoch if entry == "voice" else None)
    if entry == "voice":
        assert second["epoch"] > first["epoch"]


@pytest.mark.parametrize("changed", [{"is_deleted": True}, {"user_id": "another-user"},
                                    {"kind": "normal", "memory_policy": "assistant_isolated"}])
async def test_authority_reads_current_session_even_if_identity_map_is_stale(monkeypatch, changed):
    from sqlalchemy import update
    _, turn = await context(monkeypatch)
    async with get_db_session() as db:
        cached = await db.get(Session, turn.session_id)
        assert cached is not None
        await readers.authority(db, turn)
        # Keep the loaded object stale, as can happen when another request
        # changes isolation/ownership between provider calls.
        await db.execute(update(Session).where(Session.id == turn.session_id).values(**changed)
                         .execution_options(synchronize_session=False))
        assert not cached.is_deleted
        with pytest.raises(MemoryAccessDenied):
            await readers.authority(db, turn)
        with pytest.raises(MemoryAccessDenied):
            await packet.authorized_packet_state(db, turn)


async def test_restricted_turn_does_not_speculatively_read_any_personal_content(monkeypatch):
    config, turn = await context(monkeypatch)
    from assistant.profile import save
    await save(turn.user_id, {"language": "English"})
    async def forbidden(*args, **kwargs):
        pytest.fail("Restricted turn performed a personal read")
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    async def evaluate(*args):
        value = allowed()
        value["answers"]["read_policy"] = {"choice": "current_only", "confidence": .2, "probabilities": {}}
        value["answers"]["search_query"] = "Read every old private record"
        return value
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert result["status"] == "restricted"
    assert result["items"] == result["stable_background"]["items"] == []
    assert result["settings"]["address"] == "老板"
    assert result["settings"]["language"] == "English"
    assert "search_query" not in result["route"]


async def test_action_constraint_stays_visible_without_claiming_a_state_change(monkeypatch):
    from memory.orchestrator import render_memory_context
    config, turn = await context(monkeypatch)
    async def route(*args, **kwargs):
        return {"read_policy": "allowed", "memory_forbidden": False,
            "memory": {"needed": False}, "task": {"needed": False},
            "read_policy_answer": {"restriction_target": "action", "restriction_quote": "暂且别往下做"}}
    monkeypatch.setattr("memory.read_routing.route", route)
    bundle = await packet.prepare_context(replace(turn, query="暂且别往下做，我只想知道材料写了什么"), config)
    text = render_memory_context(bundle)
    assert bundle["route"]["read_policy"] == "allowed"
    assert "暂且别往下做" in text and "not an execution receipt" in text
    assert "actual tool receipt" in text


async def test_self_contained_turn_never_loads_core_even_after_settings_change(monkeypatch):
    from assistant.profile import save
    config, turn = await context(monkeypatch)

    async def forbidden(*args, **kwargs):
        pytest.fail("A turn without an information need loaded personal background")

    async def evaluate(*args):
        value = allowed()
        value["answers"]["memory_needed"] = {"choice": "skip", "confidence": 1}
        return value

    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    bundle = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert bundle["route"]["read_policy"] == "allowed"
    assert bundle["stable_background"]["items"] == bundle["items"] == []
    await save(turn.user_id, {"language": "English"})
    async with get_db_session() as db:
        scope = await readers.authority(db, turn)
    refreshed = await packet.refresh_packet(bundle, scope, config)
    assert refreshed["context_version"] > bundle["context_version"]
    assert refreshed["settings"]["language"] == "English"
    assert refreshed["stable_background"]["items"] == refreshed["items"] == []


async def test_deadline_cancels_pending_retrieval_and_does_not_claim_no_memory(monkeypatch):
    config, turn = await context(monkeypatch)
    cancelled = asyncio.Event()
    async def slow(**kwargs):
        kwargs["diagnostics"].update(stage="final_check", timings={"authorize": 90})
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr("memory.retrieval.search_memory", slow)
    async def evaluate(*args):
        return allowed()
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert cancelled.is_set()
    assert "recall_deadline" in result["degraded_reasons"]
    assert result["status"] in {"partial", "unavailable"}
    assert result["known_gaps"]
    assert result["retrieval_diagnostics"] == {"stage": "final_check", "timings": {"authorize": 90}}


@pytest.mark.parametrize("expire", [False, True])
async def test_checkpoint_is_reauthorized_before_a_partial_packet_is_used(monkeypatch, expire):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from db.models.memory import UserMemory
    from memory.retrieval import authorized_documents
    from memory.presentation import document_item
    from memory.policy import resolve_access_scope
    config, turn = await context(monkeypatch)
    config.recall_deadline_seconds = .2
    row = await service.create_note(user_id=turn.user_id, workspace_id=turn.workspace_id,
        summary="装模型时先分类零件")
    async def recall(**kwargs):
        async with get_db_session() as db:
            access = await resolve_access_scope(db, user_id=turn.user_id, workspace_id=turn.workspace_id)
            docs = await authorized_documents(db, access, config, only={("memory", row["id"])})
        kwargs["on_checkpoint"]({"items": [document_item(doc) for doc in docs]})
        if expire:
            async with get_db_session() as db:
                await db.execute(update(UserMemory).where(UserMemory.id == row["id"]).values(
                    ttl=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await asyncio.Event().wait()
    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    async def evaluate(*args):
        return allowed()
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert result["status"] == "partial" and "recall_deadline" in result["degraded_reasons"]
    assert result["known_gaps"]
    assert [item["id"] for item in result["items"]] == ([] if expire else [row["id"]])


async def test_permission_wait_does_not_erase_the_retrieval_budget(monkeypatch):
    config, turn = await context(monkeypatch)
    config.recall_deadline_seconds = .08
    config.read_router_timeout_seconds = .5
    decided = False
    budgets = []

    async def evaluate(*args):
        nonlocal decided
        await asyncio.sleep(.1)  # Longer than the subsequent lookup's own budget.
        decided = True
        return allowed()

    async def recall(**kwargs):
        assert decided  # Nothing is read while the restriction is unresolved.
        budgets.append(kwargs["io_budget_seconds"])
        return {"items": []}

    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert len(budgets) == 1
    assert result["route"]["read_policy"] == "allowed"
    assert "recall_deadline" not in result["degraded_reasons"]
    assert result["phase_timings"]["route_ms"] >= 90
    assert result["phase_timings"]["recall_ms"] < result["phase_timings"]["route_ms"]


async def test_router_deadline_cancels_decision_and_never_reads_persistent_content(monkeypatch):
    from memory.orchestrator import render_memory_context
    config, turn = await context(monkeypatch)
    config.read_router_timeout_seconds = .02
    cancelled = asyncio.Event()

    async def evaluate(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def forbidden(*args, **kwargs):
        pytest.fail("Timed-out policy decision allowed a persistent read")

    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert cancelled.is_set()
    assert result["status"] == "unavailable" and result["route"]["read_policy"] == "uncertain"
    assert "route_deadline" in result["degraded_reasons"]
    assert "recall_ms" not in result["phase_timings"]
    rendered = render_memory_context(result)
    assert "Briefly tell the user this" in rendered
    assert "Do not present that suggestion as personalized" in rendered
    assert result["items"] == result["stable_background"]["items"] == []


async def test_wrong_model_protocol_does_not_trigger_network_or_memory_fallback(monkeypatch):
    config, turn = await context(monkeypatch)
    config.read_router_model = "openai/jev-latest"

    def forbidden(*args, **kwargs):
        pytest.fail("A protocol configuration error must not trigger a request or personal read")

    monkeypatch.setattr("memory.providers.configured_read.shared_client", forbidden)
    monkeypatch.setattr("memory.providers.jev.shared_client", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    result = await packet.prepare_context(turn, config)
    assert result["status"] == "unavailable"
    assert result["route"]["reason_code"] == "read_router_protocol_mismatch"
    assert result["route"]["read_policy"] == "uncertain"
    assert result["items"] == result["stable_background"]["items"] == []


async def test_time_scope_is_bound_to_original_turn_for_initial_and_followup_search(monkeypatch):
    config, turn = await context(monkeypatch)
    turn = replace(turn, query="今天想看一下上周聊过的安排", recent_context=(
        {"role": "user", "text": "2026-10-01 有个约会"},))
    intent = {"choice": "source_time", "expression": "上周", "confidence": .99}
    searches = []

    async def evaluate(*args):
        value = allowed()
        value["answers"]["time_intent"] = intent
        value["answers"]["search_query"] = "2026-01-01 安排"
        return value

    async def search(**kwargs):
        searches.append(kwargs)
        return {"items": [], "time_context": {"expression": "上周", "hard_filter_applied": True}}

    monkeypatch.setattr("memory.retrieval.search_memory", search)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert result["_turn"].time_intent == result["route"]["time_intent"] == intent
    followup = await readers.read_request(packet.ReadRequest(query="2026-01-01 的安排"), result["_turn"], config)
    assert len(searches) == 2
    assert all(item["time_query"] == turn.query and item["time_intent"] == intent for item in searches)
    assert turn.query in searches[0]["query"] and "2026-01-01 安排" in searches[0]["query"]
    assert followup["time_context"]["expression"] == "上周"


async def test_slow_optional_provider_leaves_time_for_authority_check(monkeypatch):
    config, turn = await context(monkeypatch)
    config.recall_deadline_seconds = 1.0
    provider_cancelled = asyncio.Event()
    checked = False

    async def provider():
        try:
            await asyncio.Event().wait()
        finally:
            provider_cancelled.set()

    async def recall(**kwargs):
        nonlocal checked
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(kwargs["io_budget_seconds"]):
                await provider()
        # Simulate the final SQL authority/revision read after provider timeout.
        # It must finish inside the same original overall retrieval deadline.
        await asyncio.sleep(.22)
        checked = True
        return {"items": [], "degraded_reasons": ["rerank_deadline"],
                "timings": {"final_check": 220}}

    async def evaluate(*_):
        return allowed()

    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert provider_cancelled.is_set() and checked
    assert "rerank_deadline" in result["degraded_reasons"]
    assert "recall_deadline" not in result["degraded_reasons"]
    assert result["timings"]["final_check"] == 220


async def test_same_turn_refresh_rejects_deleted_consumer_session(monkeypatch):
    config, turn = await context(monkeypatch)
    async with get_db_session() as db:
        (await db.get(Session, turn.session_id)).is_deleted = True
    with pytest.raises(MemoryAccessDenied):
        await readers.read_request(packet.ReadRequest(kind="documents"), turn, config)


async def test_semantic_catalog_skip_opens_no_database_transaction(monkeypatch):
    config, turn = await context(monkeypatch)
    def unexpected_database():
        pytest.fail("An empty catalogue opened an unrelated SQL transaction")
    monkeypatch.setattr(readers, "get_db_session", unexpected_database)
    assert await readers.catalogs(turn, {}, config) == []


async def test_asset_catalog_still_checks_current_consumer_authority(monkeypatch):
    config, turn = await context(monkeypatch)
    async with get_db_session() as db:
        (await db.get(Session, turn.session_id)).is_deleted = True
    with pytest.raises(MemoryAccessDenied):
        await readers.catalogs(turn, {"assets_useful": 1}, config)


async def test_live_task_reads_are_allowed_when_history_is_forbidden_and_share_read_budget(monkeypatch):
    config, turn = await context(monkeypatch)
    ctx = SimpleNamespace(user_id=turn.user_id, memory_turn=replace(turn, read_policy="no_history"),
        memory_read_budget=packet.ReadBudget())
    assert check(ctx, "history") is not None
    assert check(ctx, "task_state") is None
    ctx.memory_turn = replace(turn, read_policy="allowed")
    assert check(ctx, "search", consume=True) is None
    assert check(ctx, "sources", consume=True) is None
    assert check(ctx, "history", consume=True)["status"] == "budget_exhausted"
    assert (await packet.read_more([packet.ReadRequest()], ctx.memory_turn, ctx.memory_read_budget, config))["status"] == "budget_exhausted"


async def test_legacy_current_run_memory_read_is_revalidated_without_another_model_call(monkeypatch):
    from assistant import memory
    from assistant.projection import current_observation
    # An empty result is still a read capability, not a way around restrictions.
    config, turn = await context(monkeypatch)
    monkeypatch.setattr(memory, "get_config", lambda: SimpleNamespace(memory=config))
    ctx = SimpleNamespace(user_id=turn.user_id, workspace_id=turn.workspace_id, session_id=turn.session_id,
        memory_turn=replace(turn, read_policy="allowed"))
    part = {"type": "tool", "tool": "memory.search", "input": {"query": "anything"},
        "output": json.dumps({"items": []})}
    assert await current_observation(part, ctx)
    ctx.memory_turn = replace(turn, read_policy="current_only")
    assert not await current_observation(part, ctx)


def test_packet_discards_whole_evidence_and_preserves_complete_filenames():
    from memory.orchestrator import render_memory_context
    config = OpenBoxConfig().memory
    filename = "2026年华东门店水果视频拍摄要求与报价（最终确认版本）.pdf"
    bundle = {"_semantic_packet": True, "settings": {"address": "老板"}, "route": {"read_policy": "allowed"},
        "items": [], "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": [],
        "catalogs": [{"kind": "asset", "id": str(i), "name": filename, "description": "视频要求" * 100} for i in range(20)]}
    packet.apply_budget(bundle, config)
    rendered = render_memory_context(bundle)
    assert packet.tokens(rendered) <= config.recall_token_budget
    assert len(bundle["catalogs"]) <= config.recall_item_limit
    assert all(row["name"] == filename for row in bundle["catalogs"])


def test_requested_asset_descriptions_survive_generic_recall_and_task_overview_pressure():
    from memory.orchestrator import render_memory_context
    config = OpenBoxConfig().memory
    assets = [{"kind": "asset", "id": "newer", "name": "IMG_123.jpg", "description": "室内陈列，备用素材", "description_state": "ready"},
              {"kind": "asset", "id": "older", "name": "IMG_123.jpg", "description": "人工选定的宣传照片，保留原图", "description_state": "manual"}]
    bundle = {"_semantic_packet": True, "settings": {"address": "老板"},
        "route": {"read_policy": "allowed", "needs": {"assets_useful": .9}},
        "items": [{"kind": "source", "id": str(i), "revision": 1, "text": "普通知识内容。" * 90} for i in range(5)],
        "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": [],
        "task_state": {"sessions": [{"title": "其他任务的完整说明" * 600}]}, "catalogs": list(assets)}
    assert packet.tokens(render_memory_context(bundle)) > config.recall_token_budget
    packet.apply_budget(bundle, config)
    assert bundle["catalogs"] == assets
    assert "task_state" not in bundle
    assert packet.tokens(render_memory_context(bundle)) <= config.recall_token_budget
    assert "task_state_requires_exact_read" in bundle["degraded_reasons"]
    assert bundle["known_gaps"]  # Lower-priority omissions are visible, not reported as complete recall.


@pytest.mark.parametrize("knowledge_hint", [None, 0.0, .3, .95])
def test_ranked_document_survives_asset_catalog_even_when_route_underestimates_knowledge(knowledge_hint):
    from memory.orchestrator import render_memory_context
    config = OpenBoxConfig().memory
    source = {"kind": "source", "id": "camera-guide", "revision": 4,
        "text": "Use a vertical frame. Total duration 24 seconds; each shot at most 6 seconds."}
    needs = {"assets_useful": .9}
    if knowledge_hint is not None:
        needs["knowledge_useful"] = knowledge_hint
    bundle = {"_semantic_packet": True, "settings": {"address": "老板"},
        "route": {"read_policy": "allowed", "needs": needs},
        "items": [source], "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": [],
        "catalogs": [{"kind": "asset", "id": str(i), "name": f"photo-{i}.jpg",
                      "description": "An unrelated shelf and assorted products. " * 70} for i in range(4)]}
    assert packet.tokens(render_memory_context(bundle)) > config.recall_token_budget
    packet.apply_budget(bundle, config)
    assert source in bundle["items"]
    assert bundle["catalogs"] and len(bundle["catalogs"]) < 4
    assert bundle["items"][0]["text"] == source["text"]
    assert packet.tokens(render_memory_context(bundle)) <= config.recall_token_budget
    assert "catalog_budget" in bundle["degraded_reasons"]


def test_self_contained_lookup_excludes_old_topic_but_reference_lookup_keeps_context():
    turn = packet.TurnContext("u", "w", "店里拍摄资料的镜头长度要求是什么？", recent_context=(
        {"role": "user", "text": "我喜欢爬山"}, {"role": "assistant", "text": "可以找安静的山林"}))
    assert packet.contextual_query(turn, {"recent_context_useful": 0}) == turn.query
    referential = replace(turn, query="那个地方下午开放吗？")
    resolved = json.loads(packet.contextual_query(referential, {"recent_context_useful": .95}))
    assert resolved["current_request"] == referential.query
    assert resolved["recent_exchange_for_references_only"][0]["text"] == "我喜欢爬山"


async def test_semantic_context_selection_reaches_retrieval_after_permission(monkeypatch):
    config, turn = await context(monkeypatch)
    turn = replace(turn, recent_context=({"role": "user", "text": "unrelated old subject"},))
    queries = []

    async def evaluate(*args):
        result = allowed()
        result["answers"]["recent_context_useful"] = {"noul": 0}
        return result

    async def recall(**kwargs):
        queries.append(kwargs["query"])
        return {"items": []}

    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert result["route"]["recent_context_useful"] == 0
    assert queries == [turn.query]


async def test_semantic_search_hint_preserves_current_input_and_uses_same_decision(monkeypatch):
    config, turn = await context(monkeypatch)
    queries, decisions = [], []
    turn = replace(turn, query="一个小时不出门，帮我安排一下", recent_context=(
        {"role": "user", "text": "unrelated old subject"},))

    async def evaluate(state, _config):
        decisions.append(state)
        assert "unrelated old subject" not in json.dumps(state)
        value = allowed()
        value["answers"].update(search_query="用户兴趣爱好 在家喜欢的休闲活动",
                                  recent_context_useful={"noul": 0})
        return value

    async def recall(**kwargs):
        queries.append(kwargs)
        return {"items": []}

    monkeypatch.setattr("memory.retrieval.search_memory", recall)
    result = await packet.prepare_context(turn, config, evaluator=evaluate)
    assert len(decisions) == len(queries) == 1
    assert queries[0]["query"] == turn.query + "\n用户兴趣爱好 在家喜欢的休闲活动"
    assert queries[0]["time_query"] == turn.query
    assert result["route"]["read_policy"] == "allowed"
    assert result["_turn"].explicit_read is False


async def test_read_plan_cache_tracks_its_actual_input_but_not_hidden_history(monkeypatch):
    from memory import read_routing
    from memory.policy import resolve_access_scope
    config, turn = await context(monkeypatch)
    # Use the actual OrderedDict contract without inheriting another test's cache.
    from collections import OrderedDict
    monkeypatch.setattr(read_routing, "_cache", OrderedDict())
    decisions = []

    async def evaluate(state, _config):
        decisions.append(state)
        value = allowed()
        value["answers"]["recent_context_useful"] = {"noul": 1}
        return value

    monkeypatch.setattr("memory.providers.configured_read.evaluate", evaluate)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=turn.user_id, workspace_id=turn.workspace_id)
    old = ({"role": "user", "text": "first private subject"},)
    new = ({"role": "user", "text": "second private subject"},)
    first = await read_routing.route(turn.query, scope, config, recent_context=old)
    second = await read_routing.route(turn.query, scope, config, recent_context=new)
    assert first["called"] and second["cache"] == "hit" and not second["called"]
    assert len(decisions) == 1
    assert "private subject" not in json.dumps(decisions)
    rebuilt = json.loads(packet.contextual_query(replace(turn, recent_context=new), second))
    assert rebuilt["recent_exchange_for_references_only"] == list(new)
    third = await read_routing.route(turn.query, scope, config, recent_context=())
    assert third["called"] and len(decisions) == 2
    from memory.versions import changed
    async with get_db_session() as db:
        await changed(db, turn.user_id)
    fourth = await read_routing.route(turn.query, scope, config, recent_context=new)
    assert fourth["called"] and len(decisions) == 3
    visible = await read_routing.route(turn.query, scope, config, recent_context=new,
        visible_conversation_available=True)
    assert visible["called"] and len(decisions) == 4
    assert decisions[-1]["visible_conversation_available"] is True
    same_view = await read_routing.route(turn.query, scope, config, recent_context=new,
        visible_conversation_available=True)
    assert same_view["cache"] == "hit" and len(decisions) == 4


def test_maximum_unicode_persona_stays_whole_without_inflating_core():
    from memory.orchestrator import render_memory_context
    config = OpenBoxConfig().memory
    persona = "𠮷" * 300
    bundle = {"_semantic_packet": True, "settings": {"address": "林姐", "persona": persona},
        "route": {"read_policy": "allowed"}, "items": [], "stable_background": {"items": []},
        "known_gaps": [], "degraded_reasons": []}
    packet.apply_budget(bundle, config)
    assert bundle["budget"]["core_tokens"] <= config.core_token_budget
    assert packet.tokens(render_memory_context(bundle)) <= config.recall_token_budget
    assert bundle["settings_details"]["persona"] == persona
