"""The duplicate-task gate's title match: bracketed variants and re-runs of the same work are the same task."""
import pytest

from assistant.commands import similar_task_title


@pytest.mark.parametrize("first, second", [
    ("艾薇兰小红瓶调研与1分钟国风视频策划", "艾薇兰小红瓶1分钟国风视频策划与制作"),
    ("艾薇兰小红瓶1分钟国风视频制作", "艾薇兰小红瓶1分钟国风视频制作（标准档）"),
    ("雨后公园浪漫偶像剧40秒短片制作", "雨后公园浪漫偶像剧40秒短片制作（MiniMax H3重新生成）"),
    ("30秒武侠剧情打斗短片（对峙交谈+打斗）", "30秒武侠剧情打斗短片（角色三视图与特写重新生成）"),
    ("酒吧四位美女写实走动视频", "酒吧四位美女写实走动视频（全身竖屏）"),
    ("生成荷叶奶蛙动态短视频", "生成荷叶奶蛙动态短视频"),
])
def test_same_work_is_similar(first, second):
    assert similar_task_title(first, second)
    assert similar_task_title(second, first)


@pytest.mark.parametrize("first, second", [
    ("生成小猫图片", "生成小狗图片"),
    ("生成机甲霸王龙图片", "生成小女孩图片"),
    ("导出本次通话对话记录Word文档", "45秒相机选购指南视频制作"),
    ("越南风味砂锅焗海鲜30秒展示视频制作", "15秒AI赋能各行各业口播视频制作"),
    ("", "生成小猫图片"),
    ("图片", "视频"),
])
def test_different_work_is_not_similar(first, second):
    assert not similar_task_title(first, second)


def test_acknowledge_tool_is_registered_and_submit_has_explicit_new_flag():
    from assistant.reporting import ASSISTANT_TOOLS
    from tool.assistant_tools import SubmitArgs, assistant_tools
    ids = {tool.id for tool in assistant_tools}
    assert "tasks.acknowledge_effects" in ids
    assert "tasks.acknowledge_effects" in ASSISTANT_TOOLS
    assert SubmitArgs.model_fields["new_despite_similar"].default is False
