"""Short lock-screen copy shared by business events and admin previews.

One row per kind, three languages (docs/LIGHT_MODE_I18N_PLAN.md §2.4). ``locale``
is normalised with ``core.ui_language.normalize`` (``zh*`` → zh-CN, ``vi*`` →
vi-VN, anything else → en-US) and a string missing from a language is taken
along ``core.ui_language.FALLBACK`` (vi-VN → en-US).
"""
import unicodedata

from core.ui_language import DEFAULT, chain, normalize

TEMPLATES = {
    'assistant_result_ready': {
        'zh-CN': ('任务结果已保存', '可在个人助理中查看任务结果和汇报进度。'),
        'en-US': ('Task result saved', 'View the result and report status in your personal assistant.'),
        'vi-VN': ('Đã lưu kết quả công việc', 'Xem kết quả và tiến độ báo cáo trong trợ lý cá nhân.')},
    'assistant_result_failed': {
        'zh-CN': ('任务未能完成', '结果已保存，可在个人助理中查看原因和汇报进度。'),
        'en-US': ('Task could not finish', 'The result is saved. View details and report status in your personal assistant.'),
        'vi-VN': ('Công việc chưa hoàn thành', 'Kết quả đã được lưu. Xem nguyên nhân và tiến độ báo cáo trong trợ lý cá nhân.')},
    'assistant_result_stopped': {
        'zh-CN': ('任务已停止', '结果已保存，可在个人助理中查看。'),
        'en-US': ('Task stopped', 'The result is saved. View it in your personal assistant.'),
        'vi-VN': ('Công việc đã dừng', 'Kết quả đã được lưu. Xem trong trợ lý cá nhân.')},
    'system_test': {
        'zh-CN': ('通知测试', '手机通知正常。'),
        'en-US': ('Notification test', 'Phone notifications work.'),
        'vi-VN': ('Kiểm tra thông báo', 'Thông báo trên điện thoại hoạt động bình thường.')},
    'task_completed': {
        'zh-CN': ('任务完成', '《{name}》已完成，查看结果。'),
        'en-US': ('Task complete', '“{name}” is ready. View results.'),
        'vi-VN': ('Công việc hoàn thành', '“{name}” đã xong. Xem kết quả.')},
    'task_failed': {
        'zh-CN': ('任务失败', '《{name}》未完成，查看原因。'),
        'en-US': ('Task failed', '“{name}” failed. Review details.'),
        'vi-VN': ('Công việc thất bại', '“{name}” chưa hoàn thành. Xem nguyên nhân.')},
    'input_required': {
        'zh-CN': ('等待回答', '《{name}》需要你的回答。'),
        'en-US': ('Answer needed', '“{name}” needs your answer.'),
        'vi-VN': ('Cần bạn trả lời', '“{name}” cần câu trả lời của bạn.')},
    'approval_required': {
        'zh-CN': ('等待确认', '《{name}》需要你的确认。'),
        'en-US': ('Approval needed', '“{name}” needs your approval.'),
        'vi-VN': ('Cần bạn xác nhận', '“{name}” cần bạn xác nhận.')},
    'cron_completed': {
        'zh-CN': ('定时任务完成', '《{name}》有新结果。'),
        'en-US': ('Scheduled result', '“{name}” has new results.'),
        'vi-VN': ('Công việc định kỳ hoàn thành', '“{name}” có kết quả mới.')},
    'cron_failed': {
        'zh-CN': ('定时任务失败', '《{name}》未完成，请检查。'),
        'en-US': ('Scheduled task failed', '“{name}” failed. Review details.'),
        'vi-VN': ('Công việc định kỳ thất bại', '“{name}” chưa hoàn thành, hãy kiểm tra.')},
    'platform_auth_expired': {
        'zh-CN': ('需要重新授权', '{name} 授权已失效，请重新登录。'),
        'en-US': ('Sign-in needed', '{name} authorization expired. Sign in again.'),
        'vi-VN': ('Cần đăng nhập lại', 'Ủy quyền {name} đã hết hạn. Hãy đăng nhập lại.')},
    'publish_done': {
        'zh-CN': ('发布成功', '《{name}》已确认发布。'),
        'en-US': ('Published', '“{name}” is confirmed published.'),
        'vi-VN': ('Đã đăng', '“{name}” đã được xác nhận đăng.')},
    'publish_failed': {
        'zh-CN': ('发布失败', '《{name}》发布失败，请检查。'),
        'en-US': ('Publish failed', '“{name}” failed to publish. Review it.'),
        'vi-VN': ('Đăng thất bại', '“{name}” đăng không thành công, hãy kiểm tra.')},
    'first_video_ready': {
        'zh-CN': ('你的第一条视频做好了', '15 秒竖屏短片已出片，点开看看，还能一键发到抖音。'),
        'en-US': ('Your first video is ready', 'The 15-second vertical clip is done. Open it, then post it to Douyin in one tap.'),
        'vi-VN': ('Video đầu tiên của bạn đã xong', 'Clip dọc 15 giây đã hoàn thành. Mở xem và đăng lên Douyin chỉ với một chạm.')},
    'first_video_failed': {
        'zh-CN': ('第一条视频没做成', '这次生成没有成功，点开看看怎么办。'),
        'en-US': ('Your first video did not finish', 'Generation did not succeed. Open the chat to continue.'),
        'vi-VN': ('Video đầu tiên chưa làm được', 'Lần tạo này không thành công. Mở cuộc trò chuyện để tiếp tục.')},
    'footage_edit_ready': {
        'zh-CN': ('你拍我剪：成片好了', '你拍的素材剪好了，点开看看，可以保存或发到抖音。'),
        'en-US': ('Your cut is ready', 'Your footage has been cut. Open it to save or post it.'),
        'vi-VN': ('Bạn quay tôi dựng: video đã xong', 'Clip bạn quay đã được dựng xong. Mở xem để lưu hoặc đăng lên Douyin.')},
    'footage_edit_failed': {
        'zh-CN': ('你拍我剪：没剪成', '这次合成没有成功，点开看看怎么办。'),
        'en-US': ('Your cut did not finish', 'The cut did not succeed. Open the chat to continue.'),
        'vi-VN': ('Bạn quay tôi dựng: chưa dựng được', 'Lần ghép này không thành công. Mở cuộc trò chuyện để tiếp tục.')},
    'trajectory_turn_failed': {
        'zh-CN': ('有一轮对话失败', '{name}，点开看哪一步出了问题。'),
        'en-US': ('A conversation turn failed', '{name}. See which step broke.'),
        'vi-VN': ('Một lượt trò chuyện thất bại', '{name}. Xem bước nào gặp lỗi.')},
}
PLATFORMS = {
    'douyin': {'zh-CN': '抖音', 'en-US': 'Douyin', 'vi-VN': 'Douyin'},
    'douyin_creator': {'zh-CN': '抖音创作者中心', 'en-US': 'Douyin Creator', 'vi-VN': 'Douyin Creator'},
    'xiaohongshu': {'zh-CN': '小红书', 'en-US': 'Xiaohongshu', 'vi-VN': 'Xiaohongshu'},
    'bilibili': {'zh-CN': '哔哩哔哩', 'en-US': 'Bilibili', 'vi-VN': 'Bilibili'},
}
DEFAULT_NAME = {'zh-CN': '任务', 'en-US': 'Task', 'vi-VN': 'Công việc'}
SAMPLE_WORK = {'zh-CN': '示例作品', 'en-US': 'Sample work', 'vi-VN': 'Tác phẩm mẫu'}
SAMPLE_TASK = {'zh-CN': '示例任务', 'en-US': 'Sample task', 'vi-VN': 'Công việc mẫu'}
TEST_PREFIX = {'zh-CN': '测试 · ', 'en-US': 'Test · ', 'vi-VN': 'Thử · '}
#: Name length per language: CJK titles carry more per character.
NAME_LIMIT = {'zh-CN': 16}


def language(locale: str | None) -> str:
    """A device locale or an interface language → one of the three; missing or empty → zh-CN (the old default)."""
    return normalize(locale) or DEFAULT if locale else DEFAULT


def pick(table: dict, lang: str):
    """``table[lang]``, else the first fallback that has it (vi-VN → en-US), else zh-CN."""
    for candidate in (*chain(lang), DEFAULT):
        if candidate in table:
            return table[candidate]
    raise KeyError(lang)


def compact(value: str, limit: int) -> str:
    # Strip control/format characters, flatten whitespace, then include the
    # ellipsis in the limit. Titles never carry full prompts or tool output.
    value = ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def render_template(kind: str, name: str = '', locale: str = 'zh-CN') -> tuple[str, str]:
    lang = language(locale)
    if kind == 'platform_auth_expired':
        name = pick(PLATFORMS[name], lang) if name in PLATFORMS else name
    name = compact(name or pick(DEFAULT_NAME, lang), NAME_LIMIT.get(lang, 32))
    title, body = pick(TEMPLATES[kind], lang)
    return title, body.format(name=name)


def recover(kind: str, title: str, body: str) -> tuple[str, str] | None:
    """The ``(name, lang)`` a stored inbox row was rendered from, or ``None`` when it is not one of these templates.

    Rows keep only the rendered title and body, so the name is read back out of the body; a platform
    name goes back to its key so it can be rendered in another language, and the "no name" default
    goes back to an empty name for the same reason. Test rows (prefixed title) and free-text rows stay
    as they are."""
    table = TEMPLATES.get(kind)
    if not table:
        return None
    for lang, (t_title, t_body) in table.items():
        if title != t_title:
            continue
        if '{name}' not in t_body:
            if body == t_body:
                return '', lang
            continue
        head, tail = t_body.split('{name}', 1)
        if len(body) < len(head) + len(tail) or not body.startswith(head) or not body.endswith(tail):
            continue
        name = body[len(head):len(body) - len(tail)]
        if name == pick(DEFAULT_NAME, lang):
            name = ''
        if kind == 'platform_auth_expired':
            name = next((key for key, names in PLATFORMS.items() if name in names.values()), name)
        return name, lang
    return None


def relocalize(kind: str, title: str, body: str, locale: str | None) -> tuple[str, str]:
    """A stored row's title and body in ``locale``: re-rendered when the row came from a template, else as stored.

    The inbox is read far more often than it is written and the person may switch the interface language
    at any time (docs/LIGHT_MODE_I18N_PLAN.md §2.4), so the list follows the language of the request
    rather than the one in force when the row was written."""
    if not locale:
        return title, body
    found = recover(kind, title, body)
    if found is None or found[1] == language(locale):
        return title, body
    return render_template(kind, found[0], locale)


def test_template(kind: str, locale: str) -> tuple[str, str]:
    lang = language(locale)
    name = pick(SAMPLE_WORK if kind.startswith('publish_') else SAMPLE_TASK, lang)
    if kind == 'platform_auth_expired': name = 'douyin'
    title, body = render_template(kind, name, locale)
    return ((pick(TEST_PREFIX, lang) + title) if kind != 'system_test' else title, body)
