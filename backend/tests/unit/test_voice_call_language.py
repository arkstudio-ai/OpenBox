"""Real ASR binding and ephemeral language precedence across the bridge."""
import pytest

from tests.support.voice_fakes import event, started, done, audio
from tests.unit.test_voice_bridge import replay
from tests.unit.test_voice_memory_context import call, packet, finish_tasks
from voice.call_language import CallLanguage


def test_legacy_prompt_keeps_explicit_language_requests_without_semantic_state():
    from voice.prompt import FrontFacts, user_lines
    assert "用户明确要求本次换语言时遵循本次要求" in "\n".join(user_lines(FrontFacts(language="English")))
    native = "\n".join(user_lines(FrontFacts(language="English", native_memory=True)))
    assert "Current spoken response policy" in native


def language_packet(text, language="zh-CN", scope="call", *, saved="English"):
    value = packet(text)
    value["settings"]["language"] = saved
    value["route"]["spoken_language"] = {"role": "restore" if scope == "reset" else "change",
        "quote": text, "language": language, "scope": scope, "confidence": 1}
    return value


def test_call_override_survives_new_turns_and_does_not_cross_calls_or_settings_changes():
    language = CallLanguage()
    request = "我们这次先用中文聊聊"
    value = language_packet(request)
    language.apply(value, request)
    language.begin_turn()
    assert language.resolve(value["settings"]) == "zh-CN"
    assert value["settings"]["language"] == "English"  # no persisted-setting mutation
    assert CallLanguage().resolve(value["settings"]) == "English"
    assert language.resolve({"language": "fr"}) == "fr"
    assert language.call is None


def test_one_reply_overrides_call_then_expires_and_reset_restores_settings():
    language = CallLanguage()
    language.apply(language_packet("今天中文聊"), "今天中文聊")
    language.apply(language_packet("这句用法语", "fr", "reply"), "这句用法语")
    assert language.resolve({"language": "English"}) == "fr"
    language.begin_turn()
    assert language.resolve({"language": "English"}) == "zh-CN"
    language.apply(language_packet("按我设置的来", "", "reset"), "按我设置的来")
    assert language.resolve({"language": "English"}) == "English"


def test_old_packet_cannot_change_language_for_an_unrelated_utterance():
    language = CallLanguage()
    language.apply(language_packet("改用中文"), "最近有什么展览")
    assert language.resolve({"language": "English"}) == "English"


@pytest.mark.parametrize("scope", ["call", "reply"])
async def test_override_reaches_both_ack_and_actual_response_policy_before_audio(scope):
    request = "我想先用中文跟你聊会儿"
    async def prepare(_scope, text, recent):
        return language_packet(text, scope=scope), "Saved default language is English"
    bridge, provider, _ = call(prepare)
    try:
        await replay(bridge, event("user_started", item_id="human"), event("user_stopped"),
            started("auto"), done("auto", status="cancelled"),
            event("user_transcript", item_id="human", text=request))
        await finish_tasks(bridge)
        assert 'Speak your reply in "zh-CN"' in provider.commands("instructions")[-1][1]
        assert 'Speak your reply in "zh-CN"' in provider.commands("response_policy")[-1][1]
        assert not bridge.link.started
        await replay(bridge, started("answer"), audio("answer"), done("answer"))
        expected = "zh-CN" if scope == "call" else "English"
        assert f'Speak your reply in "{expected}"' in bridge._response_policy()
        await bridge._create("A different task has finished.")
        assert f'Speak your reply in "{expected}"' in provider.commands("response_policy")[-1][1]
    finally:
        await bridge.close()
