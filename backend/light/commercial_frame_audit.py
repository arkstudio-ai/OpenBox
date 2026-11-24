"""Inspect every delivered picture of a selected commercial cut before it can be published."""

from __future__ import annotations

import hashlib
import io
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, model_validator

from light import commercial_plan as recipe
from light import product_story, product_story_post, reference_quality
from light.remake import RemakeStopped, _parallel, _reference_asset

VERSION = "cut-pixels-v2"
WIDTH, HEIGHT = 320, 180
COLUMNS, PAGE, BATCH = 4, 24, 48
NATIVE_GROUP = 6
LANGUAGE = {"zh": "用中文写观察、原因和修正。", "vi": "Viết nhận xét, lý do và cách sửa bằng tiếng Việt. ",
            "en": "Write observations, reasons and repairs in English. "}
READ_ERROR = {"zh": "未能完整读取实际剪辑帧，已保存拍摄进度。", "en": "The cut could not be read in full. Filming progress is saved.",
              "vi": "Chưa đọc được đầy đủ các khung hình thực tế. Tiến độ quay đã được lưu."}
SCAN = ("每页是本镜实际将输出的连续帧，绝对编号F从左到右、从上到下。看完每格，逐行报首末编号和实际可见的手、商品或动作。"
        "原始商品照片和角色定妆仅作参照。把同一时刻多出的同类商品、头戴一顶而手里还有一顶、错脸、商品漂浮或变形、"
        "手明显穿入实物、穿戴无动作瞬移、同镜偷切、无关产品广告、外加水印、乱码或烧入字幕的疑点报具体帧及归一化0–1000矩形。"
        "照常允许出框、遮挡、倒影、透视、抓住部分边缘以及两人一同扶正商品。左右两只耳杯可以同属一件商品。"
        "商品原有侧标和场地自然招牌不是额外广告。不要猜测服装缝线、亮点或模糊的小弧线属于哪家品牌。"
        "演员人像或商品跨时间格重复不等于同一帧两件。不要从裁切区域推测细节。"
        "这里只查像素穿帮及错物，既定剧情与镜头创意无需重评。")
NATIVE = ("独立复核这串按编号排列、实际将输出的原分辨率画面。对每个focus帧，只核对review_kind指定的那类问题和"
          "normalized_region所指区域。区域外的其他事物不能作为当前疑点成立的证据。"
          "只有可见的错误轮廓、第二件物品、两手离开且没有其他承托、错误脸、同镜跳变或清楚可辨的额外商标/字幕才记 confirmed。"
          "检查原本存在的商品标记、头发与帽檐、正常前后遮挡、透视、出框和倒影；确实被遮住或无法区分记 inconclusive，"
          "清楚正常记 clear。商标必须可读或有明确独特图形，衣物上的小缝线、反光或不可辨的弧线不能确认品牌。"
          "若确认错误，给出一条具体、简短、下一次拍摄本镜或重新选段能够执行的修正。输入中的绝对帧编号是画外标签。")
EFFECT = ("本镜有实际操作商品的动作。若持续可见的效果明确来自与本次操作无关的分离部件，而真正的作用部位仍在画中、没有参与，"
          "标 wrong_effect_source，框住实际发出处、正确部位和接收处。识别本来就能独立工作的附件、已经取得原料的部件、正常余滴或反光。")
EFFECT_NATIVE = ("对focus逐帧核对可见作用的发出处与接收处。先读本镜按序的小图确认物件此前的状态、是否真实接入或装过内容物，"
                 "再用带编号的原分辨率目标帧及局部看清实物。仅判normalized_region中的wrong_effect_source。即使商品外形完好，若实际"
                 "作用从已分离的错误零件持续发出，该零件代替这次操作指定的可见作用源，判confirmed并叙述证据及一条可拍的修正。"
                 "已盛装过内容物的附件、少量残滴、真实正常的作用源判clear；只见反光、遮住出发点、缺少判定所需前序画面记inconclusive。")


class Suspicion(BaseModel):
    first: int
    last: int
    kind: Literal["hand_or_support", "extra_product", "wear_or_carry_jump", "internal_jump", "distorted_product", "wrong_effect_source",
                  "wrong_actor", "unrelated_product", "extra_logo_or_text"]
    region: tuple[int, int, int, int] = Field(description="Visible region x0,y0,x1,y1, normalized 0 to 1000")
    observation: str = Field(min_length=3, max_length=260)

    @model_validator(mode="after")
    def valid_rectangle(self):
        x, y, r, b = self.region
        if self.first > self.last or not (0 <= x < r <= 1000 and 0 <= y < b <= 1000):
            raise ValueError("Identify a valid inclusive interval and visible rectangle")
        return self


class ReadRow(BaseModel):
    first: int
    last: int
    observation: str = Field(min_length=2, max_length=180)


def page_schema(first, last):
    expected = [(i, min(i + COLUMNS - 1, last)) for i in range(first, last + 1, COLUMNS)]

    class PageRead(BaseModel):
        rows: list[ReadRow] = Field(min_length=1, max_length=BATCH // COLUMNS)
        suspicions: list[Suspicion] = Field(max_length=BATCH)

        @model_validator(mode="after")
        def all_rows(self):
            if [(r.first, r.last) for r in self.rows] != expected:
                raise ValueError(f"Read exactly these inclusive grid rows in order: {expected}")
            if any(s.first < first or s.last > last for s in self.suspicions):
                raise ValueError("Only flag frames shown in the supplied pages")
            return self

    return PageRead


class NativeFrame(BaseModel):
    frame: int
    status: Literal["clear", "inconclusive", "confirmed"]
    observation: str = Field(min_length=3, max_length=300)


def native_schema(focus):
    class NativeRead(BaseModel):
        frames: list[NativeFrame] = Field(min_length=1, max_length=NATIVE_GROUP)
        correction: str = Field(default="", max_length=500)

        @model_validator(mode="after")
        def entire_focus(self):
            if [r.frame for r in self.frames] != focus:
                raise ValueError(f"Return one entry for each focus frame, in order: {focus}")
            if any(r.status == "confirmed" for r in self.frames) and not self.correction.strip():
                raise ValueError("A visible, confirmed defect needs a concrete correction")
            return self

    return NativeRead


def enabled(plan):
    return getattr(plan, "format", None) == recipe.FORMAT and getattr(plan, "score_revision", 1) == 2


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def visual_plan(plan):
    # Source preprocessing is not part of the photographed program. Earlier
    # saved plans have no such field and still own their original certificates.
    data = dict(plan.model_dump())
    data.pop("obscured_product_videos", None)
    return data


def plan_id(plan):
    # An edition frozen before the office roles and functional-causality checks can
    # be re-reviewed. Unaffected shot-level decisions remain individually reusable.
    return _digest("shot-roles-and-effect-source-v2", visual_plan(plan))


def window(cut):
    return [cut.get(key) for key in ("source", "start", "frames", "output_frames", "speed")]


def _product_state(plan, shot):
    """The travel carry is established after the home routine and ends at the office transfer."""
    i = shot["id"]
    if i < 10:
        return {"shot_opening": shot.get("frame", ""), "shot_ending": shot.get("end", ""),
                "role_and_timing": "Use the actual pickup, first use, insert or packing action in this shot. The later travel carry is not an earlier support or wear requirement."}
    if i < 25:
        return {"carry": plan.carry}
    guidance = {
        25: "The commuter enters with the item or use position. A confrontation can prepare the transfer. The authority has not taken over at the cut.",
        26: "The one transfer unfolds within this shot. Judge the visible support at each moment. The authority only has to take over by the end. Their reaction belongs to the following shot.",
        27: "The authority already controls the item or use position when this reaction starts. They may change their grip or set it down through a visible action. Their expression can take the whole shot to change. Do not request a new transfer, a dance or a smile at the opening.",
        28: "The authority is the dancer. Retain the role and product state established in the office, as visible in this wide view.",
    }
    mode = getattr(plan, "transfer_mode", "") or ("access" if plan.mode == "stationary" else "pass")
    state = {"transfer_mode": mode, "shot_opening": shot["frame"], "shot_ending": shot["end"],
             "role_and_timing": guidance[i]}
    if mode == "fit" and i in (26, 27):
        state["fitting_support"] = ("Once it is fitted, the authority's body supports the item. The giver's hands may still steady it "
                                    "across the edit and withdraw during the reaction. Their continued touch alone is physically normal.")
    return state


def _batches(index):
    first = sum(b.frames for b in recipe.BEATS[:index - 1])
    end = first + recipe.BEATS[index - 1].frames
    return [(n, min(n + BATCH, end) - 1) for n in range(first, end, BATCH)]


def certified(decisions, proof, plan, cuts=None):
    """Accept the playback review or an already completed, source-bound full-frame certificate."""
    if not enabled(plan):
        return True
    from light import commercial_playback_review as playback

    if proof.get("playback_check"):
        return playback.valid(decisions, proof, plan, cuts)
    checks, boards = proof.get("frame_checks") or [], proof.get("cut_assets") or []
    snapshot = visual_plan(plan)
    historical = {plan_id(plan), _digest(snapshot), _digest("office-roles-and-effect-source-v1", snapshot)}
    if len(checks) != len(recipe.BEATS) or len(boards) != len(checks) or proof.get("frame_plan") not in historical:
        return False
    for i, (key, board) in enumerate(zip(checks, boards, strict=True), 1):
        item = decisions.get(key) or {}
        groups = item.get("batches") or []
        if (item.get("version") not in {VERSION, "cut-pixels-v1"} or item.get("ok") is not True or item.get("shot") != i
                or item.get("board") != board or not item.get("window") or len(groups) != len(_batches(i))):
            return False
        if cuts is not None and item["window"] != window(cuts.get(i, {})):
            return False
        required_native = set()
        for group, (first, last) in zip(groups, _batches(i), strict=True):
            receipt = decisions.get(group) or {}
            scanned = receipt.get("report") or {}
            expected_rows = [(n, min(n + COLUMNS - 1, last)) for n in range(first, last + 1, COLUMNS)]
            rows = [(r.get("first"), r.get("last")) for r in scanned.get("rows", []) if r.get("observation")]
            if (rows != expected_rows or receipt.get("range") != [first, last] or not receipt.get("reviewed")
                    or len(receipt.get("sheets") or []) != math.ceil((last - first + 1) / PAGE)
                    or receipt.get("cut") != item.get("identity") or "suspicions" not in scanned):
                return False
            for suspicion in scanned["suspicions"]:
                a, b = suspicion.get("first", -1), suspicion.get("last", -1)
                if not first <= a <= b <= last or not suspicion.get("region"):
                    return False
                for start in range(a, b + 1, NATIVE_GROUP):
                    focus = list(range(start, min(start + NATIVE_GROUP, b + 1)))
                    native = f"{key}:native:{_digest(focus, suspicion.get('kind'), suspicion['region'])}"
                    required_native.add(native)
                    detail = decisions.get(native) or {}
                    frames = (detail.get("report") or {}).get("frames") or []
                    expected_count = min(_batches(i)[-1][1], focus[-1] + 2) - max(_batches(i)[0][0], focus[0] - 2) + 1
                    if (len(detail.get("full_frames") or []) != expected_count or not detail.get("reviewed") or detail.get("confirmed") != []
                            or [f.get("frame") for f in frames] != focus
                            or any(f.get("status") not in ("clear", "inconclusive") for f in frames)):
                        return False
                    if suspicion.get("kind") == "wrong_effect_source" and not detail.get("context_sheets"):
                        return False
        if set(item.get("native_checks") or []) != required_native:
            return False
    return True


def _jpeg(picture, quality=93):
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=quality, subsampling=0)
    return buffer.getvalue()


def _sheet(frames, first):
    sheet = Image.new("RGB", (COLUMNS * WIDTH, math.ceil(len(frames) / COLUMNS) * (HEIGHT + 22)), "#172024")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=16)
    for n, picture in enumerate(frames):
        x, y = n % COLUMNS * WIDTH, n // COLUMNS * (HEIGHT + 22)
        draw.text((x + 4, y + 1), f"F{first + n:04}", fill="#ffe3ac", font=font)
        sheet.paste(picture, (x, y + 22))
    return _jpeg(sheet)


def _native_detail(pictures, lo, focus, region):
    x, y, right, bottom = region
    if (right - x) * (bottom - y) > 550_000:
        return None
    w, h = pictures[0].size
    box = (max(0, x * w // 1000 - 15), max(0, y * h // 1000 - 15),
           min(w, math.ceil(right * w / 1000) + 15), min(h, math.ceil(bottom * h / 1000) + 15))
    tw, th = box[2] - box[0], box[3] - box[1]
    cols = min(2, len(focus))
    sheet = Image.new("RGB", (cols * tw, math.ceil(len(focus) / cols) * (th + 22)), "#172024")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=16)
    for i, number in enumerate(focus):
        left, top = i % cols * tw, i // cols * (th + 22)
        sheet.paste(pictures[number - lo].crop(box), (left, top + 22))
        draw.text((left + 3, top + 1), f"F{number:04}", fill="white", font=font)
    return _jpeg(sheet, 96)


async def _edited_cut(ctx, folder, cut, identity):
    source = folder / f"{cut['source']}.mp4"
    work = folder / f"pixel-review-{identity}"
    work.mkdir(exist_ok=True)
    local = work / f"source-{cut['shot']:02}.mp4"
    if not local.exists():
        if source.exists():
            local.hardlink_to(source)
        else:
            await product_story._download(ctx, cut["source"], local)
    # Use the same 24fps, retiming, end padding and 720p crop as the actual commercial compositor.
    return await product_story_post._clip(work, cut["shot"], {**cut, "has_audio": False, "dub": None},
                                          {"adaptive": True, "spoken_line": ""})


async def _frames(path, first, last, *, small, language):
    width, height = (WIDTH, HEIGHT) if small else (product_story_post.WIDTH, product_story_post.HEIGHT)
    vf = f"select=between(n\\,{first}\\,{last})" + (f",scale={width}:{height}:flags=lanczos" if small else "")
    count = last - first + 1
    try:
        raw = await product_story_post.ff("-threads", "1", "-i", path, "-an", "-vf", vf, "-fps_mode", "passthrough",
                                          "-frames:v", count, "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-threads:v", "1", "-f", "rawvideo", "-")
    except RemakeStopped as exc:
        raise RemakeStopped(READ_ERROR[language]) from exc
    stride = width * height * 3
    if len(raw) != count * stride:
        raise RemakeStopped(READ_ERROR[language])
    return [Image.frombytes("RGB", (width, height), raw[i * stride:(i + 1) * stride]) for i in range(count)]


async def inspect_cut(ctx, folder, prefix, run, plan, shot, cut, references, record):
    """Return a resumable decision for the exact edited pixels, including every transient frame."""
    i = cut["shot"]
    state = _product_state(plan, shot)
    phase = [] if "carry" in state else [state]
    effect = bool(shot["product_visible"] and i in {3, 4, 5, 6, 7, 8, 26, 27})
    if effect:
        phase.append(EFFECT)
    identity = _digest(VERSION, _digest(visual_plan(plan)), shot, window(cut), cut["review_board"], references, *phase)
    key = f"{recipe.REVIEW}:{VERSION}:{i}:{identity}"
    decisions = run.setdefault("director_decisions", {})
    if key in decisions:
        return key, decisions[key]
    base = {"version": VERSION, "identity": identity, "shot": i, "board": cut["review_board"], "window": window(cut)}
    first = _batches(i)[0][0]
    ranges = _batches(i)
    if (None in window(cut) or cut["start"] < 0 or cut["frames"] <= 0 or cut["speed"] <= 0
            or first + cut["output_frames"] - 1 != ranges[-1][1]):
        raise RemakeStopped(READ_ERROR[plan.language])
    edited = None

    async def read_pixels(start, end, *, small):
        nonlocal edited
        if edited is None:
            try:
                edited = await _edited_cut(ctx, Path(folder), cut, identity)
            except RemakeStopped as exc:
                raise RemakeStopped(READ_ERROR[plan.language]) from exc
        return await _frames(edited, start, end, small=small, language=plan.language)

    raw = None
    body = {"language": plan.language, "product": plan.product, "appearance": plan.appearance, **state,
            "mode": plan.mode, "action": shot["action"], "product_visibility": shot["product_visible"],
            "references": [label for label, asset in references], "caption_state": "Pre-composition. Dialogue captions and optional ending copy are added later."}
    if plan.mode == "stationary":
        body["installation"] = "Installed goods stay at their physical site. Outdoor empty-hand gestures and product-free inserts are intentional."
    refs = [asset for _, asset in references]
    batches, native_checks = [], []
    for start, end in ranges:
        group_key = f"{key}:read:{start}-{end}"
        page = decisions.get(group_key)
        if page is None:
            if raw is None:
                raw = await read_pixels(0, cut["output_frames"] - 1, small=True)
            sheets = []
            for offset in range(start, end + 1, PAGE):
                data = _sheet(raw[offset - first:min(end + 1, offset + PAGE) - first], offset)
                sheets.append(await _reference_asset(ctx, f"pixels-{identity}-{offset}", data))
            report = await reference_quality.ask(ctx, "commercial_pixels_in_order", page_schema(start, end),
                LANGUAGE[plan.language] + SCAN + (EFFECT if effect else ""), {**body, "frames": [start, end], "columns": COLUMNS,
                "image_order": "references, followed by chronological numbered pages"}, [*refs, *sheets],
                max_tokens=3400, timeout=85)
            page = await record(ctx, prefix, run, i, group_key, {**report, "cut": identity, "range": [start, end],
                                                                 "sheets": sheets, "reviewed": True})
        batches.append(group_key)
        for issue in page["report"]["suspicions"]:
            for at in range(issue["first"], issue["last"] + 1, NATIVE_GROUP):
                focus = list(range(at, min(at + NATIVE_GROUP, issue["last"] + 1)))
                native_key = f"{key}:native:{_digest(focus, issue['kind'], issue['region'])}"
                detail = decisions.get(native_key)
                if detail is None:
                    lo = max(first, at - 2)
                    hi = min(first + cut["output_frames"] - 1, focus[-1] + 2)
                    pictures = await read_pixels(lo - first, hi - first, small=False)
                    full = []
                    for number, picture in enumerate(pictures, lo):
                        full.append(await _reference_asset(ctx, f"pixels-{identity}-native-{number}", _jpeg(picture, 96)))
                    crop = _native_detail(pictures, lo, focus, issue["region"])
                    detail_sheet = []
                    if crop:
                        detail_sheet.append(await _reference_asset(ctx, f"pixels-{identity}-detail-{_digest(focus, issue['region'])}", crop))
                    causal = issue["kind"] == "wrong_effect_source"
                    context = list(dict.fromkeys([decisions[batches[0]]["sheets"][0], *page["sheets"]])) if causal else []
                    order = "references, " + ("chronological context sheets, " if causal else "") + "all 1280x720 output frames in native_order, then optional labeled 1:1 crops"
                    result = await reference_quality.ask(ctx, "commercial_pixels_native", native_schema(focus),
                        LANGUAGE[plan.language] + (EFFECT_NATIVE if causal else NATIVE), {**body, "focus": focus, "native_order": list(range(lo, hi + 1)),
                        "normalized_region": issue["region"], "review_kind": issue["kind"], "detail_sheet_last": bool(detail_sheet),
                        "image_order": order, **({"context_sheet_count": len(context)} if causal else {})},
                        [*refs, *context, *full, *detail_sheet], max_tokens=1800, timeout=85)
                    confirmed = [r for r in result["report"]["frames"] if r["status"] == "confirmed"]
                    detail = await record(ctx, prefix, run, i, native_key, {**result, "full_frames": full, "detail_sheets": detail_sheet,
                                          **({"context_sheets": context} if causal else {}),
                                          "focus": focus, "confirmed": confirmed, "reviewed": True})
                native_checks.append(native_key)
                if detail["confirmed"]:
                    defect = detail["confirmed"][0]
                    item = {**base, "ok": False, "batches": batches, "native_checks": native_checks,
                            "reason": f"F{defect['frame']:04} · {defect['observation']}", "correction": detail["report"]["correction"]}
                    return key, await record(ctx, prefix, run, i, key, item)
    return key, await record(ctx, prefix, run, i, key, {**base, "ok": True, "batches": batches, "native_checks": native_checks})


async def inspect(ctx, folder, prefix, run, plan, cuts, material, cast, record):
    shots = recipe.shots(plan)

    async def examine(shot):
        i = shot["id"]
        refs = [("original product", photo) for photo in material]
        if i < 28 and cast.get("protagonist"):
            refs.append(("protagonist face", cast["protagonist"]))
        if i >= 25 and cast.get("authority"):
            refs.append(("authority face", cast["authority"]))
        return await inspect_cut(ctx, folder, prefix, run, plan, shot, cuts[i], refs, record)

    return await _parallel(shots, examine, concurrency=3)


def _first_match_recheck(current, affected, evidence):
    if not evidence:
        return None
    decisions = current.get("director_decisions") or {}
    key = evidence.get("key", "")
    check = decisions.get(key) or {}
    prior_key = check.get("replaces_review") or ""
    prior = decisions.get(prior_key) or {}
    old_cut = prior.get("cut") or {}
    saved = next((c for c in current.get("wan_cuts") or [] if c.get("shot") == 11), {})
    report = check.get("report") or {}
    filmed = evidence.get("source")
    board = evidence.get("old_board")
    accepted = check.get("ok")
    new_cut = check.get("cut") or {}
    saved_board = bool(board and old_cut.get("review_board") == board == saved.get("review_board"))
    old_source = bool(filmed and check.get("source") == filmed == old_cut.get("source"))
    bound = (isinstance(prior_key, str) and prior_key.startswith(f"{recipe.REVIEW}:take:11:") and key == f"{prior_key}:recut-first-match-entry-v1"
             and prior.get("ok") is True and old_cut.get("shot") == evidence.get("shot") == 11 and old_source
             and saved_board and window(old_cut) == window(saved) and None not in window(old_cut)
             and "has_clear_approach" not in (prior.get("report") or {}))
    images = check.get("evidence")
    independent = (isinstance(images, list) and images and all(isinstance(asset, str) and asset for asset in images)
                   and report.get("entry_path") and isinstance(report.get("has_clear_approach"), bool)
                   and isinstance(report.get("reached_feature"), bool))
    revised_window = (new_cut.get("shot") == 11 and new_cut.get("source") == filmed and new_cut.get("review_board")
                      and None not in window(new_cut) and window(new_cut) != window(saved)
                      and report.get("has_clear_approach") and report.get("reached_feature"))
    if (set(affected) not in ({11}, {11, 12}) or not bound or not independent
            or not (accepted is False or accepted is True and revised_window)):
        raise RemakeStopped("第一次接点的新审片尚未核实旧剪段。", vi="Chưa xác minh được lần duyệt lại đoạn nối động tác đầu tiên.",
                            en="The independent review of the first match has not verified the saved interval.")
    return {"key": key, "replaces_review": prior_key, "source": filmed, "old_board": board, "ok": accepted}


async def reopen_saved(ctx, prefix, run, affected, issues, *, verified_first_match=None):
    """An unpublished legacy cut may already have frozen an interval that the new pixel gate rejects."""
    from light import commercial_playback_review as playback

    rejected = [issue["identity"] for issue in issues if issue.get("version") == VERSION and issue.get("ok") is False]
    visible = [issue["identity"] for issue in issues if issue.get("version") == playback.VERSION and issue.get("ok") is False]
    if not (rejected or visible or verified_first_match) or not any(c["shot"] in affected for c in run.get("wan_cuts") or []):
        return
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from light import production

    async with get_db_session() as db:
        row = await production._lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        current = form.get("run") or {}
        if current.get("job_prefix") != prefix or current.get("wan_review_version") != recipe.REVIEW:
            raise RemakeStopped("当前剪辑已属于另一版制作。", vi="Các cảnh này thuộc về một phiên bản dựng khác.",
                                en="These cuts belong to another edition.")
        previous = current.get("wan_cuts") or []
        if not any(c["shot"] in affected for c in previous):
            for field in ("wan_final_key", "wan_quality", "wan_coverage"):
                run.pop(field, None)
            run.update(current)
            return
        if previous != run.get("wan_cuts"):
            raise RemakeStopped("所选镜头区间已更新。", vi="Các đoạn quay đã chọn đã được cập nhật.",
                                en="The selected source intervals have changed.")
        first_match = _first_match_recheck(current, affected, verified_first_match)
        if current.get("wan_final_key"):
            job = await db.scalar(select(VideoJob).where(VideoJob.session_id == ctx.session_id,
                                  VideoJob.idempotency_key == current["wan_final_key"]).with_for_update())
            if job and (job.attached_message_id or job.status not in {"completed", "failed", "canceled", "cancelled"}):
                raise RemakeStopped("已发布或正被提交的成片保持原版。",
                                    vi="Bản đã phát hành hoặc đang được gửi vẫn giữ nguyên.",
                                    en="The published or in-flight edition is already locked.")
        history = [*current.get("wan_edit_history", []), {"version": current["wan_review_version"],
                   "cuts": previous, "final_key": current.get("wan_final_key"), "pixel_rejections": rejected,
                   **({"playback_rejections": visible} if visible else {}),
                   **({"first_match_recheck": first_match} if first_match else {}),
                   **{k: current[k] for k in ("wan_quality", "wan_coverage") if k in current}}]
        updated = {key: value for key, value in current.items() if key not in {"wan_final_key", "wan_quality", "wan_coverage"}}
        updated.update(wan_edit_history=history, wan_cuts=[c for c in previous if c["shot"] not in affected])
        row.light_production = {**form, "run": updated}
        row.updated_at = datetime.now(UTC)
    for key in ("wan_final_key", "wan_quality", "wan_coverage"):
        run.pop(key, None)
    run.update(updated)
