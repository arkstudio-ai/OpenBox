"""The voices offered in Settings → 语音通话 (voice/voices.py)."""
from core.config import VoiceConfig
from voice import voices
from voice.models import AUDIO_MODEL, OMNI_MODEL
from assistant import appearance


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
    standard = voices.selection({voices.MODEL_KEY: OMNI_MODEL, "assistant_voice_omni": "Andre",
        voices.MODEL_ROLLOUT_KEY: voices.MODEL_ROLLOUT}, cfg)
    assert standard["selected"] == "Andre" and standard["default"] == "Tina"
    assert all(row["id"] in voices.BY_ID for row in standard["voices"])


def test_existing_users_and_old_deployment_overrides_get_audio_without_losing_voices():
    cfg = VoiceConfig(model=OMNI_MODEL, voice="Tina")
    for extra in ({}, {voices.MODEL_KEY: OMNI_MODEL, voices.PREFERENCE_KEY: "Serena"},
                  {voices.MODEL_KEY: OMNI_MODEL, voices.MODEL_ROLLOUT_KEY: "older-rollout"},
                  {voices.MODEL_KEY: OMNI_MODEL, "assistant_voice_audio": "longanhuan_v3.1"}):
        before = dict(extra)
        selected = voices.selection(extra, cfg)
        assert selected["model"] == selected["default_model"] == AUDIO_MODEL
        assert selected["selected"] == extra.get("assistant_voice_audio", "longanqian_v3.1")
        assert extra == before


def test_configured_clone_remains_available_until_user_selects_appearance_or_voice():
    clone = AUDIO_MODEL + "-custom-123"
    cfg = VoiceConfig(model=AUDIO_MODEL, voice=clone)
    state = voices.selection({}, cfg)
    assert state["selected"] == clone and state["voice_source"] == "model_default"
    portrait = voices.selection({appearance.KEY: {"preset": "ip-11"}}, cfg)
    assert portrait["selected"] == "longanlufeng" and portrait["voice_source"] == "appearance"
    manual = voices.selection({appearance.KEY: {"preset": "ip-11"},
        "assistant_voice_audio": "longanqian_v3.1"}, cfg)
    assert manual["selected"] == "longanqian_v3.1" and manual["voice_source"] == "user"


async def test_existing_model_choice_resets_once_and_later_explicit_choice_persists():
    from db.repository.preference_repo import PgPreferenceRepo
    cfg = VoiceConfig(model=OMNI_MODEL, voice="Tina")
    repo = PgPreferenceRepo()
    user = "voice-rollout-existing"
    await repo.upsert(user, extra={voices.MODEL_KEY: OMNI_MODEL, voices.PREFERENCE_KEY: "Serena",
        "assistant_voice_audio": "longanhuan_v3.1", "locale": "zh-CN", "onboarding": {"welcome": True}})
    initial = await voices.user_selection(user, cfg)
    assert initial["model"] == AUDIO_MODEL and initial["selected"] == "longanhuan_v3.1"
    # A legacy client's voice-only save cannot silently restore the old Omni model.
    changed_voice = await voices.save_choice(user, cfg, model=None, voice="longanqian_v3.1")
    assert changed_voice["model"] == AUDIO_MODEL
    assert voices.MODEL_ROLLOUT_KEY not in (await repo.get(user))["extra"]
    # Choosing Omni explicitly remains supported and recovers its own previous voice.
    standard = await voices.save_choice(user, cfg, model=OMNI_MODEL, voice=None)
    assert standard["model"] == OMNI_MODEL and standard["selected"] == "Serena"
    assert standard["default_model"] == AUDIO_MODEL
    assert await voices.user_selection(user, VoiceConfig()) == standard
    saved = (await repo.get(user))["extra"]
    assert saved[voices.MODEL_ROLLOUT_KEY] == voices.MODEL_ROLLOUT
    assert saved["locale"] == "zh-CN" and saved["onboarding"] == {"welcome": True}
    restored = await voices.save_choice(user, cfg, model=AUDIO_MODEL, voice=None)
    assert restored["selected"] == "longanqian_v3.1"


def test_all_portraits_share_the_pickers_gender_and_expert_voice_defaults():
    catalog = appearance.preset_catalog()
    assert len({option["id"] for option in catalog}) == 34
    assert {category: sum(option["category"] == category for option in catalog)
            for category in ("female", "male", "pets")} == {"female": 17, "male": 7, "pets": 10}
    assert {option["voice_gender"] for option in catalog if option["category"] == "pets"} == {"female", "male"}
    # An old deployment voice does not override character defaults.
    cfg = VoiceConfig(voice="longanhuan_v3.1")
    for option in catalog:
        selected = voices.selection({appearance.KEY: {"preset": option["id"]}}, cfg)
        expected = "longanlufeng" if option["voice_gender"] == "male" else "longanqian_v3.1"
        assert selected["model"] == AUDIO_MODEL
        assert selected["selected"] == selected["default"] == expected
        assert selected["voice_source"] == "appearance"
    # Old aliases, missing/corrupt preferences, and custom uploads are deterministic.
    for preset, expected in (("cat", "longanlufeng"), ("sprout", "longanqian_v3.1"),
                             ("custom", "longanqian_v3.1"), ("unknown", "longanqian_v3.1"),
                             ({}, "longanqian_v3.1")):
        assert voices.selection({appearance.KEY: {"preset": preset}}, cfg)["selected"] == expected


async def test_model_only_saves_do_not_pin_automatic_voices_and_explicit_default_picks_do():
    from db.repository.preference_repo import PgPreferenceRepo
    repo, cfg, user = PgPreferenceRepo(), VoiceConfig(), "voice-appearance-auto"
    await repo.upsert(user, extra={appearance.KEY: {"preset": "ip-04"}, "locale": "zh-CN"})
    for model in (AUDIO_MODEL, OMNI_MODEL, AUDIO_MODEL):
        state = await voices.save_choice(user, cfg, model=model, voice=None)
        assert state["voice_source"] != "user"
    assert state["selected"] == "longanlufeng"
    extra = (await repo.get(user))["extra"]
    assert voices.PREFERENCE_KEY not in extra
    assert not any(key in extra for key in voices.VOICE_KEYS.values())
    await appearance.save(user, {"preset": "ip-01"})
    assert (await voices.user_selection(user, cfg))["selected"] == "longanqian_v3.1"
    # Explicitly choosing the voice already in use is a real preference.
    chosen = await voices.save_choice(user, cfg, model=None, voice="longanqian_v3.1")
    assert chosen["voice_source"] == "user"
    await appearance.save(user, {"preset": "ip-11"})
    reloaded = await voices.user_selection(user, VoiceConfig())
    assert reloaded["selected"] == "longanqian_v3.1" and reloaded["default"] == "longanlufeng"
    assert reloaded["voice_source"] == "user"
    assert (await repo.get(user))["extra"]["locale"] == "zh-CN"


async def test_custom_upload_defaults_to_female_but_never_overwrites_chosen_voices():
    from db.repository.preference_repo import PgPreferenceRepo
    cfg, user, other = VoiceConfig(), "voice-custom-manual", "voice-custom-auto"
    for owner in (user, other):
        await appearance.save(owner, {"preset": "ip-02"})
    await voices.save_choice(user, cfg, model=AUDIO_MODEL, voice="longanlufeng")
    for owner in (user, other):
        await appearance.save(owner, {"preset": "custom", "image_key": f"assistant-avatars/{owner}/{'a' * 64}.png"})
    assert (await voices.user_selection(other, cfg))["selected"] == "longanqian_v3.1"
    assert (await voices.user_selection(user, cfg))["selected"] == "longanlufeng"
    # Old explicit selections, including the old default itself, are preserved.
    for key in (voices.PREFERENCE_KEY, voices.VOICE_KEYS[AUDIO_MODEL]):
        legacy_user = f"voice-legacy-{key}"
        await PgPreferenceRepo().upsert(legacy_user, extra={key: "longanqian_v3.1", appearance.KEY: {"preset": "ip-04"}})
        assert (await voices.user_selection(legacy_user, cfg))["selected"] == "longanqian_v3.1"
