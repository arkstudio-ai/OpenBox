"""Model IDs whose realtime protocols and voice catalogues we support."""

OMNI_MODEL = "qwen3.8-omni-flash-realtime"
AUDIO_MODEL = "qwen-audio-3.1-realtime-plus"


def is_audio(model: str) -> bool:
    return model == AUDIO_MODEL
