"""MiniMax-M3.1-Flash behind the 专业 tier (2026-11-14): priced at the Bailian list price, and a reply made of raw
tool-call markup is never taken as a conversation summary."""
from decimal import Decimal

from billing import pricing


def test_minimax_m31_flash_is_sold_at_its_list_cost():
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0}
    quote = pricing.quote("openai/MiniMax-M3.1-Flash", usage, rates=pricing.base_catalogue())
    assert quote.credits == Decimal(5) and quote.cost == Decimal(5)
    assert quote.snapshot["model"] == "minimax-m3.1-flash"


def test_the_model_takes_no_reasoning_variant():
    from agent.llm import reasoning_profile
    assert reasoning_profile("openai/MiniMax-M3.1-Flash").variants == ()


def test_raw_tool_call_markup_is_not_a_summary():
    from agent.compaction import TOOL_MARKUP
    leaked = ']<]minimax[>[<tool_call>content_invocation>\n{"name": "tasks_submit", "arguments": {"title": "制作一张海报"}}'
    assert TOOL_MARKUP.search(leaked)
    assert TOOL_MARKUP.search("先说两句 <tool_call>{}</tool_call>")
    assert not TOOL_MARKUP.search("用户要一张周末小龙虾八折海报，已通过 tasks_submit 提交，任务 T1 已受理；15 秒视频尚未安排。")
