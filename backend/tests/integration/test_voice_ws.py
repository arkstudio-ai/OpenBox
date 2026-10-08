"""/ws/assistant/voice end to end with a scripted provider: handshake, refusals, a turn, hang-up."""
import json
import os
import secrets
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.testclient import TestClient

from tests.support.voice_fakes import ASK_MARKER, ScriptedProvider

os.environ["JWT_SECRET"] = "test-voice-secret-32bytes-long!!"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

FRAME = b"\x00\x00" * 1600
_cache = None


@pytest.fixture(scope="module")
async def app(tmp_path_factory):
    global _cache
    from cache.memory_cache import MemoryCache
    from core.config import reload_config
    from db.base import Base, close_engine, init_engine
    _cache = MemoryCache()
    config = reload_config()
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('voice-db') / 'test.db'}")
    import db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from auth import setup_auth
    setup_auth(config, _cache)
    from main import create_app
    yield create_app()
    await close_engine()


@pytest.fixture
def providers(app, monkeypatch):
    """Voice switched on with a scripted provider; Inbox wakes are not dispatched."""
    from api import voice as voice_socket
    from core.config import VoiceConfig, get_config
    _cache._store.clear()
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=True, api_key="test-only"))
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("voice.bridge.GREETING_MARGIN_SECONDS", 0.0)
    made = []

    def factory(config, *, debug=False):
        made.append(ScriptedProvider(config, debug=debug, auto_reply=True, fail_open=getattr(factory, "fail", False)))
        return made[-1]
    monkeypatch.setattr(voice_socket, "provider_factory", factory)
    monkeypatch.setattr(voice_socket, "handover_planner", None)  # no model call: requests go on as they are
    factory.made = made
    return factory


@pytest.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def account(http, *, ensure=True):
    response = await http.post("/api/auth/register", json={"username": f"voice_{secrets.token_hex(4)}",
                                                           "password": "password123"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if ensure:
        ensured = await http.post("/api/assistant/ensure", json={"model": "test/model"}, headers=headers)
        assert ensured.status_code == 200
    return headers


async def ticket(http, headers, audience="voice"):
    body = {"json": {"audience": audience}} if audience else {}
    response = await http.post("/api/auth/ticket", headers=headers, **body)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def read_until(socket, predicate):
    """Messages up to and including the first JSON event matching ``predicate`` (or the close)."""
    seen = []
    while True:
        message = socket.receive()
        if message["type"] == "websocket.close":
            seen.append({"type": "<close>", "code": message["code"]})
            return seen
        seen.append(message["bytes"] if message.get("bytes") is not None else json.loads(message["text"]))
        if isinstance(seen[-1], dict) and predicate(seen[-1]):
            return seen


def heard_greeting():
    """The client has played the greeting (the scripted one is 0.1 s): the microphone counts again."""
    time.sleep(0.25)


def kinds(items):
    return [item["type"] if isinstance(item, dict) else "<audio>" for item in items]


async def test_refusals_carry_the_contract_close_codes(http, providers, app, monkeypatch):
    client = TestClient(app)
    headers = await account(http)

    def close_code(url):
        with client.websocket_connect(url) as socket:
            return socket.receive()["code"]
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers, audience=None)}") == 4001
    assert close_code("/ws/assistant/voice?ticket=never-issued") == 4001
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, await account(http, ensure=False))}") == 4404
    from core.config import VoiceConfig, get_config
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=False, api_key="test-only"))
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") == 4503
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=True, api_key=""))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") == 4503
    assert not providers.made


async def test_a_call_greets_answers_ping_and_hangs_up_with_its_cost(http, providers, app):
    from db.base import get_db_session
    from db.models.voice import VoiceCall
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        ready = socket.receive_json()
        assert ready["type"] == "ready" and ready["model"] == "qwen3.8-omni-flash-realtime"
        assert (ready["input_sample_rate"], ready["output_sample_rate"], ready["max_seconds"]) == (16000, 24000, 1800)
        assert ready["price_date"] == "2026-10-07"
        greeting = read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        assert kinds(greeting) == ["phase", "phrase", "<audio>", "cost", "phase"]
        assert greeting[0]["value"] == "greeting" and greeting[1] == {"type": "phrase", "key": "greeting"}
        from voice.phrases import greeting_instructions
        assert providers.made[0].commands("create")[0][1] == greeting_instructions("zh")  # free words, no fixed text
        assert "你是用户私人助理的电话前台" in providers.made[0].instructions
        assert "现在是 " in providers.made[0].instructions
        socket.send_bytes(FRAME)
        socket.send_json({"type": "ping"})
        assert read_until(socket, lambda item: item["type"] == "heartbeat")[-1]["elapsed_seconds"] >= 0
        socket.send_json({"type": "stop"})
        ending = read_until(socket, lambda item: False)
    assert kinds(ending) == ["cost", "ended", "<close>"] and ending[-1]["code"] == 1000
    ended = ending[1]
    assert (ended["reason"], ended["pending_turns"]) == ("hangup", 0) and ended["cost"]["final"]
    assert providers.made[0].commands("audio") and providers.made[0].closed
    async with get_db_session() as db:
        row = await db.scalar(select(VoiceCall).where(VoiceCall.id == ready["call_id"]))
    assert (row.status, row.end_reason, row.client) == ("ended", "hangup", "web")
    assert row.usage["input_text"] == 1180 and row.estimated_yuan == ended["cost"]["total_yuan"]


async def test_a_turn_reaches_the_main_session_and_hang_up_reports_it_pending(http, providers, app):
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from db.models.voice import VoiceTurn
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        heard_greeting()
        socket.send_bytes(ASK_MARKER + FRAME[len(ASK_MARKER):])
        accepted = read_until(socket, lambda item: item["type"] == "turn")[-1]
        assert accepted["state"] == "accepted" and accepted["inbox_id"]
        socket.send_json({"type": "stop"})
        ended = read_until(socket, lambda item: item["type"] == "ended")[-1]
    assert ended["pending_turns"] == 1
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
        turn = await db.scalar(select(VoiceTurn).where(VoiceTurn.id == accepted["turn_id"]))
    assert item.prompt == "帮我看看贪吃蛇进展" and item.origin_ref["entrypoint"] == "assistant_voice"
    assert (turn.outcome, turn.inbox_id, turn.provider_call_id) == ("late", item.id, "call-2")


async def test_a_request_reaches_the_assistant_planned_with_the_users_words_and_the_call(http, providers, app,
                                                                                        monkeypatch):
    """The assistant never heard the call: the plan's brief arrives with what the user said around it."""
    from api import voice as voice_socket
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from voice.handover import Plan
    planned = []

    async def planner(**kwargs):
        planned.append(kwargs)
        return Plan("brief", "帮我看看「贪吃蛇」项目现在的进展。")
    monkeypatch.setattr(voice_socket, "handover_planner", planner)
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        heard_greeting()
        socket.send_bytes(ASK_MARKER + FRAME[len(ASK_MARKER):])
        accepted = read_until(socket, lambda item: item["type"] == "turn")[-1]
        socket.send_json({"type": "stop"})
        read_until(socket, lambda item: item["type"] == "ended")
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
    assert item.prompt == "帮我看看贪吃蛇进展"  # the user's own words; the planned brief is the request
    assert item.origin_ref["voice_context"]["request"] == "帮我看看「贪吃蛇」项目现在的进展。"
    assert item.origin_ref["voice_context"]["heard"] == "帮我看看贪吃蛇进展"
    assert item.origin_ref["voice_context"]["call"][-1] == "用户：帮我看看贪吃蛇进展"
    [asked] = planned
    assert (asked["request"], asked["words"], asked["reads"]) == ("帮我看看贪吃蛇进展", "帮我看看贪吃蛇进展", None)


async def test_one_call_per_user_bad_frames_credits_and_provider_failure(http, providers, app, monkeypatch):
    from db.base import get_db_session
    from db.models.voice import VoiceCall
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as first:
        ready = first.receive_json()
        assert ready["type"] == "ready"
        with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as second:
            assert second.receive()["code"] == 4009
        first.send_bytes(b"\x00" * 3201)  # odd length
        ending = read_until(first, lambda item: False)
    assert [item for item in ending if isinstance(item, dict) and item["type"] == "error"] == [
        {"type": "error", "code": "bad_frame", "message": "音频数据不正确，通话已结束。"}]
    assert kinds(ending)[-3:] == ["cost", "ended", "<close>"] and ending[-1]["code"] == 4400
    providers.fail = True
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        failed = read_until(socket, lambda item: False)
    assert failed == [{"type": "error", "code": "provider_unavailable", "message": "没能接通，请稍后再试。"},
                      {"type": "<close>", "code": 1011}]
    async with get_db_session() as db:
        user_id = (await db.get(VoiceCall, ready["call_id"])).user_id
        statuses = (await db.scalars(select(VoiceCall.status).where(VoiceCall.user_id == user_id)
                                     .order_by(VoiceCall.started_at))).all()
        assert statuses == ["failed", "failed"]  # the bad frame, then the provider that never answered
    # Calls are paid in credits: none left (enforce billing) is refused before anything is dialled.
    from billing.service import BillingError

    async def broke(workspace_id):
        raise BillingError("INSUFFICIENT_CREDITS", "积分不足，请先充值后继续")
    monkeypatch.setattr("api.voice.voice_credit_room", broke)
    made = len(providers.made)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        assert socket.receive()["code"] == 4029
    assert len(providers.made) == made


async def settle_turn(inbox_id, text):
    """The assistant's run takes the voice turn and answers it, as the agent loop would."""
    from agent import inbox
    from agent.driver import reserve_run
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from models.message import TextPart
    from session.session import create_assistant_message, save_part, update_message_info
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, inbox_id)
    lease = await reserve_run(item.session_id, item.user_id)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = (item.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(item.session_id, batch.messages[0].id, model_id="test/model",
                                                 agent="assistant", user_id=item.user_id, run_fence=fence)
        await save_part(TextPart(session_id=item.session_id, message_id=message.id, text=text, channel="final"),
                        is_new=True, user_id=item.user_id, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=item.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    finally:
        await lease.release(session_status="idle")


async def test_a_turn_is_acknowledged_at_once_and_its_result_told_once_from_a_note(http, providers, app,
                                                                                   monkeypatch):
    from db.base import get_db_session
    from db.models.voice import VoiceTurn
    from voice import phrases
    saved = []
    monkeypatch.setattr("api.voice.summary.save_after_call", lambda *args: saved.append(args))
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        heard_greeting()
        socket.send_bytes(ASK_MARKER + FRAME[len(ASK_MARKER):])
        accepted = read_until(socket, lambda item: item["type"] == "turn")[-1]
        provider = providers.made[0]
        assert accepted["state"] == "accepted"
        # The front desk's call is answered before the assistant has done anything.
        assert provider.commands("output") == [("output", "call-2", {"status": "accepted",
                                                                     "note": "结果稍后以后台备注送到"})]
        await settle_turn(accepted["inbox_id"], "[贪吃蛇](/app/s/x) 的收尾自检做完了，一切正常。")
        told = read_until(socket, lambda item: item["type"] == "turn" and item["state"] == "delivered")
        assert told[-1]["turn_id"] == accepted["turn_id"] and "<audio>" in kinds(told)
        socket.send_json({"type": "stop"})
        ended = read_until(socket, lambda item: item["type"] == "ended")[-1]
    assert ended["pending_turns"] == 0
    [(_, note)] = provider.commands("note")
    assert note == "（后台备注，不是用户说的话）关于用户说的“帮我看看贪吃蛇进展”：个人助理回来了：贪吃蛇的收尾自检做完了，一切正常。"
    deliveries = [text for _, text in provider.commands("create") if text and text.startswith("个人助理的结果到了")]
    assert deliveries == [phrases.delivery_instructions("贪吃蛇的收尾自检做完了，一切正常。", "zh")]
    assert len(provider.commands("output")) == 1  # one output per call; the result never reused it
    async with get_db_session() as db:
        turn = await db.get(VoiceTurn, accepted["turn_id"])
    assert (turn.outcome, turn.delivered_at is not None) == ("delivered", True)
    # After hang-up the call's own transcript is summarized for the next greeting (in the background).
    [(call_id, transcript, previous)] = saved
    assert call_id == turn.call_id and previous == ""
    assert "用户：帮我看看贪吃蛇进展" in transcript and "后台备注：（后台备注" in transcript


async def test_a_user_picks_a_voice_and_the_next_call_speaks_with_it(http, providers, app):
    """Settings → 语音通话: only listed voices are accepted; a call uses the saved one."""
    from db.base import get_db_session
    from db.models.voice import VoiceCall
    headers = await account(http)
    listed = (await http.get("/api/assistant/voice/voices", headers=headers)).json()
    ids = [voice["id"] for voice in listed["voices"]]
    assert listed["default"] == listed["selected"] == "Serena" and {"Tina", "Serena", "Andre", "Jennifer"} <= set(ids)
    assert all(voice["lang"] in ("zh", "en") and voice["gender"] in ("female", "male") for voice in listed["voices"])
    for refused in ("Cherry", "serena", "<script>"):
        assert (await http.put("/api/assistant/voice/voice", json={"voice": refused}, headers=headers)).status_code == 422
    chosen = await http.put("/api/assistant/voice/voice", json={"voice": "Liora Mira"}, headers=headers)
    assert chosen.json() == {"selected": "Liora Mira"}
    assert (await http.get("/api/assistant/voice/voices", headers=headers)).json()["selected"] == "Liora Mira"
    prefs = (await http.get("/api/auth/me/preferences", headers=headers)).json()
    assert prefs["extra"]["assistant_voice"] == "Liora Mira"
    client = TestClient(app)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        call_id = socket.receive_json()["call_id"]
        socket.send_json({"type": "stop"})
        read_until(socket, lambda item: False)
    assert providers.made[-1].config.voice == "Liora Mira"
    async with get_db_session() as db:
        assert (await db.get(VoiceCall, call_id)).voice == "Liora Mira"
    # Another account keeps the default.
    other = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, other)}") as socket:
        socket.receive_json()
        socket.send_json({"type": "stop"})
        read_until(socket, lambda item: False)
    assert providers.made[-1].config.voice == "Serena"


async def test_previews_are_served_for_listed_voices_only(http, providers):
    sample = await http.get("/api/assistant/voice/samples/Liora Mira")
    assert sample.status_code == 200 and sample.headers["content-type"] == "audio/mp4" and len(sample.content) > 5000
    assert (await http.get("/api/assistant/voice/samples/Cherry")).status_code == 404
    assert (await http.get("/api/assistant/voice/samples/..%2Fconfig.py")).status_code == 404



async def test_the_call_is_answered_with_the_greeting_made_and_its_echo_never_cuts_it(http, providers, app):
    """The client rings until the greeting is made; while it plays, the microphone reaches no model."""
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        ready = socket.receive_json()
        provider = providers.made[-1]
        # Made before the answer: the greeting was requested and finished before `ready` went out.
        assert ready["type"] == "ready" and provider.commands("create")
        greeting = read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        assert kinds(greeting) == ["phase", "phrase", "<audio>", "cost", "phase"]
        socket.send_bytes(ASK_MARKER + FRAME[len(ASK_MARKER):])  # its own echo, at once
        heard_greeting()
        socket.send_bytes(b"\x10\x00" * 1600)
        socket.send_json({"type": "stop"})
        read_until(socket, lambda item: False)
    first, second = [command for command in provider.commands("audio")][:2]
    assert first[2] == b"\0\0\0\0" and second[2] == b"\x10\x00\x10\x00"  # held, then the microphone again



async def test_a_call_ends_with_a_goodbye_when_its_credits_are_spent_and_is_billed(http, providers, app,
                                                                                 monkeypatch):
    """No time quota: the call may spend the balance; reaching it says goodbye and ends as ``quota``."""
    from decimal import Decimal
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.billing import UsageEvent

    async def room(workspace_id):
        return Decimal("0.000001")  # less than the greeting costs
    monkeypatch.setattr("api.voice.voice_credit_room", room)
    monkeypatch.setenv("BILLING_MODE", "shadow")
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        call_id = socket.receive_json()["call_id"]
        heard = read_until(socket, lambda item: item["type"] == "ended")
    limit = next(item for item in heard if isinstance(item, dict) and item["type"] == "limit")
    assert limit["reason"] == "credits"
    assert heard[-1]["reason"] == "quota"
    goodbye = providers.made[-1].commands("create")[-1][1]
    assert "积分用完了" in goodbye
    async with get_db_session() as db:
        [event] = (await db.scalars(select(UsageEvent).where(UsageEvent.idempotency_key == f"voice:{call_id}"))).all()
    assert (event.kind, event.status, event.session_title) == ("voice_call", "shadow", "语音通话")
    assert event.credits == Decimal(heard[-1]["cost"]["total_yuan"]) > 0


async def test_a_task_report_finished_during_the_call_is_told_unasked(http, providers, app, monkeypatch):
    """Measured on QA: a handed-over task's result reached the conversation but never the call."""
    from agent import inbox
    from agent.driver import reserve_run
    from assistant.commands import accept_task_command
    from db.base import get_db_session
    from db.models.assistant import TaskResult
    from db.models.session import Session
    from db.models.voice import VoiceCall
    from models.message import TextPart
    from session.session import create_assistant_message, save_part, update_message_info
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        call_id = socket.receive_json()["call_id"]
        read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        async with get_db_session() as db:
            main = await db.get(Session, (await db.get(VoiceCall, call_id)).main_session_id)
        # A task handed over earlier finishes now; its result is reported in the main session.
        task = await accept_task_command(user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id,
                                         project_id=main.project_id, idempotency_key="publish-1",
                                         prompt="打开抖音创作者中心", title="制作iPhone 18口播视频")
        lease = await reserve_run(task["execution_session_id"], main.user_id)
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, model_id="test/model",
                                                 agent="build", user_id=main.user_id, run_fence=fence)
        await save_part(TextPart(session_id=lease.session_id, message_id=message.id, text="云桌面自动化未就绪。"),
                        user_id=main.user_id, is_new=True, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=main.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        await lease.release(session_status="idle")
        async with get_db_session() as db:
            result = (await db.scalars(select(TaskResult).where(TaskResult.task_id == task["task_id"]))).one()
        # The assistant's report turn (assistant/results.py) writes what the conversation shows.
        await settle_turn(result.assistant_inbox_id, "尝试打开抖音创作者中心受阻，因为云桌面自动化未就绪。")
        told = read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening"
                          and not item["working"])
        notes = [text for _, text in providers.made[-1].commands("note")]
        assert notes == ["（后台备注，不是用户说的话）个人助理主动汇报，任务「制作iPhone 18口播视频」有新结果："
                         "尝试打开抖音创作者中心受阻，因为云桌面自动化未就绪。"]
        assert "<audio>" in kinds(told)  # said, unasked
        socket.send_json({"type": "stop"})
        read_until(socket, lambda item: item["type"] == "ended")
