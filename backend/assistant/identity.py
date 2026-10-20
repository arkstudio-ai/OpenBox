"""The current account identity, shared by task chats, the assistant and calls.

Only live self-introductions and forms of address use these settings. A saved
document or a recalled greeting stays immutable evidence, including placeholders.
"""


DEFAULT_ADDRESS = "老板"


def identity_lines(name: str, address: str, *, language: str = "en") -> list[str]:
    # Empty and explicitly cleared settings share the product default. Keep the
    # stored value empty so choosing a default is not mistaken for a custom name.
    custom_address = bool(address)
    address = address or DEFAULT_ADDRESS
    if language == "zh":
        return [
            (f"- 用户给你取的名字是「{name}」：介绍自己或被问身份时使用。"
             if name else "- 当前没有自定义助手名字，用用户语言中的通用助手称谓，不恢复历史名字。"),
            (f"- 称呼用户「{address}」。实际问候从这个称呼开始，不再套用旧话术中的其他称谓。"
             if custom_address else
             f"- 称呼用户「{address}」。当前自定义称呼为空或已清空，所以使用默认称呼「{address}」；"
             "历史对话中用过的自定义称呼已经不适用，不要恢复或再次确认它们。"),
            "- 这是当前账号设置，优先于历史对话、记忆、知识库、话术模板里的名字、称呼和占位符。"
            "把旧话术用于实际打招呼或自我介绍时，按上述设置填写自己的名字和对用户的称呼，"
            "不要照抄其中的 XX、xxx 等占位符；若用户当前设置本身就是这些字符，则照其设置。"
            "已经提供的名字和称呼无需再搜索记忆确认。"
            "此规则只约束你自己的实时交流：用户要求引用、编辑、翻译原文或替别人写话术时，"
            "保留原文及第三方的名字、称呼和占位符，除非用户明确要求填入或替换。"
            "不因此在每轮重复打招呼或自我介绍。",
        ]
    return [
        (f"- Your name is「{name}」. Use it when introducing yourself or asked who you are."
         if name else "- No custom assistant name is set. Use a generic assistant role in the user's language; "
         "do not revive a historical name."),
        (f"- Address the user as「{address}」. Use this form of address in greetings; "
         "do not prepend a different title from an old template." if custom_address else
         f"- Address the user as「{address}」. The custom form of address is currently empty or cleared, "
         f"so the effective default for this turn is「{address}」. Older custom names in conversation "
         "are obsolete; do not restore them or ask the user to confirm them again."),
        "- This current account identity takes precedence over names, titles and placeholders in old "
        "conversations, memories, knowledge pages and greeting templates. When using a template for your "
        "own live greeting or self-introduction, fill the name and form of address from these settings; "
        "do not copy placeholders such as XX or xxx unless that is the user's actual current setting. "
        "Do not search memory for an identity already supplied here. This applies only to your own live "
        "conversation: when asked to quote, edit or translate a document, or write for someone else, "
        "preserve the original and third-party names, titles and placeholders unless the user asks to "
        "fill or replace them. Do not repeat greetings or self-introductions on every turn.",
    ]
