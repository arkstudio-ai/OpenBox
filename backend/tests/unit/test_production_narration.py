import io
import wave
from decimal import Decimal

import pytest

from light.mode import LightError
from light.production_narration import RATE, _voice_track, check_confirmation, fingerprint
from light.production_usage import tts_quote


def _sample(seconds):
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        sound.writeframes(b"\x64\x00" * round(RATE * seconds))
    return out.getvalue()


def test_confirmed_voice_track_honors_the_edited_shots():
    shots = [{"start": 0, "end": 2}, {"start": 2, "end": 4}]
    audio, captions = _voice_track(shots, ["", "蔬菜入锅"], [b"", _sample(1.25)])
    assert captions == [{"from_sec": 2.12, "to_sec": 3.37, "text": "蔬菜入锅"}]
    with wave.open(io.BytesIO(audio)) as sound:
        assert sound.readframes(2 * RATE) == b"\x00\x00" * (2 * RATE)
        assert sound.getnframes() == 4 * RATE
    with pytest.raises(LightError) as error:
        _voice_track(shots, ["", "蔬菜入锅"], [b"", _sample(2)])
    assert error.value.code == "PRODUCTION_NARRATION_LENGTH"
    assert error.value.extra["shot"] == 2


def test_an_old_preview_cannot_replace_the_current_voice_or_footage():
    form = {"redo_narration": True, "asset_ids": ["original"], "voice_id": "system:documentary"}
    form["narration_draft"] = {"id": "cut", "materials": fingerprint(form)}
    form["narration_preview"] = {"id": "preview", "draft_id": "cut", "voice_id": form["voice_id"], "locale": "zh-CN"}
    check_confirmation(form, "preview", "zh-CN")
    for edit in (
        {"asset_ids": ["different"]},
        {"voice_id": "system:bright"},
        {"narration_preview": None},
    ):
        with pytest.raises(LightError) as error:
            check_confirmation({**form, **edit}, "preview", "zh-CN")
        assert error.value.code == "PRODUCTION_NARRATION_CONFIRM"
    with pytest.raises(LightError):
        check_confirmation(form, "preview", "en-US")
    check_confirmation({"redo_narration": False}, None, "en-US")


def test_bailian_speech_usage_uses_input_and_output_tokens():
    amount = tts_quote("qwen-audio-3.1-tts-flash", {"input_tokens": 16, "output_tokens": 39})
    assert amount.credits == amount.cost == Decimal("0.000492")


def test_streaming_voice_uses_actual_pcm_length():
    from light.production_voice import _canonical_wav

    valid = _sample(2.4)
    streaming = bytearray(valid)
    streaming[4:8] = (0x7FFFFFBF).to_bytes(4, "little")
    streaming[40:44] = (0x7FFFFF9B).to_bytes(4, "little")
    assert _canonical_wav(bytes(streaming)) == (valid, 2.4)
    assert _canonical_wav(valid)[0] is valid
