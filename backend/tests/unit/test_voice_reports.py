"""Task results reported while a call is on reach the call (voice/reports.py)."""
from types import SimpleNamespace

from voice import reports


def row(identity, state="settled", outcome="succeeded", message="msg-1", task="task-1"):
    return SimpleNamespace(id=identity, state=state, outcome=outcome, result_message_id=message,
                           origin_ref={"task_id": task})


async def test_only_reports_finished_after_the_call_began_are_told_once(monkeypatch):
    rows = [row("inbox_1"), row("inbox_2", state="claimed")]  # one told before the call, one being written

    async def recent(self):
        return list(rows)

    async def title(self, item):
        return "制作iPhone 18口播视频"

    async def reply_text(session_id, message_id, user_id):
        return {"msg-2": "打开抖音创作者中心受阻。", "msg-3": ""}.get(message_id, "旧的汇报")
    monkeypatch.setattr(reports.ReportWatcher, "_recent", recent)
    monkeypatch.setattr(reports.ReportWatcher, "_title", title)
    monkeypatch.setattr("voice.assistant_link.reply_text", reply_text)
    watcher = reports.ReportWatcher(user_id="u1", main_session_id="main-1")
    await watcher.start()
    assert await watcher.poll() == []
    rows[1] = row("inbox_2", message="msg-2")          # its report is written now
    rows.append(row("inbox_3", message="msg-3"))        # an empty reply is nothing to say
    rows.append(row("inbox_4", outcome="error"))        # a failed report turn is not a result
    # The task reporting comes along: a request of the call passed to it is answered by this report.
    assert await watcher.poll() == [reports.Report("inbox_2", "制作iPhone 18口播视频", "打开抖音创作者中心受阻。", "task-1")]
    assert await watcher.poll() == []
