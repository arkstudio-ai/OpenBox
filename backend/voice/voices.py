"""The voices a user can pick for calls (Settings → 语音通话).

Every voice here was checked live on ``qwen3.8-omni-flash-realtime`` on
2026-10-07: it speaks, in Mandarin and (for the English ones) in English.
The model's voice list is longer (56, https://help.aliyun.com/zh/model-studio/omni-voice-list,
"Qwen3.8-Omni-Flash-Realtime"); the rest are dialects (四川话、粤语…), Taiwan or
Hong Kong accents, character voices and foreign personas, which do not suit a
personal assistant's front desk. Voices listed for older omni models
(Cherry, Ethan, Chelsie…) are refused at the first reply, so they are not here.

A user's choice is stored in their preferences (``extra.assistant_voice``);
an unknown or removed id falls back to ``voice.voice`` in the config.
"""
import re
from dataclasses import asdict, dataclass
from pathlib import Path

PREFERENCE_KEY = "assistant_voice"
#: A 4 s preview per voice ("你好，我是你的私人助理，有事随时叫我。", or English for the
#: English ones), recorded from the model itself on 2026-10-07, AAC at 32 kbps.
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


def catalog() -> list[dict]:
    return [asdict(voice) for voice in VOICES]


def sample_path(voice_id: str) -> Path | None:
    """The preview file of one of our voices, or None."""
    if voice_id not in BY_ID:
        return None
    path = SAMPLES / (re.sub(r"[^A-Za-z0-9]+", "_", voice_id).strip("_") + ".m4a")
    return path if path.is_file() else None


def resolve(chosen: str | None, default: str) -> str:
    """The user's voice if it is one of ours, else the configured one."""
    return chosen if chosen in BY_ID else default


async def chosen_voice(user_id: str) -> str | None:
    """The voice saved in the user's preferences (may be None or no longer offered)."""
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        extra = ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}
    except Exception:  # a missing preference row or a read failure: the default
        return None
    value = extra.get(PREFERENCE_KEY)
    return value if isinstance(value, str) else None


async def save_voice(user_id: str, voice: str) -> None:
    if voice not in BY_ID:
        raise ValueError("Unknown voice")
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(user_id, extra={PREFERENCE_KEY: voice})
