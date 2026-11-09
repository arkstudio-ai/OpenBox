"""Inbox rows follow the language of the request, not the one they were written in (templates.relocalize)."""
import pytest

from notifications.templates import recover, relocalize, render_template


@pytest.mark.parametrize("kind,name", [("task_completed", "周末海报"), ("input_required", "做菜单"),
                                       ("first_video_ready", ""), ("platform_auth_expired", "douyin")])
def test_recover_reads_the_name_and_language_back_out_of_a_rendered_row(kind, name):
    for lang in ("zh-CN", "en-US", "vi-VN"):
        title, body = render_template(kind, name, lang)
        assert recover(kind, title, body) == (name, lang)


def test_relocalize_renders_a_chinese_row_in_vietnamese_and_back():
    title, body = render_template("task_completed", "周末海报", "zh-CN")
    assert relocalize("task_completed", title, body, "vi-VN") == render_template("task_completed", "周末海报", "vi-VN")
    vi_title, vi_body = render_template("task_completed", "周末海报", "vi-VN")
    assert relocalize("task_completed", vi_title, vi_body, "zh-CN") == (title, body)


def test_relocalize_keeps_the_row_when_the_language_matches_or_is_unknown():
    title, body = render_template("input_required", "做菜单", "zh-CN")
    assert relocalize("input_required", title, body, "zh-CN") == (title, body)
    assert relocalize("input_required", title, body, None) == (title, body)


def test_relocalize_leaves_free_text_test_and_unknown_rows_alone():
    assert relocalize("skill_pending", "有新的技能待审核", "《x》由 a 提交，等待审核。", "en-US") == \
        ("有新的技能待审核", "《x》由 a 提交，等待审核。")
    assert relocalize("task_completed", "测试 · 任务完成", "《示例任务》已完成，查看结果。", "en-US") == \
        ("测试 · 任务完成", "《示例任务》已完成，查看结果。")
    assert relocalize("task_completed", "任务完成", "something else entirely", "en-US") == \
        ("任务完成", "something else entirely")


def test_relocalize_maps_the_default_name_and_platform_names_across_languages():
    title, body = render_template("task_completed", "", "zh-CN")  # 《任务》
    assert relocalize("task_completed", title, body, "en-US") == render_template("task_completed", "", "en-US")
    title, body = render_template("platform_auth_expired", "douyin", "zh-CN")  # 抖音
    assert relocalize("platform_auth_expired", title, body, "en-US") == \
        render_template("platform_auth_expired", "douyin", "en-US")
