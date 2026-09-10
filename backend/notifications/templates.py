"""Short lock-screen copy shared by business events and admin previews."""
import unicodedata

TEMPLATES = {
    'system_test': ('通知测试', '手机通知正常。', 'Notification test', 'Phone notifications work.'),
    'task_completed': ('任务完成', '《{name}》已完成，查看结果。', 'Task complete', '“{name}” is ready. View results.'),
    'task_failed': ('任务失败', '《{name}》未完成，查看原因。', 'Task failed', '“{name}” failed. Review details.'),
    'input_required': ('等待回答', '《{name}》需要你的回答。', 'Answer needed', '“{name}” needs your answer.'),
    'approval_required': ('等待确认', '《{name}》需要你的确认。', 'Approval needed', '“{name}” needs your approval.'),
    'cron_completed': ('定时任务完成', '《{name}》有新结果。', 'Scheduled result', '“{name}” has new results.'),
    'cron_failed': ('定时任务失败', '《{name}》重试失败，请检查。', 'Scheduled task failed', '“{name}” stopped retrying. Review it.'),
    'platform_auth_expired': ('需要重新授权', '{name} 授权已失效，请重新登录。', 'Sign-in needed', '{name} authorization expired. Sign in again.'),
    'publish_done': ('发布成功', '《{name}》已确认发布。', 'Published', '“{name}” is confirmed published.'),
    'publish_failed': ('发布失败', '《{name}》发布失败，请检查。', 'Publish failed', '“{name}” failed to publish. Review it.'),
}
PLATFORMS = {'douyin': ('抖音', 'Douyin'), 'douyin_creator': ('抖音创作者中心', 'Douyin Creator'),
             'xiaohongshu': ('小红书', 'Xiaohongshu'), 'bilibili': ('哔哩哔哩', 'Bilibili')}


def compact(value: str, limit: int) -> str:
    # Strip control/format characters, flatten whitespace, then include the
    # ellipsis in the limit. Titles never carry full prompts or tool output.
    value = ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def render_template(kind: str, name: str = '', locale: str = 'zh-CN') -> tuple[str, str]:
    zh = locale.startswith('zh')
    if kind == 'platform_auth_expired':
        name = PLATFORMS.get(name, (name, name))[0 if zh else 1]
    name = compact(name or ('任务' if zh else 'Task'), 16 if zh else 32)
    title_zh, body_zh, title_en, body_en = TEMPLATES[kind]
    return (title_zh if zh else title_en, (body_zh if zh else body_en).format(name=name))


def test_template(kind: str, locale: str) -> tuple[str, str]:
    zh = locale.startswith('zh')
    name = ('示例作品' if zh else 'Sample work') if kind.startswith('publish_') else ('示例任务' if zh else 'Sample task')
    if kind == 'platform_auth_expired': name = 'douyin'
    title, body = render_template(kind, name, locale)
    return ((('测试 · ' if zh else 'Test · ') + title) if kind != 'system_test' else title, body)
