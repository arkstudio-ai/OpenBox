"""The voices offered in Settings → 语音通话 (voice/voices.py)."""
from core.config import VoiceConfig
from voice import voices
from voice.models import AUDIO_MODEL, OMNI_MODEL


def test_the_catalog_is_well_formed_and_offers_the_default():
    ids = [voice.id for voice in voices.VOICES]
    assert len(ids) == len(set(ids)) and "Tina" in ids
    assert {voice.lang for voice in voices.VOICES} == {"zh", "en"}
    assert {voice.gender for voice in voices.VOICES} == {"female", "male"}
    # Refused by qwen3.8-omni-flash-realtime (checked live): never offered.
    assert not {"Cherry", "Ethan", "Chelsie", "Vivian"} & set(ids)
    assert all(voice.name and voice.description and voice.description_en for voice in voices.VOICES)


def test_an_unknown_choice_falls_back_to_the_configured_voice():
    assert voices.resolve("Andre", "Serena") == "Andre"
    assert voices.resolve("Cherry", "Serena") == "Serena"
    assert voices.resolve(None, "Tina") == "Tina"


def test_every_offered_voice_has_a_preview():
    for voice in voices.VOICES:
        path = voices.sample_path(voice.id)
        assert path is not None and 5_000 < path.stat().st_size < 60_000, voice.id
    assert voices.sample_path("Cherry") is None and voices.sample_path("../voices") is None


def test_model_switch_resolves_old_preferences_and_defaults_without_mixing_catalogues():
    audio_ids = {row["id"] for row in voices.catalog(AUDIO_MODEL)}
    assert len(audio_ids) == 13 and "longanqian_v3.1" in audio_ids
    assert not audio_ids & set(voices.BY_ID)
    assert voices.resolve("Tina", "Serena", AUDIO_MODEL) == "longanqian_v3.1"
    assert voices.resolve("longanhuan_v3.1", "Tina", AUDIO_MODEL) == "longanhuan_v3.1"
    assert voices.resolve("longanhuan_v3.1", "longanqian_v3.1", OMNI_MODEL) == "Tina"
    clone = AUDIO_MODEL + "-custom-123"
    assert voices.resolve(None, clone, AUDIO_MODEL) == clone
    assert voices.resolve(clone, "Tina", OMNI_MODEL) == "Tina"
    assert voices.sample_path("Tina", AUDIO_MODEL) is None
    assert voices.sample_path("../config.py", AUDIO_MODEL) is None


def test_audio_voices_have_their_own_preview_recordings():
    for voice in voices.AUDIO_VOICES:
        path = voices.sample_path(voice.id, AUDIO_MODEL)
        assert path is not None and 3_000 < path.stat().st_size < 100_000, voice.id


def test_expert_is_the_default_and_invalid_preferences_cannot_mix_models():
    cfg = VoiceConfig()
    chosen = voices.selection({}, cfg)
    assert chosen["model"] == chosen["default_model"] == AUDIO_MODEL
    assert chosen["selected"] == "longanqian_v3.1"
    assert chosen["models"][0] == {"id": AUDIO_MODEL, "name": "Audio 3.1", "tier": "expert"}
    assert voices.selection({voices.MODEL_KEY: "unknown", voices.PREFERENCE_KEY: "Tina"}, cfg) == chosen
    assert voices.selection({voices.MODEL_KEY: [], voices.PREFERENCE_KEY: {}}, cfg) == chosen
    standard = voices.selection({voices.MODEL_KEY: OMNI_MODEL, "assistant_voice_omni": "Andre"}, cfg)
    assert standard["selected"] == "Andre" and standard["default"] == "Tina"
    assert all(row["id"] in voices.BY_ID for row in standard["voices"])
