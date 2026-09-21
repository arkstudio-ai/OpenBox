"""The 联调 script drives the documented sequence and checks every contract point.

A mock server replays doc §9 (script → card → storyboard card → final file)
so the polling, card answering, replay and download logic is verified without
a deployment.
"""
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "gaode_flow_e2e", Path(__file__).resolve().parents[2] / "scripts" / "gaode_flow_e2e.py",
)
flow = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = flow  # dataclasses resolve postponed annotations via sys.modules
_spec.loader.exec_module(flow)


class FakeGaodeServer:
    """State machine over the nine endpoints, in the doc's order."""

    def __init__(self):
        self.polls = 0
        self.answers = {}
        self.sent = {}
        self.busy = False
        self.log = []

    def _json(self, status, body, **headers):
        return httpx.Response(status, json=body, headers={"X-Request-Id": "req_test", **headers})

    def _error(self, status, code):
        return self._json(status, {"error": {"code": code, "message": code, "request_id": "req_test"}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.log.append((method, path))
        auth = request.headers.get("authorization", "")
        if auth != "Bearer obx_sk_good":
            return self._error(401, "UNAUTHORIZED")
        if method == "POST" and path == "/v1/files":
            assert b'filename="road.mp4"' in request.content
            return self._json(201, {"id": "fil_A", "filename": "road.mp4", "mime_type": "video/mp4",
                                    "size": 5, "duration_s": None, "created_at": "2026-09-21T08:00:00Z"})
        if method == "POST" and path == "/v1/sessions":
            body = json.loads(request.content or b"{}")
            if body.get("quality") == "low":
                return self._error(400, "INVALID_REQUEST")
            return self._json(201, {"id": "ses_1", "title": body.get("title"), "status": "idle",
                                    "quality": body.get("quality", "medium"), "metadata": body.get("metadata", {}),
                                    "credits_used": "0", "created_at": "t", "updated_at": "t"})
        if method == "GET" and path == "/v1/sessions/ses_1":
            return self._json(200, {"id": "ses_1", "status": "busy" if self.busy else "idle", "credits_used": "128.5"})
        if method == "GET" and path.startswith("/v1/sessions/"):
            if path.endswith("/messages"):
                return self._messages(request)
            return self._error(404, "NOT_FOUND")
        if method == "POST" and path == "/v1/sessions/ses_1/messages":
            body = json.loads(request.content)
            cid = body["client_message_id"]
            if cid in self.sent:
                return self._json(202, self.sent[cid])
            if self.busy:
                return self._error(409, "SESSION_BUSY")
            self.last_send = body
            self.busy = True
            self.sent[cid] = {"session_id": "ses_1", "user_message_id": "msg_u1", "assistant_message_id": "msg_a1"}
            return self._json(202, self.sent[cid])
        if method == "POST" and path.startswith("/v1/sessions/ses_1/questions/"):
            qid = path.rsplit("/", 1)[-1]
            if qid in self.answers:
                return self._error(409, "INTERACTION_RESOLVED")
            self.answers[qid] = json.loads(request.content)["answers"]
            return self._json(200, {"ok": True})
        if method == "GET" and path == "/v1/files/fil_final/content":
            return httpx.Response(302, headers={"X-Request-Id": "req_test", "location": "https://oss.test/final.mp4?sig=1"})
        if method == "POST" and path == "/v1/sessions/ses_1/abort":
            return self._json(200, {"ok": True, "aborted": self.busy})
        return self._error(404, "NOT_FOUND")

    def _messages(self, request):
        assert request.url.params["after"] == "msg_u1"
        self.polls += 1
        parts = [{"type": "text", "id": "prt_1", "text": "好的，先给你写讲稿。"}]
        finish = None
        if self.polls >= 2:
            parts[0]["text"] += "\n\n## 讲稿\n…"
            parts.append({"type": "question", "id": "prt_2", "question_id": "qst_1",
                          "status": "answered" if "qst_1" in self.answers else "pending",
                          "expires_at": "2026-09-21T08:10:00Z",
                          "questions": [{"header": "成稿", "question": "讲稿如上，可以开始分镜吗？",
                                         "options": [{"label": "可以", "description": ""}, {"label": "需要修改", "description": ""}],
                                         "multiple": False, "custom": True}]})
        if "qst_1" in self.answers:
            parts.append({"type": "text", "id": "prt_3", "text": "分镜表与报价…"})
            parts.append({"type": "question", "id": "prt_4", "question_id": "qst_2",
                          "status": "answered" if "qst_2" in self.answers else "pending",
                          "expires_at": "2026-09-21T08:20:00Z",
                          "questions": [{"header": "报价", "question": "确认开始生成？",
                                         "options": [{"label": "确认（约 120 积分）", "description": "预估价"}],
                                         "multiple": False, "custom": False}]})
        if "qst_2" in self.answers:
            parts.append({"type": "file", "id": "prt_5", "file": {
                "id": "fil_final", "filename": "final_1080p.mp4", "mime_type": "video/mp4", "size": 99,
                "duration_s": None, "width": None, "height": None, "url": "https://oss.test/final.mp4?sig=0", "role": "final"}})
            finish = "stop"
            self.busy = False
        return self._json(200, {"data": [
            {"id": "msg_u1", "role": "user", "finish": "stop", "error": None, "parts": []},
            {"id": "msg_a1", "role": "assistant", "finish": finish, "error": None, "parts": parts},
        ], "has_more": False})


def test_full_flow_matches_the_document(tmp_path):
    server = FakeGaodeServer()
    transport = httpx.MockTransport(server.handle)
    material = tmp_path / "road.mp4"
    material.write_bytes(b"\x00\x00\x00\x00\x00")
    api = flow.HarnessClient("http://test/v1", "obx_sk_good", transport=transport)
    logged = []
    summary = flow.run_flow(
        api, text="25 秒口播", materials=[material], quality="high", title="t",
        metadata={"task_id": "t_1"}, choose_answers=flow.first_option_answers,
        client_message_id="t_1-1", poll_interval=0, deadline_s=60, sleep=lambda _s: None, log=logged.append,
    )
    assert summary.finish == "stop" and summary.error is None
    assert summary.session_id == "ses_1"
    assert summary.user_message_id == "msg_u1" and summary.assistant_message_id == "msg_a1"
    assert [c["question_id"] for c in summary.cards] == ["qst_1", "qst_2"]
    assert server.answers == {"qst_1": [["可以"]], "qst_2": [["确认（约 120 积分）"]]}
    assert server.last_send["attachments"] == ["fil_A"] and server.last_send["text"] == "25 秒口播"
    assert [f["id"] for f in summary.final_files] == ["fil_final"]
    assert summary.credits_used == "128.5"
    assert summary.checks == {
        "session_created_idle": True, "send_returns_both_ids": True, "busy_send_is_409": True,
        "assistant_id_matches_202": True, "idempotent_replay": True, "session_idle_after_turn": True,
        "download_link_refreshes": True, "final_file_delivered": True,
    }
    # Text is streamed once per growth, never re-printed.
    assert logged.count("好的，先给你写讲稿。") == 1
    assert "\n\n## 讲稿\n…" in logged
    assert server.polls >= 3
    assert ("POST", "/v1/sessions/ses_1/abort") not in server.log


def test_preflight_checks_key_and_error_shapes():
    server = FakeGaodeServer()
    transport = httpx.MockTransport(server.handle)
    api = flow.HarnessClient("http://test/v1", "obx_sk_good", transport=transport)
    checks = flow.preflight(api, "http://test/v1", transport=transport)
    assert checks == {"key_accepted": True, "not_found_shape": True, "bad_key_is_401": True, "low_quality_refused": True}


def test_deadline_aborts_and_reports_timeout(monkeypatch):
    server = FakeGaodeServer()
    server._messages = lambda request: server._json(200, {"data": [
        {"id": "msg_a1", "role": "assistant", "finish": None, "error": None, "parts": []}], "has_more": False})
    api = flow.HarnessClient("http://test/v1", "obx_sk_good", transport=httpx.MockTransport(server.handle))
    clock = iter([0, 0, 0, 0, 0, 0, 100, 100, 100, 100, 100, 100])
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(clock, 100))
    summary = flow.run_flow(
        api, text="x", materials=[], quality=None, title=None, metadata=None,
        choose_answers=flow.first_option_answers, client_message_id="t-1",
        poll_interval=0, deadline_s=10, sleep=lambda _s: None, log=lambda _m: None,
    )
    assert summary.finish == "timeout"
    assert ("POST", "/v1/sessions/ses_1/abort") in server.log
    assert summary.checks["final_file_delivered"] is False


def test_unexpected_status_names_the_request_id():
    def handler(request):
        return httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "boom", "request_id": "req_9"}},
                              headers={"X-Request-Id": "req_9"})
    api = flow.HarnessClient("http://test/v1", "obx_sk_good", transport=httpx.MockTransport(handler))
    with pytest.raises(flow.FlowError) as exc:
        api.create_session(title=None, quality=None, metadata=None)
    assert "500" in str(exc.value) and "req_9" in str(exc.value)


def test_interactive_answers_accept_numbers_and_free_text(monkeypatch):
    prompts = iter(["2", "1,3", "自己写的"])
    monkeypatch.setattr("builtins.input", lambda _p: next(prompts))
    card = {"questions": [
        {"question": "a", "options": [{"label": "A1"}, {"label": "A2"}], "multiple": False, "custom": False},
        {"question": "b", "options": [{"label": "B1"}, {"label": "B2"}, {"label": "B3"}], "multiple": True, "custom": False},
        {"question": "c", "options": [{"label": "C1"}], "multiple": False, "custom": True},
    ]}
    assert flow.interactive_answers(card) == [["A2"], ["B1", "B3"], ["自己写的"]]
    assert flow.scripted_answers([[["x"]]])(card) == [["x"]]
    assert flow.scripted_answers([])(card) == [["A1"], ["B1"], ["C1"]]
