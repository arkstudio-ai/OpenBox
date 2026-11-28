"""Review the actual product and the whole edit, with bounded, recorded corrections."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from typing import Literal

import httpx
from PIL import Image, ImageDraw, ImageStat
from pydantic import BaseModel, Field, ValidationError

from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from light import wan_edit
from light.remake import RemakeStopped, _check, _parallel, _reference_asset
from tool.footage_analyze import parse_picture
from video import vision_contract

VERSION = "product-and-story-v2"
MIN_CONFIDENCE = 0.85
MAX_REPAIRS = 2


class ProductReport(BaseModel):
    subject: Literal["product", "story_prop"]
    matches: bool
    stable: bool
    confidence: float = Field(ge=0, le=1)
    observed_product: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    action: Literal["keep", "omit", "retake"]
    correction: str = Field(default="", max_length=700)

    @property
    def accepted(self):
        return self.matches and self.stable and self.confidence >= MIN_CONFIDENCE and self.action == "keep"

    def accepts_shot(self, shot):
        return self.accepted and (shot.kind != "product" or self.subject == "product")


class Issue(BaseModel):
    shot: int = Field(ge=1, le=12)
    action: Literal["omit", "retake"]
    reason: str = Field(min_length=1, max_length=500)
    correction: str = Field(default="", max_length=700)


class StoryReport(BaseModel):
    coherent: bool
    cast_consistent: bool
    props_continuous: bool
    natural_actions: bool
    product_consistent: bool
    reason: str = Field(min_length=1, max_length=800)
    issues: list[Issue] = Field(max_length=12)
    order: list[int] | None = Field(default=None, max_length=12)

    @property
    def accepted(self):
        return all((self.coherent, self.cast_consistent, self.props_continuous,
                    self.natural_actions, self.product_consistent)) and not self.issues


class FinalReport(BaseModel):
    story_visible: bool
    product_visible: bool
    title_readable: bool
    no_visual_defect: bool
    reason: str = Field(min_length=1, max_length=700)

    @property
    def accepted(self):
        return all((self.story_visible, self.product_visible, self.title_readable, self.no_visual_defect))


def fingerprint(cuts):
    return hashlib.sha256(json.dumps(cuts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def lettering_palette(images):
    """Choose ink from measured title-area backgrounds, not a fixed light-on-light overlay."""
    backgrounds = []
    for image in images:
        image = image.convert("RGB")
        w, h = image.size
        region = image.crop((int(w * 0.3), int(h * 0.05), int(w * 0.97), int(h * 0.4)))
        backgrounds.append(ImageStat.Stat(region).median)
    if not backgrounds:
        raise ValueError("Actual hero frames are required to choose readable lettering")

    def luminance(rgb):
        channels = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return sum(a * b for a, b in zip(channels, (0.2126, 0.7152, 0.0722)))

    def contrast(ink, background):
        a, b = luminance(ink), luminance(background)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    options = [("#FFF8E9", (255, 248, 233), "#3B2618"), ("#3B2618", (59, 38, 24), "#FFF8E9")]
    ink, rgb, outline = max(options, key=lambda candidate: min(contrast(candidate[1], bg) for bg in backgrounds))
    return {"ink": ink, "outline": outline, "background_rgb": backgrounds,
            "minimum_contrast": round(min(contrast(rgb, bg) for bg in backgrounds), 3)}


async def title_style(ctx, hero, state):
    ids = state["products"][hero["source"]]["evidence"][-4:]
    images = []
    for asset_id in ids:
        async with get_db_session() as db:
            row = await db.get(FileAsset, asset_id)
            if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready":
                raise RemakeStopped("片尾实际画面不可读取，不能猜测题字背景。")
        data = await get_oss().get_object(row.oss_key)
        images.append(Image.open(io.BytesIO(data)))
    return {**lettering_palette(images), "evidence": ids}


def validate_selection(plan, original, selected, omissions):
    """Omitting an optional insert must never erase dialogue, story beats or the hero."""
    count = len(plan.shots)
    if [c["shot"] for c in original] != list(range(1, count + 1)):
        raise ValueError("The original review must account for every planned shot")
    indices = [c["shot"] for c in selected]
    omitted = [c["shot"] for c in omissions]
    if (len(set(indices)) != len(indices) or not indices or indices[-1] != count or len(omitted) != len(set(omitted))
            or set(indices) & set(omitted) or set(indices) | set(omitted) != set(range(1, count + 1))):
        raise ValueError("Every planned shot needs a unique retained or omitted decision")
    for item in omissions:
        shot = plan.shots[item["shot"] - 1]
        if not shot.can_omit or not item.get("reason") or not item.get("evidence"):
            raise ValueError("Only evidenced, silent optional shots may be omitted")
    if len(selected) < 5 or sum(c["duration_sec"] for c in selected) < 18:
        raise ValueError("The selection must retain a complete film")
    if len({c["source"] for c in selected}) != len(selected):
        raise ValueError("Duplicate takes cannot stand in for missing story shots")
    for cut in selected:
        if cut.get("speed") is not None:
            speed = cut["speed"]
            if (not 0.25 <= speed <= 4 or cut.get("cues") or cut["shot"] < 1 or plan.shots[cut["shot"]-1].line
                    or not math.isclose(cut.get("source_duration_sec", 0)/speed, cut["duration_sec"], abs_tol=0.002)):
                raise ValueError("Invalid timing for a silent source interval")
        if cut.get("hold_asset") and (plan.shots[cut["shot"]-1].kind != "product" or
                                      not cut["first"] <= cut.get("hold_frame", -1) <= cut["last"]):
            raise ValueError("An end-card hold needs an observed frame from its product shot")
        if cut.get("omit_reason") or not cut.get("source") or cut["in_sec"] < 0 or cut["duration_sec"] < 0.5:
            raise ValueError("Invalid retained media interval")
        if plan.shots[cut["shot"] - 1].line and not cut.get("cues"):
            raise ValueError("A dialogue shot cannot silently lose its line")
        for cue in cut.get("cues") or []:
            if cue["from_sec"] < cut["in_sec"] - 0.01 or cue["to_sec"] > cut["in_sec"] + cut["duration_sec"] + 0.01:
                raise ValueError("The selection truncates measured dialogue")


def delivery_cuts(run):
    """Legacy completed edits remain readable; new quality-gated edits need a signed-off selection."""
    original = run.get("wan_cuts") or []
    quality = run.get("wan_quality")
    if quality is None:
        return original
    try:
        from light.reference_plan import ShotPlan

        if (quality.get("version") != VERSION or quality.get("input_digest") != fingerprint(original)
                or quality.get("status") != "passed"
                or not StoryReport.model_validate(quality["story"]["report"]).accepted):
            return None
        cuts = quality["cuts"]
        plan = ShotPlan.model_validate(run["remake_adaptation"])
        validate_selection(plan, original, cuts, quality.get("omissions") or [])
        for cut in cuts:
            if plan.shots[cut["shot"] - 1].kind in {"detail", "product"}:
                report = quality["products"][cut["source"]]["report"]
                if not ProductReport.model_validate(report).accepts_shot(plan.shots[cut["shot"] - 1]):
                    return None
        return cuts
    except (KeyError, TypeError, ValueError):
        return None


async def owned_images(ctx, ids):
    urls = {}
    async with get_db_session() as db:
        for asset_id in dict.fromkeys(ids):
            row = await db.get(FileAsset, asset_id)
            if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready" or not row.mime.startswith("image/"):
                raise RemakeStopped("审片参考图片不可读取，已保留生成素材。",
                                    vi="Chưa đọc được ảnh tham chiếu để duyệt cảnh. Tư liệu đã tạo được lưu.",
                                    en="The reference image for review could not be read. The generated footage was saved.")
            urls[get_oss().presign_get(row.oss_key)] = row.id
    return urls


async def ask(ctx, name, schema, guide, data, images, *, attempts=2, max_tokens=6000, timeout=90,
              reasoning_effort=None):
    import litellm
    from agent.llm import _get_provider_kwargs
    from trajectory.capture import capture_service_dispatch, service_scope

    urls = await owned_images(ctx, images)
    by_asset = {asset: url for url, asset in urls.items()}
    for alias in dict.fromkeys(asset for asset in images if asset not in by_asset):
        # Distinct owned asset rows can point at the same stored bytes.
        by_asset[alias] = next(iter(await owned_images(ctx, [alias])))
    messages = [{"role": "user", "content": [
        {"type": "text", "text": guide + "\n" + json.dumps(data, ensure_ascii=False)},
        *({"type": "image_url", "image_url": {"url": by_asset[asset]}} for asset in images),
    ]}]
    function = {"name": name, "description": "Submit an evidence-based advertising quality decision.",
                "parameters": schema.model_json_schema()}
    model = vision_contract.selected_model(getattr(ctx, "model_id", None))
    error = ""
    for _ in range(attempts):
        await _check(ctx)
        try:
            route = vision_contract.request_options(model, name, _get_provider_kwargs(model),
                                                    reasoning_effort=reasoning_effort)
            options = dict(model=model, messages=messages, temperature=0.1, max_tokens=max_tokens, timeout=timeout,
                           tools=[{"type": "function", "function": function}], **route)
            async with (service_scope(ctx, asset_urls=urls), capture_service_dispatch(
                purpose="video_quality", provider="agent_vision", model=model, operation=name,
                profile=VERSION, body={"messages": messages, "tools": options["tools"],
                                       "tool_choice": options["tool_choice"], "extra_body": options.get("extra_body")},
            ) as trace):
                answer = await asyncio.wait_for(litellm.acompletion(**options), timeout + 5)
                content = vision_contract.output(answer, name)
                await trace.chunk(vision_contract.response_evidence(answer, content))
            report = schema.model_validate(parse_picture(content), context=data)
            return {"report": report.model_dump(), "model": model, "evidence": images}
        except asyncio.CancelledError:
            raise
        except ValidationError as exc:
            error = type(exc).__name__
            invalid = [{"field": ".".join(map(str, item["loc"])), "problem": item["msg"]}
                       for item in exc.errors(include_input=False)[:8]]
            messages.append({"role": "user", "content": "Return the full corrected result. "
                             "Correct these fields while preserving the other directions. "
                             + json.dumps(invalid, ensure_ascii=False)})
        except Exception as exc:
            from question.runtime import RunRevoked
            if isinstance(exc, RunRevoked):
                raise
            error = type(exc).__name__
    language = data.get("language")
    if language == "vi":
        raise RemakeStopped(f"Chưa nhận được kết quả kiểm tra hợp lệ cho quảng cáo ({error}).")
    if language == "en":
        raise RemakeStopped(f"The advertising review returned no valid result ({error}).")
    raise RemakeStopped(f"广告质量审片暂未返回有效结果（{error}），不能跳过检查交付。")


async def _sample_frames(ctx, client, cut, positions, width):
    async with get_db_session() as db:
        row = await db.get(FileAsset, cut["source"])
        if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready":
            raise RemakeStopped("待审片的实际视频不可读取。")
    fps = cut["fps"]

    async def frame(position):
        at = cut["in_sec"] + cut.get("source_duration_sec", cut["duration_sec"]) * position
        number = cut.get("hold_frame")
        if number is None:
            number = min(cut["last"], max(cut["first"], math.floor(at * fps)))
        data = await wan_edit.exact_frame(client, get_oss(), row.oss_key, number, fps, width)
        return number, data

    return await _parallel(positions, frame, concurrency=4)


async def sample(ctx, cut, positions=(0.06, 0.35, 0.65, 0.94), width=540):
    async with httpx.AsyncClient(timeout=30) as client:
        frames = await _sample_frames(ctx, client, cut, positions, width)
    return [(number, await _reference_asset(ctx, f"quality-{cut['source']}-{number}-{width}", data))
            for number, data in frames]


def filmed_action(cut):
    if cut.get("hold_asset"):
        return "实拍商品定帧，稳定展示商品和品牌题字。"
    return (cut.get("direction") or {}).get("action") or cut.get("filming_instruction") or ""


def matching_setup(plan, cuts, index):
    shot = plan.shots[index - 1]
    if not shot.speaker or shot.kind != "scene":
        return None
    available = (c for c in cuts if c["shot"] != index and c.get("first_reference")
                 and plan.shots[c["shot"] - 1].scene == shot.scene
                 and plan.shots[c["shot"] - 1].speaker == shot.speaker)
    return min(available, key=lambda c: (abs(c["shot"] - index), c["shot"]), default=None)


async def product(ctx, plan, cut, materials, *, reference_copy=""):
    frames = await sample(ctx, cut)
    guide = (
        "你是广告插镜质检员。先读intended_shot及前后分镜，判断本镜用途，不能因为kind=detail就要求每镜都有商品。"
        "subject=product表示剧本要求本镜展示或处理实际商品；subject=story_prop表示剧本仅要求手机、酒杯等剧情道具。"
        "用途由原分镜决定，不能因为商品镜头拍错或拍不到商品而改成story_prop来放行。最后kind=product必须subject=product。"
        "approved_filming_action是拍摄时的意图，用来理解素材。stable检查商品本身在动作中是否变形、手和道具是否穿帮。镜头推拉、锅耳碰边和摆盘取景由剪辑导演自行决定，商品与题字自然清楚时可直接保留。"
        "具体表演和机位可以改变。依据用户创意、实际商品和已核定拍法判断；构图切到器皿边缘或装饰细节不同，只在实际妨碍辨认商品、叙事或题字时才判为失败。始终独立比较实拍与用户原图，核对真实商品、剧情必要动作和前后状态。"
        "剧情道具镜没有商品是正常的，matches检查是否符合该镜用途与道具状态，confidence检查对此的把握；"
        "绝不能为了商品一致性把手机、空桌等镜头改成提前上菜或增加一锅，不改变叙事顺序。"
        "前面的图片是用户原始商品，后四张是实际生成视频在保留区间内的连续采样帧。"
        "对需要出现商品的镜头，独立观察实际商品是什么，不因为剧本写了商品名称就假定正确。比较品类、解剖结构、材质、形状、尺度、锅具和完整动作。"
        "检查手指、筷子、食物是否变形、悬空、穿插，商品不能在动作中变成另一种。"
        "例如小龙虾肉如果看起来像普通大虾、剥壳后无法确认，就说明不确定，不算正确的宣传特写。"
        "商品镜的confidence是对实际商品身份及稳定性的把握，低于0.85不能keep；两种用途的matches、stable都必须为true才能keep。"
        "按这条广告选择的拍法判断商品是否清晰可信。reason必须引用实际看见的证据，observed_product写实际观察。"
        "不合格时按实际叙事选择：can_omit允许且剪除更好时给omit；需要导演试剪、改变拍法或补拍时给retake并描述问题。"
        "若应重拍，correction具体描述可靠替代构图与简单动作，并对照原始商品。不得添加字幕或改变剧情、对白。"
        "调用check_ad_product返回完整结构；不能调用函数时只返回同结构JSON。"
    )
    return await ask(ctx, "check_ad_product", ProductReport, guide,
                     {"user_reference_copy": reference_copy, "product": plan.product, "appearance": plan.appearance,
                      "source_images": len(materials), "shot": cut["shot"],
                      "kind": plan.shots[cut["shot"] - 1].kind,
                      "can_omit": plan.shots[cut["shot"] - 1].can_omit,
                      "intended_shot": plan.shots[cut["shot"] - 1].model_dump(),
                      "approved_filming_action": filmed_action(cut),
                      "previous_shot": plan.shots[cut["shot"] - 2].model_dump() if cut["shot"] > 1 else None,
                      "next_shot": plan.shots[cut["shot"]].model_dump() if cut["shot"] < len(plan.shots) else None,
                      "sample_frames": [f for f, _ in frames]},
                     [*materials, *(aid for _, aid in frames)])


async def story(ctx, plan, cuts, materials, anchor, *, reference_copy=""):
    async with httpx.AsyncClient(timeout=30) as client:
        async def story_frames(cut):
            return await _sample_frames(ctx, client, cut, (0.07, 0.5, 0.93), 270)

        # The model sees the numbered boards. Build them directly from the same exact frames.
        samples = await _parallel(cuts, story_frames, concurrency=2, stop_on_error=True)
    boards = []
    for start in range(0, len(cuts), 3):
        batch = cuts[start:start + 3]
        board = Image.new("RGB", (810, len(batch) * 506), "#202020")
        draw = ImageDraw.Draw(board)
        for row, cut in enumerate(batch):
            for col, (number, data) in enumerate(samples[start + row]):
                im = Image.open(io.BytesIO(data)).convert("RGB")
                board.paste(im.resize((270, 480)), (col * 270, row * 506 + 26))
                draw.text((col * 270 + 6, row * 506 + 5), f"SHOT {cut['shot']} / frame {number}", fill="white")
        buf = io.BytesIO()
        board.save(buf, "JPEG", quality=90)
        boards.append(await _reference_asset(ctx, "story-quality-" + hashlib.sha256(buf.getvalue()).hexdigest()[:20], buf.getvalue()))
    guide = (
        "你是独立终审剪辑师。第一张是演员身份锚图，接着是用户商品原图，最后是按成片顺序排列的实际视频采样板。"
        "每行标注原分镜SHOT号，三帧分别是该镜入、中、出点，已省略的编号是可选镜。"
        "按这条广告自己选定的创意、实拍与对白检查叙事和商品呈现，节奏与接镜应顺畅。"
        "重点看本片出镜人物的身份和视线，核对每一个关键道具出场、转移与数量的一致性。"
        "检查产品品类与形态、不自然手部或入口动作、前后突变。允许合理正反打、时间跳切和场景转换。"
        "依据观众在正常播放中能否理解广告来判断。已讲清的情节允许用跳切、省略与画外动作，不需逐字重现原分镜的装饰性表演。不要把正常镜头角度变化或采样瞬间表情认作明显缺陷。"
        "如有approved_coverage，按这次改拍的实际取景判断。单人近景可由视线与反打交代听者，"
        "道具连续性只比较前后都清楚展示的同一区域。真正错人、错误说话人、同一动作里重复出现商品等仍是缺陷。判定需要修正时写明它如何妨碍这条广告，机位、盘沿和背景陈设可由导演适配。"
        "有影响观感的具体缺陷则给issues，写原SHOT号、实际证据、omit或retake及修正要求。"
        "可以omit明确可省的无对白镜。保留必要剧情与末尾商品；如果剪点、镜头或表演有问题，填retake并把目标交给导演自主试剪或拍摄。如果改变现有镜头顺序能改善衔接，order填全部当前镜号的新排列，片尾商品保留最后，无需变动则留空。若实剪不足18秒，指出可延长的实际片段并说清相差多少秒。可延长现有实拍，商品也可采用真实帧定格展示宣传字。"
        "全部通过才给所有检查true及空issues。发现问题不能只写reason，必须给可执行issues。"
        "题字将在合成后另检，此处的采样编号不是成片字幕。调用check_ad_story或只返回同结构JSON。"
    )
    return await ask(ctx, "check_ad_story", StoryReport, guide,
                     {"user_reference_copy": reference_copy, "product": plan.product, "cast": plan.setting_and_cast,
                      "source_images": len(materials), "duration_sec": sum(c["duration_sec"] for c in cuts), "shots": [
                          {"shot": c["shot"], "description": plan.shots[c["shot"] - 1].model_dump(),
                           "approved_filming_action": filmed_action(c), "approved_coverage": c.get("coverage"), "can_omit": plan.shots[c["shot"] - 1].can_omit,
                           "heard": c.get("heard"), "duration": c["duration_sec"]} for c in cuts]},
                     [anchor, *materials, *boards])


async def finish(ctx, plan, original, materials, anchor, state, save, repair, *, reference_copy=""):
    """The model chooses corrections; the executor bounds cost and preserves mandatory story beats."""
    from copy import deepcopy
    recording = asyncio.Lock()
    review_context = {"reference_copy": reference_copy} if reference_copy else {}
    async def checkpoint():
        async with recording:
            await save(deepcopy(state))

    # An unfinished older edit may have replaced a valid shot solely to restore
    # staging which its director had already changed. Reassess the base cuts
    # with the actual shooting instructions; preserve the earlier review as evidence.
    recheck_intent = (state and state.get("status") != "passed" and not state.get("uses_filming_intent")
                      and any(filmed_action(cut) for cut in original))
    if not state or state.get("input_digest") != fingerprint(original) or state.get("version") != VERSION or recheck_intent:
        history = [state] if state else []
        state = {"version": VERSION, "input_digest": fingerprint(original), "status": "reviewing",
                 "cuts": list(original), "omissions": [], "products": {}, "actions": [], "story_history": [],
                 "previous_reviews": history, "uses_filming_intent": True}
        await checkpoint()
    if state["status"] == "passed":
        return state

    async def correct(issue, evidence):
        index = issue.shot
        cut = next((c for c in state["cuts"] if c["shot"] == index), None)
        if cut is None:
            raise RemakeStopped("审片修正引用了不在当前成片中的镜头。")
        proposed = [c for c in state["cuts"] if c["shot"] != index]
        omission = {"shot": index, "source": cut["source"], "reason": issue.reason, "evidence": evidence}
        if issue.action == "omit":
            try:
                validate_selection(plan, original, proposed, [*state["omissions"], omission])
            except ValueError:
                issue = issue.model_copy(update={"action": "retake"})
            else:
                state["cuts"] = proposed
                state["omissions"].append(omission)
                state["actions"].append({**issue.model_dump(), "source": cut["source"], "evidence": evidence})
                await checkpoint()
                return
        past = [a for a in state["actions"] if a.get("shot") == index and a["action"] == "retake"]
        unpaid = {"recut", "retime", "hold", "omit"}
        paid = sum(a.get("method") not in unpaid for a in past)
        if len(past) >= 5:
            raise RemakeStopped(f"第{index}镜已尝试多种剪法仍未通过，已保留完整记录。")
        feedback = issue.reason + "。" + issue.correction
        if paid >= MAX_REPAIRS:
            revised = await repair(index, feedback, len(past) + 1, existing_only=True)
            if revised.get("director_action") not in unpaid:
                raise RemakeStopped(f"第{index}镜自动修正两次后仅可使用现有素材。")
        else:
            revised = await repair(index, feedback, len(past) + 1)
        if revised.get("omit_reason"):
            removal = {"shot": index, "source": cut["source"], "reason": revised["omit_reason"],
                       "evidence": revised["omit_evidence"]}
            proposed = [c for c in state["cuts"] if c["shot"] != index]
            validate_selection(plan, original, proposed, [*state["omissions"], removal])
            state["cuts"] = proposed
            state["omissions"].append(removal)
            state["actions"].append({**removal, "action": "omit"})
            await checkpoint()
            return
        state["cuts"] = [revised if c["shot"] == index else c for c in state["cuts"]]
        state["actions"].append({**issue.model_dump(), "action": "retake", "method": revised.get("director_action"), "source": cut["source"],
                                 "replacement": revised["source"], "evidence": evidence,
                                 **({"setup_reference": revised["setup_reference"]} if revised.get("setup_reference") else {})})
        await checkpoint()

    product_checkpoint = asyncio.Lock()

    async def inspect_product(cut):
        entry = await product(ctx, plan, cut, materials, **review_context)
        async with product_checkpoint:
            state["products"][cut["source"]] = {**entry, "filmed_action": filmed_action(cut),
                                               "interval": [cut["first"], cut["last"]]}
            await checkpoint()

    # A director may park an unusable optional take on its first review. Decide
    # against the length of the actual other takes before making that omission final.
    while any(c.get("omit_reason") for c in state["cuts"]):
        pending = [c for c in state["cuts"] if c.get("omit_reason")]
        kept = [c for c in state["cuts"] if not c.get("omit_reason")]
        omissions = [{"shot": c["shot"], "source": c["source"], "reason": c["omit_reason"],
                      "evidence": c["omit_evidence"]} for c in pending]
        try:
            validate_selection(plan, original, kept, [*state["omissions"], *omissions])
        except ValueError:
            cut = pending[0]
            await correct(Issue(shot=cut["shot"], action="retake", reason=cut["omit_reason"],
                                correction="其余实拍不足以单独成片，本镜需要提供可用时长。"), cut["omit_evidence"])
        else:
            state["cuts"] = kept
            state["omissions"].extend(omissions)
            state["actions"].extend({**o, "action": "omit"} for o in omissions)
            await checkpoint()

    async def correct_batch(issues):
        # Optional omissions compete for the same minimum duration; serialize those.
        # Repairs to required shots can shoot and review in parallel.
        optional, groups = [], []
        for item in issues:
            if plan.shots[item[0].shot - 1].can_omit:
                optional.append(item)
            else:
                groups.append([item])
        async def apply(group):
            for issue, evidence in group:
                await correct(issue, evidence)
        await _parallel(([optional] if optional else []) + groups, apply, concurrency=2, stop_on_error=True)

    for _ in range(8):
        issues = []
        inserts = [cut for cut in state["cuts"] if plan.shots[cut["shot"] - 1].kind in {"detail", "product"}]
        pending = [cut for cut in inserts if cut["source"] not in state["products"]
                   or state["products"][cut["source"]].get("filmed_action", "") != filmed_action(cut)
                   or state["products"][cut["source"]].get("interval", [cut["first"], cut["last"]]) != [cut["first"], cut["last"]]]
        await _parallel(pending, inspect_product, concurrency=2, stop_on_error=True)
        for cut in inserts:
            entry = state["products"][cut["source"]]
            report = ProductReport.model_validate(entry["report"])
            if not report.accepts_shot(plan.shots[cut["shot"] - 1]):
                issues.append((Issue(shot=cut["shot"], action="omit" if report.action == "omit" else "retake",
                                     reason=report.reason, correction=report.correction), entry["evidence"]))
        if issues:
            await correct_batch(issues)
            continue
        selection = fingerprint(state["cuts"])
        entry = next((x for x in reversed(state["story_history"]) if x.get("selection") == selection), None)
        if entry is None:
            entry = {**await story(ctx, plan, state["cuts"], materials, anchor, **review_context), "selection": selection}
            state["story_history"].append(entry)
            await checkpoint()
        report = StoryReport.model_validate(entry["report"])
        if report.order and report.order != [c["shot"] for c in state["cuts"]]:
            indexed = {c["shot"]: c for c in state["cuts"]}
            if (len(report.order) != len(indexed) or set(report.order) != set(indexed)
                    or report.order[-1] != len(plan.shots)):
                raise RemakeStopped("终审提出的接镜顺序遗漏或重复了镜头，已保留素材。")
            state["cuts"] = [indexed[i] for i in report.order]
            state["actions"].append({"action": "reorder", "order": report.order, "reason": report.reason})
            await checkpoint()
            continue
        length = sum(c["duration_sec"] for c in state["cuts"])
        if report.accepted and length < 18:
            timing = entry.get("timing_issue")
            if timing is None:
                timing = await ask(ctx, "direct_ad_timing", Issue,
                                   "目前实际剪辑不足18秒。看这些实际素材，选最适合多留动作、产品或反应的镜头。"
                                   "给出原镜号、reason和可执行的延长目标；已有素材可用时优先尝试重新选剪点。",
                                   {"duration": length, "reference_copy": reference_copy,
                                    "cuts": [{"shot": c["shot"], "duration": c["duration_sec"], "intent": filmed_action(c)}
                                             for c in state["cuts"]]}, entry["evidence"])
                entry["timing_issue"] = timing
                await checkpoint()
            issue = Issue.model_validate(timing["report"]).model_copy(update={"action": "retake"})
            await correct(issue, timing["evidence"])
            continue
        if report.accepted:
            validate_selection(plan, original, state["cuts"], state["omissions"])
            state.update(status="passed", story=entry)
            await checkpoint()
            return state
        if not report.issues or len({i.shot for i in report.issues}) != len(report.issues):
            raise RemakeStopped("终审发现问题但没有给出唯一可执行的修正镜头，不能直接交付。")
        await correct_batch([(issue, entry["evidence"]) for issue in report.issues])
    raise RemakeStopped("自动审片与修正达到本轮上限，已保留生成素材，不交付未通过的视频。")


async def final(ctx, plan, output, meta, cuts, shop, materials):
    """Inspect the actual IMS result, including title visibility, before attachment."""
    fps, duration = float(meta["fps"]), float(meta["duration_sec"])
    at, positions = 0.0, []
    for cut in cuts:
        positions.append((at + cut["duration_sec"] * 0.5) / duration)
        at += cut["duration_sec"]
    positions.extend([(duration - 0.65) / duration])
    samples = await sample(ctx, {"source": output, "fps": fps, "in_sec": 0, "duration_sec": duration,
                                 "first": 0, "last": math.floor(duration * fps) - 1}, positions, 540)
    return await ask(ctx, "check_ad_delivery", FinalReport,
                     "审查实际合成后的广告：第一张是用户商品图，其余按播放顺序的成片帧，最后一张是片尾。"
                     "确认包含完整剧情而非只剩片尾，商品正确可见，无明显人物/道具/文字穿帮。"
                     "片尾须能读出提供的宣传题字和店名，艺术字允许书法造型，不能乱码、被截断、与食物重叠导致不清晰。"
                     "不要把自然虚焦背景、人物瞬间表情当作缺陷。依据实际帧给四项布尔判断和具体reason。"
                     "调用check_ad_delivery或只返回同结构JSON。",
                     {"headline": plan.headline, "shop": shop, "product": plan.product, "duration": duration},
                     [materials[0], *(a for _, a in samples)])
