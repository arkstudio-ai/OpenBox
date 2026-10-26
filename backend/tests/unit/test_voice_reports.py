"""Task results reported while a call is on reach the call (voice/reports.py)."""
from types import SimpleNamespace

import pytest

from voice import reports


def row(identity, state="settled", outcome="succeeded", message="msg-1", task="task-1", origin="task_result"):
    return SimpleNamespace(id=identity, state=state, outcome=outcome, result_message_id=message,
                           origin=origin, origin_ref={"task_id": task})


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


async def test_watch_own_reply_too_so_a_timed_out_waiter_does_not_lose_the_result(monkeypatch):
    rows = []
    async def recent(self):
        return rows
    async def title(self, item):
        return ""
    async def text(*args):
        return "已更新素材说明。"
    monkeypatch.setattr(reports.ReportWatcher, "_recent", recent)
    monkeypatch.setattr(reports.ReportWatcher, "_title", title)
    monkeypatch.setattr("voice.assistant_link.reply_text", text)
    watcher = reports.ReportWatcher(user_id="u", main_session_id="main", call_id="call-1")
    await watcher.start()
    typed, voice = row("typed", origin="human"), row("voice", origin="human")
    typed.origin_ref, voice.origin_ref = {}, {"voice_call_id": "call-1"}
    other_call = row("other-call", origin="human")
    other_call.origin_ref = {"voice_call_id": "other-call"}
    resumed = row("resumed", origin="system_recovery")
    resumed.origin_ref = {"entrypoint": "question_answer"}
    rows.extend([typed, voice, other_call, resumed])
    found = await watcher.poll()
    assert [report.inbox_id for report in found] == ["resumed", "voice"]
    assert await watcher.poll() == []


async def test_transient_read_error_does_not_consume_any_report_in_the_batch(monkeypatch):
    rows, fail = [], True
    async def recent(self):
        return rows
    async def title(self, item):
        return ""
    async def text(session, message, user):
        if message == "second" and fail:
            raise TimeoutError("temporary read failure")
        return message
    monkeypatch.setattr(reports.ReportWatcher, "_recent", recent)
    monkeypatch.setattr(reports.ReportWatcher, "_title", title)
    monkeypatch.setattr("voice.assistant_link.reply_text", text)
    watcher = reports.ReportWatcher(user_id="u", main_session_id="main")
    await watcher.start()
    rows.extend([row("1", message="first"), row("2", message="second")])
    with pytest.raises(TimeoutError):
        await watcher.poll()
    fail = False
    assert [value.text for value in await watcher.poll()] == ["first", "second"]
    assert await watcher.poll() == []


async def test_typed_reply_keeps_running_status_and_skips_superseded_card_speech(monkeypatch):
    rows = []
    async def recent(self):
        return rows
    async def title(self, item):
        return "清单"
    async def text(*args):
        return "已经交办，清单做好后通知你。"
    async def running(*args):
        return [{"task_id": "task-1", "title": "开业清单"}]
    async def decisions(session, user, workspace, inbox):
        return {"q1": "v1"}, inbox == "stale"
    monkeypatch.setattr(reports.ReportWatcher, "_recent", recent)
    monkeypatch.setattr(reports.ReportWatcher, "_title", title)
    monkeypatch.setattr("voice.assistant_link.reply_text", text)
    monkeypatch.setattr("voice.assistant_link.running_tasks", running)
    monkeypatch.setattr("voice.decisions.for_turn", decisions)
    watcher = reports.ReportWatcher(user_id="u", main_session_id="main", workspace_id="w")
    await watcher.start()
    rows.extend([row("fresh"), row("stale")])
    [found] = await watcher.poll()
    assert found.inbox_id == "fresh" and found.running[0]["title"] == "开业清单"
    assert found.decision_versions == {"q1": "v1"}


async def test_running_report_is_delivered_as_pending_work_not_completion():
    import json
    from voice import phrases
    from tests.unit.test_voice_routing import make, greeted
    bridge, provider, _ = make()
    await greeted(bridge, provider)
    await bridge.report("清单", "已经交办，做好后通知你。", "report-1", running=[{"title": "开业清单"}])
    payload = json.loads(provider.commands("note")[0][1].removeprefix(phrases.NOTE_PREFIX["zh"]))["results"][0]
    assert payload["status"] == "running" and "尚未取得成品" in payload["state_note"]
    assert payload["running_tasks"] == ["开业清单"]
    assert provider.response_purposes()[-1][1] == phrases.notice_instructions("zh")
