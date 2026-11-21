from types import SimpleNamespace

import pytest

from agent import inbox, loop, processor
from agent.driver import reserve_run
from core.device_locale import DeviceLocale, for_turn, mobile_request_locale, valid_timezone
from core.ui_language import UILanguageMiddleware, set_ui_language
from models.message import TextPart
from session.session import create_session, create_user_message
from tests.unit.test_agent_inbox import _seed
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def mobile_request(handler, language, timezone):
    async def run(_scope, _receive, _send):
        await handler()

    await UILanguageMiddleware(run)({
        "type": "http",
        "headers": [
            (b"x-client-type", b"mobile"),
            (b"accept-language", language.encode()),
            (b"x-time-zone", timezone.encode()),
        ],
    }, None, None)


async def test_first_acceptance_freezes_locale_but_a_retry_stays_idempotent():
    user_id, _, session_id, _ = await _seed()
    receipts = []

    async def accept():
        receipts.append(await inbox.accept_inbox_item(
            session_id=session_id, user_id=user_id, delivery="followup",
            prompt="Make a menu", client_id="mobile-menu", origin="human",
            origin_ref={"actor_user_id": user_id, "entrypoint": "light_production"},
        ))

    await set_ui_language(user_id, "zh-CN")
    await mobile_request(accept, "vi-VN", "Asia/Ho_Chi_Minh")
    await mobile_request(accept, "en-GB", "America/Los_Angeles")
    assert not mobile_request_locale.get()
    first, retry = receipts
    assert retry.id == first.id and not retry.created
    assert retry.origin_ref["device_locale"] == {"language": "vi-VN", "timezone": "Asia/Ho_Chi_Minh"}
    part = TextPart(text="Make a menu", origin="human", origin_ref=retry.origin_ref)
    locale = await for_turn(user_id, SimpleNamespace(parts=[part]))
    assert locale == DeviceLocale("vi-VN", "Asia/Ho_Chi_Minh")
    assert valid_timezone("../etc/passwd") is None


@pytest.mark.parametrize(("language", "zone", "label"), [
    ("en-US", "America/Los_Angeles", "英文（English）"),
    ("vi-VN", "Asia/Ho_Chi_Minh", "越南语（Tiếng Việt）"),
])
async def test_regular_agent_uses_the_frozen_mobile_locale(monkeypatch, language, zone, label):
    user_id, _, workspace_id = await accounts()
    config = _loop_config()
    session = await create_session(
        title="Locale QA", model=config.model, agent="build",
        user_id=user_id, workspace_id=workspace_id,
    )
    session_id = session.id
    await set_ui_language(user_id, "zh-CN")
    real_system = loop._build_system_prompt
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_build_system_prompt", real_system)
    observed = []

    async def stream(**kwargs):
        observed.append("\n".join(kwargs["system"]))
        yield {"type": "text_delta", "text": "Ready" if language == "en-US" else "Sẵn sàng"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(session_id, user_id)
    try:
        async def add_input():
            await create_user_message(
                session_id=session_id, text="帮我写一份菜单", user_id=user_id,
                model=config.model,
                origin="human", origin_ref={"actor_user_id": user_id},
                run_fence=(session_id, lease.run_id, lease.generation), bind_trigger=True,
            )

        await mobile_request(add_input, language, zone)
        await loop.run_loop(session_id, user_id=user_id, lease=lease)
    finally:
        await lease.release(session_status="idle")
    [system] = observed
    assert "用" + label in system
    assert f"User's time zone: {zone}" in system
