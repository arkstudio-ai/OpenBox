"""The voices offered in Settings → 语音通话 (voice/voices.py)."""
from core.config import VoiceConfig
from voice import voices


def test_the_catalog_is_well_formed_and_offers_the_default():
    ids = [voice.id for voice in voices.VOICES]
    assert len(ids) == len(set(ids)) and VoiceConfig().voice in ids
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
