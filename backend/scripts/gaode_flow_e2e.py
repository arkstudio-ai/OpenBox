#!/usr/bin/env python
"""End-to-end rehearsal of the 高德 integration against a live /v1 deployment.

Plays the partner's whole sequence with one API key, exactly as their server
would (docs/external/OpenBox-Video-API-Gaode-v1.1.md §9):

    upload material → create session → send the brief → poll messages every
    2–3 s → answer each confirmation card → collect the final file → re-send
    the same client_message_id (must replay, not rerun) → read credits_used →
    refresh the download link

Nothing here imports the backend: it speaks HTTP only, so it also serves as
the contract check from the outside. Run from ``backend/``:

    uv run python scripts/gaode_flow_e2e.py --base-url https://<host>/v1 \\
        --key obx_sk_… --material ~/road.mp4 \\
        --text "帮我做一条 25 秒的五一杭州自驾攻略口播，用我上传的路况视频做素材"

    # answer cards by hand instead of taking the first option
    uv run python scripts/gaode_flow_e2e.py … --answers interactive

    # only check that the key works and errors have the documented shape
    uv run python scripts/gaode_flow_e2e.py --base-url … --key … --preflight-only

Exit code 0 means the final file arrived and every contract check passed.
The JSON summary on stdout lists ids, cards, files and timings for the 联调 log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

POLL_INTERVAL_SECONDS = 2.5
DEFAULT_DEADLINE_SECONDS = 20 * 60
KEY_ENV = "OPENBOX_API_KEY"


class FlowError(RuntimeError):
    """A step returned something the contract does not allow."""


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ─── HTTP client: one method per documented endpoint ───

class HarnessClient:
    def __init__(self, base_url: str, key: str, *, timeout: float = 120.0, transport=None):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _expect(response: httpx.Response, *statuses: int) -> httpx.Response:
        if response.status_code not in statuses:
            request_id = response.headers.get("X-Request-Id", "-")
            raise FlowError(
                f"{response.request.method} {response.request.url.path} → {response.status_code} "
                f"(expected {' or '.join(map(str, statuses))}); request_id={request_id}; "
                f"body={response.text[:500]}"
            )
        if "X-Request-Id" not in response.headers:
            raise FlowError(f"{response.request.url.path}: response lacks X-Request-Id")
        return response

    def upload(self, path: Path) -> dict:
        with path.open("rb") as handle:
            response = self.http.post("/files", files={"file": (path.name, handle)})
        return self._expect(response, 201).json()

    def create_session(self, *, title: str | None, quality: str | None, metadata: dict | None) -> dict:
        body = {k: v for k, v in {"title": title, "quality": quality, "metadata": metadata}.items() if v}
        return self._expect(self.http.post("/sessions", json=body), 201).json()

    def get_session(self, session_id: str) -> dict:
        return self._expect(self.http.get(f"/sessions/{session_id}"), 200).json()

    def send(self, session_id: str, *, text: str, attachments: list[str], client_message_id: str) -> dict:
        body = {"text": text, "attachments": attachments, "client_message_id": client_message_id}
        return self._expect(self.http.post(f"/sessions/{session_id}/messages", json=body), 202).json()

    def send_raw(self, session_id: str, body: dict) -> httpx.Response:
        return self.http.post(f"/sessions/{session_id}/messages", json=body)

    def poll(self, session_id: str, *, after: str, limit: int = 50) -> dict:
        params = {"after": after, "limit": limit}
        return self._expect(self.http.get(f"/sessions/{session_id}/messages", params=params), 200).json()

    def answer(self, session_id: str, question_id: str, answers: list[list[str]]) -> dict:
        url = f"/sessions/{session_id}/questions/{question_id}"
        return self._expect(self.http.post(url, json={"answers": answers}), 200).json()

    def reject(self, session_id: str, question_id: str) -> dict:
        url = f"/sessions/{session_id}/questions/{question_id}/reject"
        return self._expect(self.http.post(url), 200).json()

    def abort(self, session_id: str) -> dict:
        return self._expect(self.http.post(f"/sessions/{session_id}/abort"), 200).json()

    def download_location(self, file_id: str) -> str:
        response = self._expect(self.http.get(f"/files/{file_id}/content"), 302)
        location = response.headers.get("location")
        if not location:
            raise FlowError("/files/{id}/content answered 302 without a Location header")
        return location


# ─── Answer strategies ───

def first_option_answers(card: dict) -> list[list[str]]:
    """The recommended answer is the first option (doc §8.2)."""
    answers = []
    for question in card["questions"]:
        options = question.get("options") or []
        if not options:
            raise FlowError(f"card {card['question_id']} has a question without options and no scripted answer")
        answers.append([options[0]["label"]])
    return answers


def interactive_answers(card: dict) -> list[list[str]]:
    answers = []
    for index, question in enumerate(card["questions"], 1):
        _log(f"\n[{index}] {question.get('header') or ''} {question['question']}")
        for number, option in enumerate(question.get("options") or [], 1):
            desc = f"  — {option['description']}" if option.get("description") else ""
            _log(f"    {number}. {option['label']}{desc}")
        hint = "numbers separated by commas" if question.get("multiple") else "a number"
        if question.get("custom"):
            hint += ", or free text"
        raw = input(f"    answer ({hint}): ").strip()
        chosen: list[str] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token.isdigit() and 1 <= int(token) <= len(question.get("options") or []):
                chosen.append(question["options"][int(token) - 1]["label"])
            else:
                chosen.append(token)
        answers.append(chosen)
    return answers


def scripted_answers(script: list[list[list[str]]]):
    """Answers taken in order from ``--answers-json``; falls back to first option."""
    queue = list(script)

    def choose(card: dict) -> list[list[str]]:
        if queue:
            return queue.pop(0)
        return first_option_answers(card)

    return choose


# ─── Flow ───

@dataclass
class FlowSummary:
    session_id: str = ""
    user_message_id: str = ""
    assistant_message_id: str = ""
    uploaded: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    final_files: list[dict] = field(default_factory=list)
    finish: str | None = None
    error: dict | None = None
    credits_used: str = "0"
    polls: int = 0
    elapsed_s: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    downloaded: list[str] = field(default_factory=list)


def _assistant_turn(page: dict, assistant_id: str) -> dict | None:
    for message in page.get("data", []):
        if message.get("id") == assistant_id:
            return message
    for message in page.get("data", []):
        if message.get("role") == "assistant":
            return message
    return None


def run_flow(
    api: HarnessClient,
    *,
    text: str,
    materials: list[Path],
    quality: str | None,
    title: str | None,
    metadata: dict | None,
    choose_answers,
    client_message_id: str,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    deadline_s: float = DEFAULT_DEADLINE_SECONDS,
    download_dir: Path | None = None,
    sleep=time.sleep,
    log=_log,
) -> FlowSummary:
    started = time.monotonic()
    summary = FlowSummary()

    for path in materials:
        uploaded = api.upload(path)
        log(f"uploaded {path.name} → {uploaded['id']} ({uploaded['mime_type']}, {uploaded['size']} bytes)")
        summary.uploaded.append(uploaded)

    session = api.create_session(title=title, quality=quality, metadata=metadata)
    summary.session_id = session["id"]
    summary.checks["session_created_idle"] = session.get("status") == "idle"
    log(f"session {session['id']} quality={session.get('quality')} status={session.get('status')}")

    accepted = api.send(
        session["id"], text=text,
        attachments=[item["id"] for item in summary.uploaded],
        client_message_id=client_message_id,
    )
    summary.user_message_id = accepted["user_message_id"]
    summary.assistant_message_id = accepted["assistant_message_id"]
    summary.checks["send_returns_both_ids"] = bool(accepted.get("user_message_id") and accepted.get("assistant_message_id"))
    log(f"accepted user={accepted['user_message_id']} assistant={accepted['assistant_message_id']}")

    # The doc's busy rule: a second brief while the first runs is refused, not queued.
    busy = api.send_raw(session["id"], {"text": "second brief while busy", "client_message_id": f"{client_message_id}-busy"})
    summary.checks["busy_send_is_409"] = (
        busy.status_code == 409 and busy.json().get("error", {}).get("code") == "SESSION_BUSY"
    )
    if not summary.checks["busy_send_is_409"]:
        log(f"warning: send while busy answered {busy.status_code} {busy.text[:200]}")

    printed: dict[str, int] = {}
    answered: set[str] = set()
    seen_files: set[str] = set()
    while True:
        summary.polls += 1
        page = api.poll(session["id"], after=summary.user_message_id)
        turn = _assistant_turn(page, summary.assistant_message_id)
        if turn is None:
            log("poll: assistant message not visible yet")
        else:
            if summary.polls == 1:
                summary.checks["assistant_id_matches_202"] = turn["id"] == summary.assistant_message_id
            for part in turn.get("parts", []):
                kind = part.get("type")
                if kind == "text":
                    shown = printed.get(part["id"], 0)
                    if len(part["text"]) > shown:
                        log(part["text"][shown:])
                        printed[part["id"]] = len(part["text"])
                elif kind == "question":
                    card = {k: part[k] for k in ("question_id", "status", "expires_at", "questions")}
                    if part["question_id"] not in {c["question_id"] for c in summary.cards}:
                        summary.cards.append(card)
                    if part["status"] == "pending" and part["question_id"] not in answered:
                        answers = choose_answers(part)
                        log(f"card {part['question_id']} (expires {part['expires_at']}) → {answers}")
                        api.answer(session["id"], part["question_id"], answers)
                        answered.add(part["question_id"])
                        card["answers"] = answers
                    elif part["status"] != "pending":
                        for known in summary.cards:
                            if known["question_id"] == part["question_id"]:
                                known["status"] = part["status"]
                elif kind == "file":
                    info = part["file"]
                    if info["id"] not in seen_files:
                        seen_files.add(info["id"])
                        summary.files.append(info)
                        log(f"file {info['role']}: {info['filename']} ({info['size']} bytes) {info['id']}")
                        if info["role"] == "final":
                            summary.final_files.append(info)
            if turn.get("finish") is not None:
                summary.finish = turn["finish"]
                summary.error = turn.get("error")
                break
        if time.monotonic() - started > deadline_s:
            log("deadline reached; aborting the session")
            try:
                api.abort(session["id"])
            finally:
                summary.finish = "timeout"
            break
        sleep(poll_interval)

    summary.elapsed_s = round(time.monotonic() - started, 1)
    log(f"turn finished: {summary.finish} after {summary.polls} polls / {summary.elapsed_s}s")

    # Replay: the same client_message_id must hand back the original ids, not a new run.
    replay = api.send_raw(session["id"], {
        "text": text, "attachments": [item["id"] for item in summary.uploaded],
        "client_message_id": client_message_id,
    })
    summary.checks["idempotent_replay"] = (
        replay.status_code == 202
        and replay.json().get("user_message_id") == summary.user_message_id
        and replay.json().get("assistant_message_id") == summary.assistant_message_id
    )
    if not summary.checks["idempotent_replay"]:
        log(f"warning: replay answered {replay.status_code} {replay.text[:200]}")

    final_session = api.get_session(session["id"])
    summary.credits_used = final_session.get("credits_used", "0")
    summary.checks["session_idle_after_turn"] = final_session.get("status") in ("idle", "error")
    log(f"session status={final_session.get('status')} credits_used={summary.credits_used}")

    if summary.final_files:
        location = api.download_location(summary.final_files[0]["id"])
        summary.checks["download_link_refreshes"] = location.startswith("http")
        if download_dir is not None:
            download_dir.mkdir(parents=True, exist_ok=True)
            target = download_dir / summary.final_files[0]["filename"]
            with httpx.stream("GET", location, timeout=600, follow_redirects=True) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            summary.downloaded.append(str(target))
            log(f"downloaded {target}")
    summary.checks["final_file_delivered"] = bool(summary.final_files) and summary.finish == "stop"
    return summary


def preflight(api: HarnessClient, base_url: str, *, transport=None) -> dict[str, bool]:
    """Key works, and a bad key gets the documented 401 body."""
    checks: dict[str, bool] = {}
    session = api.create_session(title="preflight", quality=None, metadata={"probe": "preflight"})
    checks["key_accepted"] = session.get("status") == "idle" and session.get("metadata", {}).get("probe") == "preflight"
    missing = api.http.get(f"/sessions/{session['id']}x")
    body = missing.json().get("error", {})
    checks["not_found_shape"] = missing.status_code == 404 and body.get("code") == "NOT_FOUND" and bool(body.get("request_id"))
    bad = HarnessClient(base_url, "obx_sk_invalid", transport=transport)
    try:
        response = bad.http.get(f"/sessions/{session['id']}")
    finally:
        bad.close()
    checks["bad_key_is_401"] = response.status_code == 401 and response.json().get("error", {}).get("code") == "UNAUTHORIZED"
    low = api.http.post("/sessions", json={"quality": "low"})
    checks["low_quality_refused"] = low.status_code == 400
    return checks


# ─── CLI ───

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="e.g. https://host/v1")
    parser.add_argument("--key", default=os.environ.get(KEY_ENV), help=f"API key (or ${KEY_ENV})")
    parser.add_argument("--text", default="帮我做一条 25 秒的五一杭州自驾攻略口播，竖屏，不加字幕", help="the brief")
    parser.add_argument("--material", action="append", default=[], type=Path, help="file to upload; repeatable")
    parser.add_argument("--quality", default="high", help="high / medium (omit with '')")
    parser.add_argument("--title", default="高德联调 e2e")
    parser.add_argument("--task-id", default=None, help="metadata.task_id and client_message_id prefix")
    parser.add_argument("--answers", default="first", help="'first', 'interactive', or a JSON list of answer arrays")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE_SECONDS, help="seconds before abort")
    parser.add_argument("--download-dir", type=Path, default=None, help="save the final file here")
    parser.add_argument("--preflight-only", action="store_true", help="only verify the key and error shapes")
    args = parser.parse_args(argv)
    if not args.key:
        parser.error(f"--key or ${KEY_ENV} is required")

    task_id = args.task_id or f"e2e-{int(time.time())}"
    if args.answers == "first":
        choose = first_option_answers
    elif args.answers == "interactive":
        choose = interactive_answers
    else:
        choose = scripted_answers(json.loads(args.answers))

    api = HarnessClient(args.base_url, args.key)
    try:
        checks = preflight(api, args.base_url)
        _log(f"preflight: {checks}")
        if args.preflight_only:
            print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
            return 0 if all(checks.values()) else 1
        summary = run_flow(
            api,
            text=args.text,
            materials=args.material,
            quality=args.quality or None,
            title=args.title,
            metadata={"task_id": task_id},
            choose_answers=choose,
            client_message_id=f"{task_id}-1",
            poll_interval=args.poll_interval,
            deadline_s=args.deadline,
            download_dir=args.download_dir,
        )
    except FlowError as exc:
        _log(f"FAILED: {exc}")
        return 2
    finally:
        api.close()
    summary.checks.update({f"preflight.{k}": v for k, v in checks.items()})
    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 0 if all(summary.checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
