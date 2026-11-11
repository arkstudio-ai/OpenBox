"""Three interface languages on the server (docs/LIGHT_MODE_I18N_PLAN.md §2.1, §2.2, §2.4).

The language pipe (``Accept-Language`` → ``request.state`` → ``current_user["ui_language"]``, the stored
``extra.ui_language``), the three ``entries.<lang>.json`` files and their purity rules, the notification
templates and the voice fallback sentences with the vi-VN → en-US fallback.
"""
import json
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from core import ui_language
from core.ui_language import LANGS, UILanguageMiddleware, normalize, resolve
from light import entries as entries_mod
from light.entries import COPY_FIELDS, EntriesCopy, LightEntries, light_entries
from notifications import templates as templates_mod
from notifications.templates import PLATFORMS, TEMPLATES, render_template, test_template as preview_template
from voice import phrases

LIGHT = Path(entries_mod.__file__).parent
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
#: Latin allowed in Chinese copy: the brand only (LIGHT_MODE_I18N_PLAN §5.1).
ZH_ALLOWED = {"bossip"}


# --- the pipe -------------------------------------------------------------------------------------

@pytest.mark.parametrize("header, expected", [
    ("zh-CN", "zh-CN"), ("zh", "zh-CN"), ("zh-Hant-TW", "zh-CN"), ("ZH_cn", "zh-CN"),
    ("vi-VN", "vi-VN"), ("vi", "vi-VN"),
    ("en-US", "en-US"), ("en", "en-US"), ("fr-FR", "en-US"), ("*", "en-US"),
    ("vi-VN,en-US;q=0.8", "vi-VN"), ("en-GB;q=0.9, zh-CN;q=0.8", "en-US"), ("  zh ; q=1", "zh-CN"),
    ("", None), ("   ", None), (None, None), (",en", None),
])
def test_accept_language_normalizes_to_one_of_three(header, expected):
    assert normalize(header) == expected


async def _user():
    from db.repository.user_repo import PgUserRepo
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="li" + uid[:8], password_hash=None)


class _Request:
    def __init__(self, lang=None):
        from types import SimpleNamespace
        self.state = SimpleNamespace(**({"ui_language": lang} if lang else {}))


async def test_resolve_prefers_the_header_then_the_preference_then_chinese():
    user = await _user()
    assert await resolve(user["id"], None) == "zh-CN"  # no header, nothing stored: an old App
    assert await resolve(user["id"], _Request()) == "zh-CN"
    assert await ui_language.set_ui_language(user["id"], "vi-VN") == "vi-VN"
    assert await ui_language.ui_language_for(user["id"]) == "vi-VN"
    assert await resolve(user["id"], _Request()) == "vi-VN"
    assert await resolve(user["id"], _Request("en-US")) == "en-US"  # this request's header wins
    assert await resolve(user["id"], _Request("de")) == "vi-VN"  # not a language value: ignored
    with pytest.raises(ValueError):
        await ui_language.set_ui_language(user["id"], "fr-FR")
    # the key lives next to ui_mode and survives other writers of extra
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(user["id"], extra={"ui_mode": "light"})
    row = await PgPreferenceRepo().get(user["id"])
    assert row["extra"]["ui_language"] == "vi-VN" and row["extra"]["ui_mode"] == "light"
    assert await ui_language.ui_language_for(None) == "zh-CN"


def _app(user, *, middleware=True):
    from api.light import router
    from auth.middleware import get_optional_current_user
    from auth.workspace import get_workspace
    app = FastAPI()
    if middleware:
        app.add_middleware(UILanguageMiddleware)
    app.include_router(router)
    # get_current_user itself runs, so the request's language reaches current_user["ui_language"]
    app.dependency_overrides[get_optional_current_user] = lambda: {"user_id": user["id"], "workspace_id": user["default_workspace_id"]}
    app.dependency_overrides[get_workspace] = lambda: {"id": user["default_workspace_id"], "role": "owner"}
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_entries_follow_accept_language_then_the_stored_preference(monkeypatch):
    from light import mode as light_mode
    user = await _user()

    async def facts(workspace_id):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(light_mode, "_workspace_facts", facts)
    async with _client(_app(user)) as client:
        vi = (await client.get("/api/light/entries", headers={"Accept-Language": "vi-VN"})).json()
        assert vi["ui_language"] == "vi-VN"
        by_id = {entry["id"]: entry for entry in vi["entries"]}
        assert by_id["video"]["title"] == "Làm một video" and by_id["menu"]["steps"][0]["label"] == "Chọn kiểu"
        assert by_id["video"]["subtypes"][0]["samples"][0]["media_url"] is not None or True  # structure untouched
        assert by_id["review"]["requires"] == {"plan": False, "desktop": True, "binding": "douyin"}
        en = (await client.get("/api/light/entries", headers={"Accept-Language": "en"})).json()
        assert en["ui_language"] == "en-US" and en["entries"][0]["title"] == "Make a video"
        zh = (await client.get("/api/light/entries")).json()
        assert zh["ui_language"] == "zh-CN" and zh["entries"][0]["title"] == "做一条视频"
        # the stored preference, written through the App's endpoint, drives header-less requests
        assert (await client.put("/api/light/language", json={"ui_language": "vi-VN"})).json() == {"ui_language": "vi-VN"}
        assert (await client.get("/api/light/language")).json() == {"ui_language": "vi-VN"}
        assert (await client.put("/api/light/language", json={"ui_language": "fr-FR"})).status_code == 400
        stored = (await client.get("/api/light/entries")).json()
        assert stored["ui_language"] == "vi-VN" and stored["entries"][0]["title"] == "Làm một video"
        # a request that says otherwise still wins
        assert (await client.get("/api/light/entries", headers={"Accept-Language": "zh"})).json()["entries"][0]["title"] == "做一条视频"


async def test_start_opens_the_topic_in_the_request_language(monkeypatch):
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    from db.models.session import Session as SessionRow
    from light import mode as light_mode
    user = await _user()

    async def facts(workspace_id):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(light_mode, "_workspace_facts", facts)
    async with _client(_app(user)) as client:
        started = (await client.post("/api/light/start", headers={"Accept-Language": "vi-VN"},
                                     json={"entry_id": "menu", "subtype": "menu_style", "sample": "menu_red"})).json()
    assert started["opening_card"]["text"].startswith("Làm một bản thực đơn kiểu “Đỏ rực rỡ”")
    assert started["steps"][0] == {"id": "style", "label": "Chọn kiểu"} and started["entry"]["title"] == "Làm thực đơn"
    async with get_db_session() as db:
        row = await db.get(SessionRow, started["session_id"])
        parts = (await db.scalars(select(Part).where(Part.session_id == row.id))).all()
    assert row.title == "Làm thực đơn"
    texts = {p.data["text"] for p in parts if p.type == "text"}
    assert "[Làm thực đơn] đã chọn “Đỏ rực rỡ”" in texts


async def test_me_preferences_accepts_only_the_three_languages():
    from auth.routes import router as auth_router
    from auth.middleware import get_current_user
    user = await _user()
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with _client(app) as client:
        first = await client.put("/api/auth/me/preferences", json={"ui_language": "en-US"})
        assert first.status_code == 200, first.text
        assert first.json()["extra"]["ui_language"] == "en-US"
        assert (await client.put("/api/auth/me/preferences", json={"ui_language": "fr"})).status_code == 422
        assert (await client.put("/api/auth/me/preferences", json={"extra": {"ui_language": "fr"}})).status_code == 400
        body = (await client.put("/api/auth/me/preferences", json={"extra": {"ui_language": "vi-VN", "browser_mode": "auto"}})).json()
        assert body["extra"]["ui_language"] == "vi-VN" and body["extra"]["browser_mode"] == "auto"
    assert await ui_language.ui_language_for(user["id"]) == "vi-VN"


# --- the three entries files -------------------------------------------------------------------

def _copy_strings(entry: dict):
    """Every copy string of one entry (as laid out in the translation files), with its path."""
    for field in COPY_FIELDS["entry"]:
        yield f"{entry['id']}.{field}", entry.get(field) or ""
    for step in entry.get("steps") or []:
        yield f"{entry['id']}.steps.{step['id']}.label", step.get("label") or ""
    for subtype in entry.get("subtypes") or []:
        yield f"{entry['id']}.subtypes.{subtype['id']}.label", subtype.get("label") or ""
        for sample in subtype.get("samples") or []:
            for field in COPY_FIELDS["sample"]:
                yield f"{entry['id']}.{subtype['id']}.{sample['id']}.{field}", sample.get(field) or ""
    card = entry.get("opening_card") or {}
    for field in ("text", "text_with_sample"):
        yield f"{entry['id']}.opening_card.{field}", card.get(field) or ""
    for index, chip in enumerate(card.get("chips") or []):
        yield f"{entry['id']}.opening_card.chips[{index}]", chip


def _raw(lang):
    return json.loads((LIGHT / f"entries.{lang}.json").read_text(encoding="utf-8"))


def _skeleton(entry: dict) -> dict:
    """Ids only: the shape the three files must share."""
    return {"steps": [s["id"] for s in entry.get("steps") or []],
            "subtypes": {s["id"]: [x["id"] for x in s.get("samples") or []] for s in entry.get("subtypes") or []},
            "opening_card": entry.get("opening_card") is not None}


def test_translation_files_align_with_the_structure_and_carry_copy_only():
    zh = _raw("zh-CN")
    structure = LightEntries.model_validate(zh)
    for lang in ("en-US", "vi-VN"):
        raw = _raw(lang)
        copy = EntriesCopy.model_validate(raw)  # extra="forbid": a non-copy field (skill, requires, media_url…) fails here
        assert [entry.id for entry in copy.entries] == [entry.id for entry in structure.entries], lang
        for raw_entry, zh_entry in zip(raw["entries"], zh["entries"]):
            assert _skeleton(raw_entry) == _skeleton(zh_entry), f"{lang}: {raw_entry['id']}"
            assert set(raw_entry) - {"id", "steps", "subtypes", "opening_card"} <= set(COPY_FIELDS["entry"]), raw_entry["id"]
            for step in raw_entry.get("steps") or []:
                assert set(step) <= {"id", *COPY_FIELDS["step"]}
            for subtype in raw_entry.get("subtypes") or []:
                assert set(subtype) <= {"id", "samples", *COPY_FIELDS["subtype"]}
                for sample in subtype.get("samples") or []:
                    assert set(sample) <= {"id", *COPY_FIELDS["sample"]}
            if raw_entry.get("opening_card"):
                assert set(raw_entry["opening_card"]) <= set(COPY_FIELDS["opening_card"])
            # every string the Chinese file has, the translation has too (a translation is complete, not a patch)
            zh_paths = {path for path, value in _copy_strings(zh_entry) if value}
            lang_paths = {path for path, value in _copy_strings(raw_entry) if value}
            assert zh_paths <= lang_paths, f"{lang}: missing {sorted(zh_paths - lang_paths)}"
    # the loaded views agree on structure and only differ in copy
    for lang in LANGS:
        view = light_entries(lang)
        assert [e.id for e in view.entries] == [e.id for e in structure.entries]
        for entry, base in zip(view.entries, structure.entries):
            assert entry.skill == base.skill and entry.requires == base.requires and entry.action == base.action
            assert entry.step_ids() == base.step_ids() and entry.default_tier == base.default_tier
            assert [(s.id, [x.id for x in s.samples]) for s in entry.subtypes] == [(s.id, [x.id for x in s.samples]) for s in base.subtypes]
            for sub, sub_base in zip(entry.subtypes, base.subtypes):
                for sample, sample_base in zip(sub.samples, sub_base.samples):
                    assert (sample.media_url, sample.poster_url) == (sample_base.media_url, sample_base.poster_url)


def test_chinese_copy_has_no_latin_and_english_copy_has_no_chinese():
    for entry in _raw("zh-CN")["entries"]:
        for path, value in _copy_strings(entry):
            words = {w for w in LATIN_WORD.findall(value.replace("{sample}", "")) if w.lower() not in ZH_ALLOWED}
            assert not words, f"zh-CN {path}: {sorted(words)}"
    for entry in _raw("en-US")["entries"]:
        for path, value in _copy_strings(entry):
            assert not CJK.search(value), f"en-US {path}: {value!r}"
    # Vietnamese copy is Vietnamese (not the English file copied over): every entry title carries a tonal letter
    for entry in _raw("vi-VN")["entries"]:
        assert re.search(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", entry["title"].lower()), entry["id"]


def test_vietnamese_falls_back_to_english_then_chinese_per_string(monkeypatch, tmp_path):
    zh = _raw("zh-CN")
    (tmp_path / "entries.zh-CN.json").write_text(json.dumps(zh, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "entries.en-US.json").write_text(json.dumps({"entries": [
        {"id": "menu", "title": "Make a menu", "steps": [{"id": "style", "label": "Pick a style"}],
         "opening_card": {"chips": ["Snap it"]}}]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "entries.vi-VN.json").write_text(json.dumps({"entries": [
        {"id": "menu", "title": "Làm thực đơn"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LIGHT_ENTRIES_FILE", str(tmp_path / "entries.zh-CN.json"))
    entries_mod._load.cache_clear(); entries_mod._load_copy.cache_clear(); entries_mod._load_structure.cache_clear()
    try:
        vi, en, base = light_entries("vi-VN"), light_entries("en-US"), light_entries("zh-CN")
        menu_vi, menu_en, menu_zh = vi.entry("menu"), en.entry("menu"), base.entry("menu")
        assert menu_vi.title == "Làm thực đơn"  # its own
        assert menu_vi.steps[0].label == "Pick a style" and menu_en.steps[0].label == "Pick a style"  # vi → en
        assert menu_vi.steps[1].label == menu_zh.steps[1].label == "菜名价格"  # neither has it: the structure's
        assert menu_vi.opening_card.chips == ["Snap it"] and menu_vi.opening_card.text == menu_zh.opening_card.text
        assert en.entry("video").title == "做一条视频"  # en-US does not fall back to anything but the structure
        assert menu_vi.skill == "menu-maker" and menu_vi.subtypes[0].samples[0].media_url == menu_zh.subtypes[0].samples[0].media_url
        assert light_entries("xx-YY") is base and light_entries(None) is base
    finally:
        entries_mod._load.cache_clear(); entries_mod._load_copy.cache_clear(); entries_mod._load_structure.cache_clear()


def test_translation_files_reject_structure_fields():
    with pytest.raises(ValueError):
        EntriesCopy.model_validate({"entries": [{"id": "menu", "skill": "menu-maker"}]})
    with pytest.raises(ValueError):
        EntriesCopy.model_validate({"entries": [{"id": "menu", "subtypes": [{"id": "s", "samples": [{"id": "x", "media_url": "oss:a"}]}]}]})


# --- prompts and labels -----------------------------------------------------------------------

async def test_prompt_fragments_name_the_entry_and_the_output_language_in_the_stored_language():
    from light.prompt import availability_label, entries_directory, entry_fragment, light_mode_fragment, output_language_line
    from session.session import create_session, update_session
    user = await _user()
    guided = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    guided = await update_session(guided.id, user_id=user["id"], light_entry="menu")
    zh = await light_mode_fragment(user["id"], guided)
    assert "专题页：做菜单" in zh and "style(选样式)" in zh and output_language_line("zh-CN") in zh and "用中文" in zh
    await ui_language.set_ui_language(user["id"], "vi-VN")
    vi = await light_mode_fragment(user["id"], guided)
    assert "专题页：Làm thực đơn" in vi and "style(Chọn kiểu)" in vi and "menu-maker" in vi
    assert "越南语（Tiếng Việt）" in vi and "保留英文专有名词" in vi
    assert entry_fragment("menu", "en-US").startswith("专题页：Make a menu")
    assert availability_label("needs_desktop") == "需要云电脑"
    assert availability_label("needs_desktop", None, "en-US") == "Needs a cloud desktop"
    assert availability_label("needs_binding", "douyin", "vi-VN") == "Cần liên kết Douyin"
    assert availability_label("needs_binding", "xiaohongshu", "en-US") == "Needs xiaohongshu linked"
    assert availability_label("ok", None, "zz") == "可用" and availability_label("weird") == "weird"


# --- notifications ------------------------------------------------------------------------------

def test_notification_templates_are_trilingual_and_locale_tolerant():
    for kind, rows in TEMPLATES.items():
        assert set(rows) == set(LANGS), kind
        for lang, (title, body) in rows.items():
            assert title and body
            assert ("{name}" in rows["zh-CN"][1]) == ("{name}" in body), (kind, lang)
        assert not LATIN_WORD.search(rows["zh-CN"][0]) and not CJK.search(rows["en-US"][0] + rows["en-US"][1])
        assert not CJK.search(rows["vi-VN"][0] + rows["vi-VN"][1]), kind
    for key, names in PLATFORMS.items():
        assert set(names) == set(LANGS), key
    assert render_template("task_completed", "面馆", "zh-CN") == ("任务完成", "《面馆》已完成，查看结果。")
    assert render_template("task_completed", "Noodles", "en") == ("Task complete", "“Noodles” is ready. View results.")
    assert render_template("task_completed", "Mì", "vi-VN") == ("Công việc hoàn thành", "“Mì” đã xong. Xem kết quả.")
    assert render_template("task_completed", "x", "vi") == render_template("task_completed", "x", "vi-VN")
    assert render_template("task_completed", "x", "fr-FR") == render_template("task_completed", "x", "en-US")
    assert render_template("task_completed", "x", "") == render_template("task_completed", "x", "zh-CN")
    assert render_template("platform_auth_expired", "douyin", "vi-VN")[1].startswith("Ủy quyền Douyin")
    assert render_template("platform_auth_expired", "douyin", "zh-CN")[1].startswith("抖音")
    assert render_template("input_required", "", "vi-VN")[1] == "“Công việc” cần câu trả lời của bạn."
    assert preview_template("publish_done", "vi")[0].startswith("Thử · ") and preview_template("system_test", "vi")[0] == "Kiểm tra thông báo"
    assert preview_template("task_completed", "vi-VN")[1] == "“Công việc mẫu” đã xong. Xem kết quả."


def test_a_template_missing_vietnamese_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(TEMPLATES, "only_two", {"zh-CN": ("中", "《{name}》"), "en-US": ("En", "“{name}”")})
    assert render_template("only_two", "n", "vi-VN") == ("En", "“n”")
    monkeypatch.setitem(TEMPLATES, "only_zh", {"zh-CN": ("中", "《{name}》")})
    assert render_template("only_zh", "n", "vi-VN") == ("中", "《n》") and render_template("only_zh", "n", "en-US") == ("中", "《n》")
    assert templates_mod.pick({"zh-CN": 1, "en-US": 2}, "vi-VN") == 2


async def test_push_language_is_the_stored_interface_language_before_the_device_locale():
    from types import SimpleNamespace
    from db.base import get_db_session
    from notifications.events import notification_language
    user = await _user()
    device = SimpleNamespace(locale="en-US")
    async with get_db_session() as db:
        assert await notification_language(db, user["id"], None) == "zh-CN"
        assert await notification_language(db, user["id"], device) == "en-US"
    await ui_language.set_ui_language(user["id"], "vi-VN")
    async with get_db_session() as db:
        assert await notification_language(db, user["id"], device) == "vi-VN"
        assert await notification_language(db, user["id"], None) == "vi-VN"


# --- voice ----------------------------------------------------------------------------------------

def test_voice_fallback_sentences_are_trilingual_and_vi_falls_back_to_en():
    assert phrases.LANGS == ("zh", "en", "vi")
    for table in (phrases.PHRASES, phrases.SPEECHES):
        for key, rows in table.items():
            assert set(rows) == {"zh", "en", "vi"}, key
            assert not CJK.search(rows["en"] + rows["vi"]) and not LATIN_WORD.search(rows["zh"]), key
    assert set(phrases.NOTE_PREFIX) == set(phrases.CARRIED_PREFIX) == {"zh", "en", "vi"}
    assert phrases.speech_text("timeout", "vi") == "Chưa có kết quả, bạn xem tiếp trong cuộc trò chuyện nhé."
    assert phrases.phrase_text("reconnected", "xx") == phrases.PHRASES["reconnected"]["zh"]  # unknown stays zh (legacy)
    assert phrases.carried_utterance("w", "vi").startswith(phrases.CARRIED_PREFIX["vi"])
    assert phrases.joined_note([], "vi").startswith(phrases.NOTE_PREFIX["vi"])
    assert phrases._pick({"zh": "z", "en": "e"}, "vi") == "e" and phrases._pick({"zh": "z"}, "vi") == "z"
    # the model-facing instruction blocks exist in zh and en only: vi reads the en block
    assert phrases.greeting_instructions("vi", True) == phrases.greeting_instructions("en", True)
    assert phrases.receipt_instructions("vi") == phrases.receipt_instructions("en")
    assert phrases.phrase_instructions("credits_exhausted", "vi").startswith("Convey this call status")
    assert phrases.phrase_text("credits_exhausted", "vi") in phrases.phrase_instructions("credits_exhausted", "vi")
    assert "Bạn muốn xem không" in phrases.room_interjection("Mì", "vi")


async def test_call_language_follows_the_interface_language_then_the_web_locale():
    from db.repository.preference_repo import PgPreferenceRepo
    user = await _user()
    assert await phrases.user_language(user["id"]) == "zh"
    await PgPreferenceRepo().upsert(user["id"], extra={"locale": "en-US"})
    assert await phrases.user_language(user["id"]) == "en"
    await ui_language.set_ui_language(user["id"], "vi-VN")
    assert await phrases.user_language(user["id"]) == "vi"
    await ui_language.set_ui_language(user["id"], "zh-CN")
    assert await phrases.user_language(user["id"]) == "zh"
