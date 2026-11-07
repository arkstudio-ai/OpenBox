"""What the agent is told when the person is in Light mode (docs/LIGHT_MODE_PLAN.md §4.4).

One stable fragment per session: it names the entry and its steps, so every
``question`` card can carry a ``step_id`` the App lights up, and it asks for the
one-decision-per-card manner the guided page is built around. Stable text keeps
the provider prompt cache warm across turns.
"""
from __future__ import annotations

from light.entries import light_entries
from light.examples import examples_fragment
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


async def shop_fragment(user_id: str | None) -> str:
    """The shop name and headline the owner gave in the first-video form, when there are any."""
    if not user_id:
        return ""
    try:
        from db.repository.preference_repo import PgPreferenceRepo
        row = await PgPreferenceRepo().get(user_id)
    except Exception:
        return ""
    extra = (row or {}).get("extra") or {}
    record = extra.get("first_video") if isinstance(extra, dict) else None
    if not isinstance(record, dict):
        return ""
    name = " ".join(str(record.get("shop_name") or "").split())[:40]
    headline = " ".join(str(record.get("headline") or "").split())[:40]
    if not name and not headline:
        return ""
    bits = [f"店名「{name}」" if name else "", f"主打「{headline}」" if headline else ""]
    return "这家店：" + "，".join(bit for bit in bits if bit) + "（老板在首条视频里填的）。"


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
    shop = await shop_fragment(user_id)
    if shop:
        parts.append(shop)
    parts.extend(filter(None, [await examples_fragment(user_id, session)]))  # 参考样例 (light/examples.py)
    return "\n".join(parts)


# --- the personal assistant's view of Light (docs/LIGHT_MODE_IMPL_PLAN.md §11) -----------------------

#: App route of a home entry's topic page (``Paths.lightTopic``).
TOPIC_PATH = "/light/topic/{entry_id}"
AVAILABILITY_LABELS = {"ok": "可用", "needs_desktop": "需要云电脑", "needs_plan": "需要套餐", "coming_soon": "即将上线"}


def availability_label(availability: str, binding: str | None = None) -> str:
    if availability == "needs_binding":
        return "需要绑定抖音" if (binding or "douyin") == "douyin" else f"需要绑定{binding}"
    return AVAILABILITY_LABELS.get(availability, availability)


async def entries_directory(user_id: str, workspace_id: str) -> dict:
    """The Light home as the assistant sees it: one row per entry, with this workspace's availability.

    The same ``entries_view`` the App renders, reduced to what a prompt or a read-only tool needs;
    the ``light.entries`` tool and ``assistant_section`` both read this, so they never disagree."""
    from light.mode import entries_view
    view = await entries_view(user_id, workspace_id)
    rows = []
    for entry in view["entries"]:
        binding = (entry.get("requires") or {}).get("binding")
        rows.append({
            "id": entry["id"], "title": entry["title"], "subtitle": entry.get("subtitle") or "",
            "availability": entry["availability"],
            "availability_label": availability_label(entry["availability"], binding),
            "url": TOPIC_PATH.format(entry_id=entry["id"]),
        })
    return {"ui_mode": view["ui_mode"], "plan_id": view["plan_id"], "desktop": view["desktop"], "entries": rows}


ASSISTANT_LIGHT_RULES = (
    "规则：",
    "1. 做视频、做菜单、做海报、剪自己拍的素材这几类活，在文字对话里默认引导老板去首页对应入口，并给出直达链接"
    "（首页有样片和一步一卡的引导，比在这里聊更省事）；老板明确说「你直接做」「不用去首页」，或者说要新建一个对话、"
    "新开一个任务去做，就用 tasks.submit 派活，并把 work_type 选成对应的 Light 工作类型"
    "（light_video / menu / poster / footage_edit），帮手会在云端完成，不需要云电脑。"
    "电话里（消息带 assistant:voice-turn 来电上下文）老板点不了链接：电话里提的这几类活一律直接 tasks.submit 派活，不引导首页。"
    "引导去首页时要说清这件事还没开始做、没有交给帮手，不能说「已交办」「正在处理」。",
    "2. 标着「需要云电脑」的入口，在这个账号没有云电脑时，说明需要先开通云电脑（我的 → 云电脑），"
    "不要派帮手去撞；标着「需要绑定抖音」的先引导去绑定。",
    "3. 不出现模型名、档位的内部名称、积分估算或任何技术词；价格由界面按整条显示。"
    "老板点名要用某个模型或档位做 Light 入口的活：帮手用的是页面上已选好的那款，电话和对话里都换不了，"
    "如实说一句「用页面上选好的那款做」就派活或确认，不把模型要求当成改方案的理由转给帮手，也不为此再问老板。",
    "目录有变动时可用 light.entries 工具核对。",
)


async def assistant_section(user_id: str | None, workspace_id: str | None) -> str:
    """What the personal assistant is told about the interface mode, in every mode.

    Pro: one line naming the mode, so the main line's behaviour is unchanged. Light: that line, the home
    directory (title, purpose, this account's availability, deep link) and the three rules above."""
    mode = await ui_mode(user_id) if user_id else "pro"
    if mode != "light":
        return "界面模式：专业。"
    lines = ["界面模式：Light（功能引导）。这个人是第一次用 AI 工作台的小店老板，App 首页是下面这些入口。",
             "首页目录（标题｜用途｜当前账号状态｜直达链接）："]
    if workspace_id:
        directory = await entries_directory(user_id, workspace_id)
        for row in directory["entries"]:
            lines.append(f"- {row['title']}｜{row['subtitle']}｜{row['availability_label']}｜{row['url']}")
    else:
        lines.append("- （目录暂不可用，用 light.entries 工具读取）")
    lines.extend(ASSISTANT_LIGHT_RULES)
    return "\n".join(lines)
