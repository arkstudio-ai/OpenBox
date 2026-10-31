"""What the agent is told when the person is in Light mode (docs/LIGHT_MODE_PLAN.md §4.4).

One stable fragment per session: it names the entry and its steps, so every
``question`` card can carry a ``step_id`` the App lights up, and it asks for the
one-decision-per-card manner the guided page is built around. Stable text keeps
the provider prompt cache warm across turns.
"""
from __future__ import annotations

from light.entries import light_entries
from light.mode import ui_mode

LIGHT_MODE_TEXT = (
    "界面模式：Light（功能引导）。这个人是第一次用 AI 工作台的小店老板。规则："
    "每张卡只问一件事，能点选就用 question 的选项，不让对方打字；"
    "需要确认时只出一张合并的 review 卡（内容、时长、素材一起），不要拆成多张；"
    "不出现模型名、档位的内部名称、积分/秒或任何技术词；价格由界面按整条显示，你不要估算积分；"
    "工具过程不解释，直接给结果；对方插话先回答，再回到当前步骤。"
)


def entry_fragment(entry_id: str | None) -> str:
    entry = light_entries().entry(entry_id or "") if entry_id else None
    if entry is None:
        return ""
    steps = " → ".join(f"{step.id}({step.label})" for step in entry.steps)
    lines = [
        f"专题页：{entry.title}（entry_id={entry.id}），对应技能 `{entry.skill}`，开始前先读取它。",
        f"步骤条：{steps}。每次调用 question 都填 step_id（取其中一个 id），界面据此点亮步骤。",
        "开场卡已经发出，对方的第一句话是对它的回答；开场卡里点选的样子就是这次的风格，不要再问一遍。",
    ]
    return " ".join(lines)


async def light_mode_fragment(user_id: str | None, session) -> str:
    """Empty for ordinary chats; the Light rules (and the entry's steps) for guided ones."""
    if session is None or getattr(session, "kind", "normal") != "normal":
        return ""
    entry_id = getattr(session, "light_entry", None)
    mode = await ui_mode(user_id) if user_id else "pro"
    if mode != "light" and not entry_id:
        return ""
    parts = [LIGHT_MODE_TEXT]
    fragment = entry_fragment(entry_id)
    if fragment:
        parts.append(fragment)
    return "\n".join(parts)
