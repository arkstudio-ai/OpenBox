"""Public video preset labels shared by task choices and spoken presentation."""
import re

VIDEO_LABELS = {"fast": "快速", "low": "低", "medium": "中", "high": "高", "ultra": "极致"}


def video_label(tier: str, label: str = "") -> str:
    """Honor the same configured label the web and mobile pickers display."""
    value = label.strip() or VIDEO_LABELS[tier]
    return value + "档" if re.search(r"[\u4e00-\u9fff]", value) and not value.endswith("档") else value
