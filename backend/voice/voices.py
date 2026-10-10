"""The voices a user can pick for calls (Settings → 语音通话).

The Omni catalogue was checked live on ``qwen3.8-omni-flash-realtime`` on
2026-10-07; the Audio catalogue on ``qwen-audio-3.1-realtime-plus`` on
2026-10-10, including every preview. The Omni list is longer
(56, https://help.aliyun.com/zh/model-studio/omni-voice-list,
"Qwen3.8-Omni-Flash-Realtime"); the rest are dialects (四川话、粤语…), Taiwan or
Hong Kong accents, character voices and foreign personas, which do not suit a
personal assistant's front desk. Voices listed for older omni models
(Cherry, Ethan, Chelsie…) are refused at the first reply, so they are not here.

Model and per-model voices live in user preferences. The legacy
``extra.assistant_voice`` is preserved when first switching models.
"""
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from voice.models import AUDIO_MODEL, OMNI_MODEL, is_audio

PREFERENCE_KEY = "assistant_voice"
MODEL_KEY = "assistant_voice_model"
MODEL_OPTIONS = (
    {"id": AUDIO_MODEL, "name": "Audio 3.1", "tier": "expert"},
    {"id": OMNI_MODEL, "name": "Omni", "tier": "standard"},
)
MODEL_IDS = frozenset(row["id"] for row in MODEL_OPTIONS)
VOICE_KEYS = {AUDIO_MODEL: "assistant_voice_audio", OMNI_MODEL: "assistant_voice_omni"}
#: A 4 s preview per voice ("你好，我是你的私人助理，有事随时叫我。", or English for the
#: English ones), recorded from each model itself, AAC at 32 kbps.
SAMPLES = Path(__file__).resolve().parent / "samples"


@dataclass(frozen=True)
class Voice:
    id: str           # the provider's voice parameter, exactly (case and spaces matter)
    name: str         # its Chinese name in Alibaba Cloud's list
    gender: str       # female / male
    lang: str         # zh: Mandarin first; en: English first (both speak both)
    description: str
    description_en: str


VOICES: tuple[Voice, ...] = (
    Voice("Tina", "甜甜", "female", "zh", "甜暖亲切，办事利落（官方默认）", "Sweet and warm, quick to solve problems"),
    Voice("Serena", "苏瑶", "female", "zh", "温柔小姐姐", "A gentle young woman"),
    Voice("Maia", "四月", "female", "zh", "知性与温柔", "Intellect and gentleness"),
    Voice("Liora Mira", "清欢", "female", "zh", "温柔，有烟火气", "Gentle, warm and down to earth"),
    Voice("Mia", "舒然", "female", "zh", "细腻治愈，慢节奏", "Delicate, soothing and unhurried"),
    Voice("Katerina", "卡捷琳娜", "female", "zh", "成熟干练的御姐音", "Mature and commanding"),
    Voice("Cici", "绵绵", "female", "zh", "活泼的邻家妹妹，声线软糯", "A lively girl next door, soft-voiced"),
    Voice("Andre", "安德雷", "male", "zh", "磁性、自然、沉稳", "Magnetic, natural and steady"),
    Voice("Raymond", "林川野", "male", "zh", "声音清亮", "Clear and bright"),
    Voice("Theo Calm", "予安", "male", "zh", "安静温和，善解人意", "Calm, understanding and soothing"),
    Voice("Evan", "江晨", "male", "zh", "阳光的男大学生", "A youthful college student"),
    Voice("Zane", "泽恩", "male", "zh", "低沉有磁性", "Deep and magnetic"),
    Voice("Jennifer", "詹妮弗", "female", "en", "美式英语女声，电影质感", "American English, cinematic quality"),
    Voice("Mione", "敏儿", "female", "en", "英式英语，成熟知性", "British English, mature and bright"),
    Voice("Aiden", "艾登", "male", "en", "美式英语男声，阳光随和", "American English, an easygoing young man"),
)
BY_ID = {voice.id: voice for voice in VOICES}

# Only IDs listed for Realtime 3.1, not the much larger Qwen-Audio-TTS catalogue.
# https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides
AUDIO_VOICES: tuple[Voice, ...] = (
    Voice("longanqian_v3.1", "龙安浅", "female", "zh", "中文女声（官方默认）", "Chinese female voice (default)"),
    Voice("longanhuan_v3.1", "龙安欢", "female", "zh", "多语种女声", "Multilingual female voice"),
    Voice("longanlingxin_v3.1", "龙安灵心", "female", "zh", "多语种女声", "Multilingual female voice"),
    Voice("longanfengyue_v3.1", "龙安风悦", "female", "zh", "多语种女声", "Multilingual female voice"),
    Voice("xunanchuan_v3.1", "许南川", "male", "zh", "多语种男声", "Multilingual male voice"),
    Voice("beth_v3.1", "Beth", "female", "en", "英语女声", "English female voice"),
    Voice("betty_v3.1", "Betty", "female", "en", "英语女声", "English female voice"),
    Voice("cally_v3.1", "Cally", "female", "en", "英语女声", "English female voice"),
    Voice("longanqian", "龙安浅（经典）", "female", "zh", "中文女声", "Chinese female voice"),
    Voice("longanlingxin", "龙安灵心（经典）", "female", "zh", "中文女声", "Chinese female voice"),
    Voice("longanlingxi", "龙安灵希（经典）", "female", "zh", "中文女声", "Chinese female voice"),
    Voice("longanxiaoxin", "龙安小昕（经典）", "female", "zh", "亲切活泼", "Friendly and lively"),
    Voice("longanlufeng", "龙安鲁风（经典）", "male", "zh", "明亮开朗", "Bright and cheerful"),
)
AUDIO_BY_ID = {voice.id: voice for voice in AUDIO_VOICES}


def _by_id(model: str) -> dict[str, Voice]:
    return AUDIO_BY_ID if is_audio(model) else BY_ID


def catalog(model: str = OMNI_MODEL) -> list[dict]:
    return [asdict(voice) for voice in _by_id(model).values()]


def sample_path(voice_id: str, model: str = OMNI_MODEL) -> Path | None:
    """The preview file of one of our voices, or None."""
    if voice_id not in _by_id(model):
        return None
    path = SAMPLES / (re.sub(r"[^A-Za-z0-9]+", "_", voice_id).strip("_") + ".m4a")
    return path if path.is_file() else None


def resolve(chosen: str | None, default: str, model: str = OMNI_MODEL) -> str:
    """Never send a saved voice from the other model to the provider.

    Configured clone IDs remain usable; user preferences are limited to the
    active catalogue. Switching models does not erase a previous preference.
    """
    offered = _by_id(model)
    if chosen in offered:
        return chosen
    clone_prefix = AUDIO_MODEL + "-" if is_audio(model) else "qwen-omni-vc-"
    if default in offered or default.startswith(clone_prefix):
        return default
    return "longanqian_v3.1" if is_audio(model) else "Tina"


def selection(extra: dict, config) -> dict:
    """One consistent model/catalogue/voice snapshot, also used to start a call."""
    default_model = config.model if config.model in MODEL_IDS else AUDIO_MODEL
    model = extra.get(MODEL_KEY)
    if not isinstance(model, str) or model not in MODEL_IDS:
        model = default_model
    default = resolve(None, config.voice, model)
    chosen = extra.get(VOICE_KEYS[model], extra.get(PREFERENCE_KEY))
    return {"model": model, "models": list(MODEL_OPTIONS), "default_model": default_model,
            "voices": catalog(model), "default": default,
            "selected": resolve(chosen if isinstance(chosen, str) else None, default, model)}


async def user_selection(user_id: str, config) -> dict:
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        extra = ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}
    except Exception:  # a missing preference row or a read failure: the default
        extra = {}
    return selection(extra, config)


async def save_choice(user_id: str, config, *, model: str | None, voice: str | None) -> dict:
    """Save a model/voice atomically without losing another model's saved voice.

    Voice-only requests from older clients follow the user's effective model
    and do not pin the deployment's model as an explicit user preference.
    """
    if (model is None and voice is None) or (model is not None and model not in MODEL_IDS):
        raise ValueError("Pick a listed model or voice")
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    async with get_db_session() as session:
        row = await locked_preference(session, user_id)
        extra = dict(row.extra or {})
        legacy = extra.get(PREFERENCE_KEY)
        if isinstance(legacy, str):
            for old_model, key in VOICE_KEYS.items():
                if legacy in _by_id(old_model):
                    extra.setdefault(key, legacy)
        if model is not None:
            extra[MODEL_KEY] = model
        state = selection(extra, config)
        if voice is not None:
            if voice not in _by_id(state["model"]):
                raise ValueError("Pick a voice from this model")
            extra[VOICE_KEYS[state["model"]]] = voice
            state = selection(extra, config)
        extra[PREFERENCE_KEY] = state["selected"]
        row.extra = extra
        return state
