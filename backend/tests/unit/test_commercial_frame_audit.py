import asyncio
import io
import shutil
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from light import commercial_film as film
from light import commercial_frame_audit as audit


class Plan(SimpleNamespace):
    def model_dump(self):
        return vars(self)


def plan():
    return Plan(format=audit.recipe.FORMAT, score_revision=2, language="vi", product="Sản phẩm", appearance="đỏ",
                mode="carry", carry="Mang bằng tay.", track_wardrobe="")


def rows(first, last):
    return [{"first": n, "last": min(n + 3, last), "observation": "Khung thực tế"} for n in range(first, last + 1, 4)]


def test_grid_requires_all_output_rows_and_native_confirmations():
    schema = audit.page_schema(351, 364)
    checked = schema.model_validate({"rows": rows(351, 364), "suspicions": []})
    assert checked.rows[-1].last == 364
    with pytest.raises(ValidationError):
        schema.model_validate({"rows": rows(351, 364)[:-1], "suspicions": []})
    with pytest.raises(ValidationError):
        schema.model_validate({"rows": rows(351, 364), "suspicions": [{"first": 350, "last": 351,
                               "kind": "extra_product", "region": [1, 2, 3, 4], "observation": "outside the shot"}]})
    with pytest.raises(ValidationError):
        audit.native_schema([353]).model_validate({"frames": [{"frame": 353, "status": "confirmed", "observation": "two items"}], "correction": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("kind", ["extra_product", "wrong_effect_source"])
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="The frame-accurate media round trip requires FFmpeg.")
async def test_one_frame_flash_and_occlusion_use_exact_retained_pixels_and_resume(monkeypatch, tmp_path, confirmed, kind):
    p = plan()
    shot = {"id": 4, "action": "Giơ tay.", "product_visible": True}
    count = audit.recipe.BEATS[3].frames
    cut = {"shot": 4, "source": "source", "start": 2, "frames": count, "output_frames": count, "speed": 1.0,
           "review_board": "selected", "has_audio": False}
    # One red frame at source F4 falls only at absolute commercial F190 (F188 + 4 - 2).
    frames = b"".join(Image.new("RGB", (160, 90), (245, 20, 20) if n == 4 else (16, 20, 16)).tobytes()
                      for n in range(count + 6))
    await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "160x90",
                    "-framerate", "24", "-i", "-", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "source.mp4")],
                   input=frames, check=True)
    stored, calls, state = {}, [], {}
    async def image(ctx, key, data):
        stored[key] = Image.open(io.BytesIO(data)).convert("RGB")
        return key
    async def ask(ctx, name, schema, guide, data, images, **kwargs):
        calls.append(name)
        assert data["language"] == "vi" and guide.startswith("Viết")
        if name == "commercial_pixels_in_order":
            sheet = stored[images[-1]]
            colors = [sheet.getpixel((n % 4 * 320 + 12, n // 4 * 202 + 37))[0] for n in range(count)]
            assert [i for i, red in enumerate(colors) if red > 180] == [2]
            value = {"rows": rows(188, 198), "suspicions": [{"first": 190, "last": 190, "kind": kind, "region": [15, 16, 100, 110],
                                                             "observation": "Chi tiết đỏ."}]}
        else:
            assert data["focus"] == [190] and data["native_order"] == list(range(188, 193))
            context = data.get("context_sheet_count", 0)
            assert context == int(kind == "wrong_effect_source") and data["review_kind"] == kind
            if context:
                assert stored[images[1]].width == audit.COLUMNS * audit.WIDTH and audit.EFFECT_NATIVE in guide
            pics = [stored[asset] for asset in images[1 + context:6 + context]]
            assert all(pic.size == (1280, 720) for pic in pics)
            assert [i for i, pic in enumerate(pics) if pic.getpixel((50, 50))[0] > 180] == [2]
            value = {"frames": [{"frame": 190, "status": "confirmed" if confirmed else "inconclusive",
                                 "observation": "Có vật thứ hai." if confirmed else "Bị bàn tay che."}],
                     "correction": "Bỏ vật thứ hai." if confirmed else ""}
        return {"model": "unit", "report": schema.model_validate(value).model_dump(), "evidence": images}
    async def record(ctx, prefix, run, i, key, choice):
        return run.setdefault("director_decisions", {}).setdefault(key, choice)
    monkeypatch.setattr(audit, "_reference_asset", image)
    monkeypatch.setattr(audit.reference_quality, "ask", ask)
    arguments = (None, tmp_path, "job", state, p, shot, cut, [("original product", "raw")], record)
    key, result = await audit.inspect_cut(*arguments)
    assert result["ok"] == (not confirmed)
    if confirmed:
        assert result["shot"] == 4 and "F0190" in result["reason"] and result["correction"]
        selection = {4: cut, 5: {"shot": 5}}
        corrections = {}
        film._repair_joins([result], selection, corrections, {})
        assert list(selection) == [5] and 4 in corrections
    assert await audit.inspect_cut(*arguments) == (key, result)
    assert calls == ["commercial_pixels_in_order", "commercial_pixels_native"]
    # A restart after a page/native write but before the shot checkpoint reuses both model decisions.
    state["director_decisions"].pop(key)
    def already_read(*args):
        pytest.fail("A completed pixel review must not download or re-encode the source again.")
    monkeypatch.setattr(audit, "_edited_cut", already_read)
    assert (await audit.inspect_cut(*arguments))[1] == result
    assert len(calls) == 2


def certificate(p):
    decisions, cuts, checks = {}, {}, []
    for i, beat in enumerate(audit.recipe.BEATS, 1):
        key = f"shot-{i}"
        cut = {"shot": i, "source": f"source-{i}", "start": 3, "frames": beat.frames, "output_frames": beat.frames,
               "review_board": f"board-{i}", "speed": 1.0}
        cuts[i] = cut
        groups = []
        for first, last in audit._batches(i):
            batch = f"{key}:page:{first}"
            groups.append(batch)
            decisions[batch] = {"reviewed": True, "cut": f"identity-{i}", "range": [first, last],
                                "sheets": [f"sheet:{n}" for n in range(first, last + 1, audit.PAGE)],
                                "report": {"rows": rows(first, last), "suspicions": []}}
        decisions[key] = {"ok": True, "shot": i, "version": audit.VERSION, "identity": f"identity-{i}",
                          "board": cut["review_board"], "window": audit.window(cut), "batches": groups, "native_checks": []}
        checks.append(key)
    return decisions, cuts, {"frame_checks": checks, "frame_plan": audit.plan_id(p), "cut_assets": [cut["review_board"] for cut in cuts.values()]}


def test_new_cut_requires_complete_1277_frame_receipts_and_matching_source_windows():
    p = plan()
    decisions, cuts, proof = certificate(p)
    assert audit.certified(decisions, proof, p, cuts)
    assert sum(sum(row['last'] - row['first'] + 1 for row in v['report']['rows']) for v in decisions.values() if 'report' in v) == 1277
    assert not audit.certified(decisions, {}, p, cuts)
    assert audit.certified(decisions, {**proof, "frame_plan": audit._digest(p.model_dump())}, p, cuts)
    assert audit.certified({}, {}, Plan(format=audit.recipe.FORMAT, score_revision=1))
    assert audit.certified({}, {}, Plan(format="product-story-v3", score_revision=2))
    assert not audit.certified(decisions, proof, p, {**cuts, 4: {**cuts[4], "start": 4}})
    changed = deepcopy(decisions)
    changed[changed['shot-11']['batches'][0]]['report']['rows'].pop()
    assert not audit.certified(changed, proof, p, cuts)
    group = decisions['shot-11']['batches'][0]
    decisions[group]['report']['suspicions'] = [{'first': 352, 'last': 352, 'kind': 'extra_product', 'region': [0, 0, 80, 80]}]
    assert not audit.certified(decisions, proof, p, cuts)
    native = f"shot-11:native:{audit._digest([352], 'extra_product', [0, 0, 80, 80])}"
    decisions[native] = {'reviewed': True, 'full_frames': ['f351', 'f352', 'f353', 'f354'], 'confirmed': [], 'report': {'frames': [{'frame': 352, 'status': 'clear'}]}}
    decisions['shot-11']['native_checks'] = [native]
    assert audit.certified(decisions, proof, p, cuts)
    decisions[native]['confirmed'] = [{'frame': 352, 'status': 'confirmed'}]
    assert not audit.certified(decisions, proof, p, cuts)


@pytest.mark.asyncio
@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.parametrize('watching', [False, True])
async def test_reopen_rejected_saved_window_preserves_history_and_published_output(monkeypatch, attached, watching):
    from contextlib import asynccontextmanager

    from db import base
    from light import commercial_playback_review, production
    from light.remake import RemakeStopped

    prior = {'job_prefix': 'job', 'wan_review_version': audit.recipe.REVIEW, 'wan_final_key': 'old-composition',
             'wan_cuts': [{'shot': 4, 'source': 'bad'}, {'shot': 5, 'source': 'unaffected'}], 'wan_quality': {'id': 'former'}}
    row = SimpleNamespace(light_production={'run': deepcopy(prior)})
    class ReadOnlyJob:
        async def scalar(self, query):
            return SimpleNamespace(attached_message_id='published-message' if attached else None, status='completed')
    @asynccontextmanager
    async def db_session():
        yield ReadOnlyJob()
    async def lock(db, sid, uid, workspace, *, run_fence):
        assert (sid, uid, workspace, run_fence) == ('session', 'user', 'workspace', 'fence')
        return row
    monkeypatch.setattr(base, 'get_db_session', db_session)
    monkeypatch.setattr(production, '_lock', lock)
    ctx = SimpleNamespace(session_id='session', user_id='user', workspace_id='workspace', run_fence='fence')
    version = commercial_playback_review.VERSION if watching else audit.VERSION
    args = (ctx, 'job', prior, {4}, [{'version': version, 'ok': False, 'identity': 'real-native-bad-frame'}])
    if attached:
        with pytest.raises(RemakeStopped):
            await audit.reopen_saved(*args)
        assert row.light_production['run'] == prior and len(prior['wan_cuts']) == 2
    else:
        await audit.reopen_saved(*args)
        assert prior == row.light_production['run']
        assert prior['wan_cuts'] == [{'shot': 5, 'source': 'unaffected'}] and 'wan_final_key' not in prior
        archive, = prior['wan_edit_history']
        assert archive['final_key'] == 'old-composition' and archive['cuts'][0]['source'] == 'bad'
        field = "playback_rejections" if watching else "pixel_rejections"
        assert archive[field] == ['real-native-bad-frame'] and archive['wan_quality']['id'] == 'former'
        await audit.reopen_saved(*args)
        assert len(prior['wan_edit_history']) == 1


def test_previous_published_attachment_remains_in_mobile_history_without_new_pixel_receipts():
    from datetime import UTC, datetime

    from light import production, production_form

    stamp = datetime.now(UTC)
    old = {"action": "render", "remake_adaptation": {"format": audit.recipe.FORMAT, "score_revision": 2},
           "director_decisions": {f"{audit.recipe.REVIEW}:joins": {"approved": True}}}
    session = SimpleNamespace(light_entry="video", status="idle", created_at=stamp,
                              light_production={**production_form.initial_form("video", "name", ""),
                                                "template": "product_story", "run": old})
    attachment = SimpleNamespace(created_at=stamp, message_id="published-message", data={"asset_id": "earlier-result",
                                 "mime_type": "video/mp4", "relation": {"role": "final"}})
    assert production._output_candidates(session, [], [attachment], advanced_messages={"published-message"}) == ["earlier-result"]
    assert production._work_view(session, None, [], "earlier-result", False)["status"] == "completed"


@pytest.mark.parametrize("mode,transfer", [("wear", "fit"), ("carry", "pass"), ("stationary", "access")])
def test_office_phase_follows_the_new_user_of_the_product(mode, transfer):
    p = plan()
    p.mode, p.transfer_mode = mode, transfer
    carry = audit._product_state(p, {"id": 24})
    assert carry == {"carry": p.carry}
    phase = {i: audit._product_state(p, {"id": i, "frame": "Actual opening", "end": "Actual ending"}) for i in range(25, 29)}
    assert all("carry" not in state and state["transfer_mode"] == transfer for state in phase.values())
    assert all(state["shot_opening"] == "Actual opening" and state["shot_ending"] == "Actual ending" for state in phase.values())
    assert "has not taken over" in phase[25]["role_and_timing"]
    assert "by the end" in phase[26]["role_and_timing"]
    assert "already controls" in phase[27]["role_and_timing"] and "dance or a smile at the opening" in phase[27]["role_and_timing"]
    assert "authority is the dancer" in phase[28]["role_and_timing"]
    assert ("fitting_support" in phase[26]) == ("fitting_support" in phase[27]) == (transfer == "fit")


def test_home_pickup_and_inserts_use_their_own_support_before_the_commute():
    p = plan()
    p.carry = "Closed and carried at the left waist."
    for i in range(1, 10):
        shot = {"id": i, "frame": "Uncapped on the table.", "end": "A hand reaches the loose cap."}
        phase = audit._product_state(p, shot)
        assert phase["shot_opening"] == shot["frame"] and phase["shot_ending"] == shot["end"] and "carry" not in phase
    assert audit._product_state(p, {"id": 10}) == {"carry": p.carry}


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted,published", [(False, False), (True, False), (True, True)])
async def test_verified_first_match_only_reopens_bound_unpublished_windows(monkeypatch, accepted, published):
    from contextlib import asynccontextmanager

    from db import base
    from light import production
    from light.remake import RemakeStopped

    old_key = f"{audit.recipe.REVIEW}:take:11:old"
    key = old_key + ":recut-first-match-entry-v1"
    cut = {"shot": 11, "source": "actual-paid-take", "start": 3, "frames": 14, "output_frames": 14, "speed": 1.0,
           "review_board": "old-board", "review_tail": "old-tail"}
    proposed = {**cut, "start": 1, "review_board": "new-board", "review_tail": "new-tail"}
    checked = {"ok": accepted, "source": cut["source"], "replaces_review": old_key, "evidence": ["real-source-board"],
               "report": {"entry_path": "Hand enters from outside on the real board", "has_clear_approach": accepted,
                          "reached_feature": True}, **({"cut": proposed} if accepted else {})}
    current = {"job_prefix": "job", "wan_review_version": audit.recipe.REVIEW, "wan_final_key": "prior-export",
               "wan_cuts": [cut, {"shot": 12, "source": "old-incoming-tail"}, {"shot": 22, "source": "street"}],
               "director_decisions": {old_key: {"ok": True, "cut": cut}, key: checked}}
    e = {"key": key, "old_board": cut["review_board"], "source": cut["source"], "shot": 11}
    ctx = SimpleNamespace(session_id="session", user_id="owner", workspace_id="workspace", run_fence="fence")
    row = SimpleNamespace(light_production={"run": deepcopy(current)})
    class Jobs:
        async def scalar(self, query):
            return SimpleNamespace(attached_message_id="message" if published else None, status="completed")
    @asynccontextmanager
    async def db_session():
        yield Jobs()
    async def lock(db, sid, uid, workspace, *, run_fence):
        assert (sid, uid, workspace, run_fence) == ("session", "owner", "workspace", "fence")
        return row
    monkeypatch.setattr(base, "get_db_session", db_session)
    monkeypatch.setattr(production, "_lock", lock)
    for wrong in [{**e, "old_board": "other-board"}, {**e, "source": "other-take"}, {**e, "key": old_key}]:
        with pytest.raises(RemakeStopped):
            await audit.reopen_saved(ctx, "job", current, {11, 12}, [], verified_first_match=wrong)
    with pytest.raises(RemakeStopped):
        await audit.reopen_saved(ctx, "job", current, {11, 22}, [], verified_first_match=e)
    if accepted:
        db_result = row.light_production["run"]["director_decisions"][key]
        db_result["cut"] = cut
        with pytest.raises(RemakeStopped):
            await audit.reopen_saved(ctx, "job", current, {11, 12}, [], verified_first_match=e)
        db_result["cut"] = proposed
    if published:
        with pytest.raises(RemakeStopped):
            await audit.reopen_saved(ctx, "job", current, {11, 12}, [], verified_first_match=e)
        assert row.light_production["run"]["wan_cuts"][0] == cut
        return
    await audit.reopen_saved(ctx, "job", current, {11, 12}, [], verified_first_match=e)
    assert [i["shot"] for i in current["wan_cuts"]] == [22]
    history, = current["wan_edit_history"]
    assert history["cuts"][0] == cut and history["first_match_recheck"]["key"] == key
    assert history["first_match_recheck"]["ok"] == accepted
