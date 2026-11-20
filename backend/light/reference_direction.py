"""Bind a director's named roles to the people actually visible in a first frame."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from light.remake import RemakeStopped, _check
from tool.footage_analyze import parse_picture
from video import vision_contract


class UnusableFrame(RemakeStopped):
    def __init__(self, message, frame):
        super().__init__(message)
        self.frame = frame


class Coverage(BaseModel):
    frame: str = Field(min_length=1, max_length=700)
    action: str = Field(min_length=1, max_length=700)


class Crop(BaseModel):
    left: float = Field(ge=0, lt=1)
    top: float = Field(ge=0, lt=1)
    size: float = Field(ge=0.35, le=1)

    @model_validator(mode="after")
    def inside_frame(self):
        if max(self.left, self.top) + self.size > 1.001:
            raise ValueError("The crop must fit within the original frame")
        return self


class Direction(BaseModel):
    frame_ok: bool
    repair_mode: Literal["edit", "reframe", "crop"] = "edit"
    crop: Crop | None = None
    coverage: Coverage | None = None
    reason: str = Field(min_length=1, max_length=400)
    correction: str = Field(default="", max_length=700)
    speaker: str = Field(max_length=200)
    action: str = Field(min_length=1, max_length=700)
    seconds: int = Field(ge=3, le=8)

    @model_validator(mode="after")
    def repair_instruction(self):
        if not self.frame_ok and self.repair_mode == "crop" and self.crop is None:
            raise ValueError("Specify the visible crop to use")
        if not self.frame_ok and not self.correction:
            raise ValueError("An unsuitable first frame needs a concrete correction")
        return self


def apply_coverage(plan, index, coverage):
    shots = list(plan.shots)
    shots[index - 1] = shots[index - 1].model_copy(update=Coverage.model_validate(coverage).model_dump())
    return plan.model_copy(update={"shots": shots})


class Edit(BaseModel):
    action: Literal["recut", "retime", "hold", "reframe", "reshoot", "omit"]
    reason: str = Field(min_length=1, max_length=500)
    instruction: str = Field(min_length=1, max_length=700)
    coverage: Coverage | None = None
    take: str = ""
    use_matching_setup: bool = False
    output_seconds: float | None = Field(default=None, ge=1, le=8)
    first: int | None = Field(default=None, ge=0)
    last: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def executable(self):
        if self.action in {"retime", "hold"} and self.output_seconds is None:
            raise ValueError("Specify the length of the edited clip")
        if self.action == "reframe" and self.coverage is None:
            raise ValueError("A new camera setup needs a frame and an action")
        if (self.first is None) != (self.last is None) or self.first is not None and self.last < self.first:
            raise ValueError("Return a complete, ordered frame interval")
        return self


async def choose_edit(ctx, plan, index, copy, anchor, materials, current, takes, history, setup,
                      neighbors, feedback, *, can_omit, can_film, can_recut):
    from light.reference_quality import ask

    shot = plan.shots[index - 1]
    options = (["recut"] if can_recut else []) + (["reframe", "reshoot"] if can_film else []) + (["omit"] if can_omit else [])
    if not shot.line and any(t.get("last") is not None for t in takes.values()):
        options.append("retime")
        if shot.kind == "product":
            options.append("hold")
    if not options:
        raise RemakeStopped(f"第{index}镜已用尽可执行修正，现有素材和审片记录已保存。")
    images = list(dict.fromkeys([anchor, *materials, current["first_reference"],
                                *([setup["first_reference"]] if setup else []),
                                *([feedback["rejected_frame"]] if feedback.get("rejected_frame") else []),
                                *(t["review_asset"] for t in takes.values() if t.get("review_asset")),
                                *(c["review_asset"] for c in neighbors if c.get("review_asset"))]))
    guide = (
        "你是拥有剪辑和拍摄工具的广告导演。每条take中的source_media_seconds是完整原片时长，in_sec/duration_sec是已选片段的起点和时长。第一次发现问题就决定最适合这条广告的下一步，可以立即换机位。"
        "先看实际帧、失败原因、其他已拍素材和相邻镜头，再从available_actions任选。没有预设处理顺序。"
        "recut：选本镜已有take的source试剪，instruction写可用动作和应剪掉的缺陷。知道精确帧号可以填first和last，"
        "否则留空让剪辑师逐帧选点。根据实际对白时间和动作选出自然的段落。"
        "retime：对已经审过的无对白片段轻微变速，给output_seconds延长或缩短停留，不需重拍。hold：在商品镜头实际保留区间内选择一帧做片尾定帧，给first和last同一个帧号、output_seconds给品牌展示秒数，不需重拍。"
        "允许根据用户创意和整片实效修订之前自己写下的摆盘、锅具露边、机位和运镜指标。店家宣传的是实际商品，质量目标以播放出来是否好看、易懂为准。"
        "reframe：另选机位、可见人物或动作。保留本镜叙事目的和已确认对白，装饰性表演可以替换。"
        "coverage.frame只正向描写开拍前屏幕里真正可见的内容，coverage.action只描写这一机位内可见的变化。画外发生的事、剪点、下一镜以及旧片的问题另写在instruction，不要写进coverage。"
        "reshoot：用已有首帧重新表演，instruction给演员或摄影机明确指导。"
        "当问题是片长或品牌可读时间时，请具体比较现有实拍的轻微变速、实拍定帧、延长入出点和付费补拍各能增加几秒。需要借同场合格机位时选use_matching_setup。omit：剪掉可选镜，说明为何不伤叙事。"
        "比较不同选择对成片质量、时长和等待的影响。若上次方法失败，结合history调整。角色身份、真实商品、实际说话人要可信。"
        "角色和道具只按本片实际设定。用户文案提供创意，构图与措辞允许改编。"
        "返回本次一个可执行决定，reason说明画面或声音证据。"
    )
    data = {"available_actions": options, "reference_copy": copy, "shot": shot.model_dump(),
            "previous": plan.shots[index-2].model_dump() if index > 1 else None,
            "next": plan.shots[index].model_dump() if index < len(plan.shots) else None,
            "current_take": current["source"], "takes": list(takes.values()), "prior_decisions": history,
            "feedback": feedback, "matching_setup": setup and setup["shot"],
            "adjacent_cuts": neighbors, "image_order": images}
    for _ in range(2):
        entry = await ask(ctx, "direct_ad_edit", Edit, guide, data, images)
        choice = Edit.model_validate(entry["report"])
        target = takes.get(choice.take or current["source"], {})
        timed = choice.action in {"retime", "hold"}
        if (choice.action in options and target and (not choice.use_matching_setup or setup)
                and (not timed or target.get("last") is not None)):
            return entry
        data["validation_feedback"] = "只从本轮提供的available_actions、takes、matching_setup中选。"
    raise RemakeStopped(f"第{index}镜导演未给出可执行的剪辑决定，已保存素材。")


async def resolve(ctx, plan, index, first, anchor, regenerate, feedback="", max_repairs=3, *, reference_copy=""):
    """Recheck each actual repair; never submit video from an unapproved first frame."""
    coverage = None
    for attempt in range(max_repairs + 1):
        direction = await bind(ctx, plan, index, first, anchor, feedback, reference_copy=reference_copy)
        if direction.coverage:
            coverage = direction.coverage
            plan = apply_coverage(plan, index, coverage.model_dump())
        if direction.frame_ok:
            return first, direction.model_copy(update={"coverage": coverage})
        if attempt == max_repairs:
            raise UnusableFrame(f"第{index}镜首帧自动修正{max_repairs}次后仍未通过：{direction.reason}。已有素材已保留。", first)
        if direction.repair_mode == "crop":
            first = await crop_frame(ctx, first, direction.crop)
        else:
            first = await regenerate(attempt + 1, first, direction.correction, direction.repair_mode)
    raise AssertionError("unreachable")


async def crop_frame(ctx, asset, box):
    import io
    import hashlib
    from PIL import Image
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from light.remake import _reference_asset

    async with get_db_session() as db:
        row = await db.get(FileAsset, asset)
        if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready" or not row.mime.startswith("image/"):
            raise RemakeStopped("首帧裁切的素材不可用。")
        raw = await get_oss().get_object(row.oss_key)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = image.size
    bounds = (round(w*box.left), round(h*box.top), min(w, round(w*(box.left+box.size))), min(h, round(h*(box.top+box.size))))
    buf = io.BytesIO()
    image.crop(bounds).resize(image.size, Image.Resampling.LANCZOS).save(buf, "JPEG", quality=94)
    data = buf.getvalue()
    return await _reference_asset(ctx, "director-crop-" + hashlib.sha256(data).hexdigest()[:20], data)


async def bind(ctx, plan, index, first, anchor, feedback="", *, reference_copy=""):
    import litellm

    from agent.llm import _get_provider_kwargs
    from agent.trajectory import capture_service_dispatch, service_scope
    from core.config import get_config
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    shot = plan.shots[index - 1]
    guide = (
        "你是现场导演，先核对本镜首帧，再把剧本中的人名绑定到实际画面。第一张是本镜首帧，第二张仅用于辨认演员。"
        "根据角色设定核对脸、发型、衣服、视线、主动作执行人和道具状态；相同衣服不能代替人物身份核对，不能无故从披发变成扎发。"
        "不要因为首帧画错了人，就改剧本角色来迁就它。"
        "frame_ok 判断这一帧能否拍成可用的广告镜头。以实际商品、参考文案表达的创意和关键剧情为依据，"
        "镜头描述中的构图、陪体边缘、静止一瞬的表情是导演可调整的拍法。区分出售的商品与陈列配件，"
        "核对商品主体是否可辨、有足够空间叠印可读题字。只有具体妨碍上述目的、演员身份、交流对象、"
        "动作顺序或后续接镜的问题才退回，说明对成片的实际影响。"
        "说话人的视线应能明确落向交流对象；听者可以自然看向说话人或正在展示的商品。"
        "如果当前首帧能完成目标，让 action 指导演员在实拍中完成眼神、嘴形和姿态变化。"
        "核对本剧本指定动作的执行人及其在相邻镜头中的身份。有对白时指定说话人应是本镜面部清楚可见的主体；"
        "如果他在远处、虚焦、被前景人物抢走主体位置，frame_ok=false，改拍说话人的中近景。核对道具在本镜起始时的实际状态，"
        "同一件道具不能凭空重复。最后kind=product的产品镜头不能有人；kind=detail允许剧本明确的持筷手、袖口或制作动作，"
        "不应为追求无人而造出悬浮的餐具。已有明显矛盾，自选edit局部修图、reframe换构图、crop裁切当前真实首帧，可第一次就换拍法。crop给归一化的left、top、size（宽高同比例），观察人物和商品是否仍完整。若已有首帧可以通过改变拍法实现本镜叙事目的，可直接frame_ok=true并用coverage写实际首帧与替代动作；拍法变化时始终给coverage，以它作为之后的审片依据。景别、装饰物、非必要的具体表演和运镜由你决定。需要生图时correction只描述新首帧真正入画的东西，不描述未来动作、旧缺陷或画外物。"
        "action 把本镜动作中的每个名字换成可见的衣着、年龄、位置描述，明确谁看谁、谁闭口。保留本镜一个主动作，"
        "不加入新剧情、对白、镜头切换或下一场景。speaker 只填指定说话人在本镜中的外貌衣着与位置，不只填人名或职位。"
        "previous_take_problem记录上次观察和上一位导演的建议。真实商品身份、清楚的宣传字、人物和实物穿帮需要解决；具体运镜和道具的装饰性摆放可以改编。不要把建议中自行加的秒数、边缘留白百分比或盛器是否完整变成验收指标。商品特写可改为原商品完整形态和更简单动作，不能为遵守旧细节重复错误。"
        "action 是给摄影模型的正向画面描述，只描述实际会在取景框内出现和移动的事物，"
        "用视线方向与真实布景交代空间，避免点名画外人物或复述以往的穿帮。"
        "无对白时 speaker 为空。不要让前景人物代替背景的指定角色说话，也不要让空手的人凭空拿出杯子。"
        "seconds 是拍摄预算，必须给指定台词自然语速与说完反应留空间，可以长于原计划，3至8秒；之后会按实际画面剪短。"
        "不把一组慢动作和长台词挤进3秒。对白原文另行传给视频模型，这里action不写台词、字幕或题字。"
        "调用 bind_frame 提交判断；如果不能调用函数，只返回同一结构的 JSON，不要解释。\n"
    )
    data = {
        "user_reference_copy": reference_copy,
        "cast_and_setting": plan.setting_and_cast,
        "shot": shot.model_dump(),
        "previous_take_problem": feedback,
    }
    content = [{"type": "text", "text": guide + json.dumps(data, ensure_ascii=False)}]
    urls = {}
    async with get_db_session() as db:
        for asset_id in dict.fromkeys([first, anchor]):
            row = await db.get(FileAsset, asset_id)
            if (
                not row
                or row.user_id != ctx.user_id
                or row.is_deleted
                or row.status != "ready"
            ):
                raise RemakeStopped("首帧与演员参考尚不可读取。")
            url = get_oss().presign_get(row.oss_key)
            urls[url] = row.id
            content.append({"type": "image_url", "image_url": {"url": url}})
    function = {
        "name": "bind_frame",
        "description": "Ground the actors and action in the actual first frame before filming.",
        "parameters": Direction.model_json_schema(),
    }
    model = get_config().video_analysis.model
    messages = [{"role": "user", "content": content}]
    error = ""
    for _ in range(2):
        await _check(ctx)
        try:
            options = dict(
                model=model,
                messages=messages,
                temperature=0.1,
                max_tokens=4000,
                timeout=75,
                tools=[{"type": "function", "function": function}],
                **vision_contract.request_options(model, "bind_frame", _get_provider_kwargs(model)),
            )
            async with (
                service_scope(ctx, asset_urls=urls),
                capture_service_dispatch(
                    purpose="video_direction",
                    provider="agent_vision",
                    model=model,
                    operation="bind_reference_cast",
                    profile="reference_direction",
                    body={
                        "messages": messages,
                        "tools": options["tools"],
                        "tool_choice": options["tool_choice"],
                        "extra_body": options.get("extra_body"),
                    },
                ) as trace,
            ):
                answer = await asyncio.wait_for(litellm.acompletion(**options), 80)
                content = vision_contract.output(answer, "bind_frame")
                await trace.chunk(vision_contract.response_evidence(answer, content))
                result = Direction.model_validate(parse_picture(content))
                if bool(result.speaker) != bool(shot.line):
                    raise ValueError(
                        "The speaker must match the shot's dialogue requirement"
                    )
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from question.runtime import RunRevoked

            if isinstance(exc, RunRevoked):
                raise
            error = type(exc).__name__
    raise RemakeStopped(f"首帧角色核对暂未返回有效结果（{error}），已有素材已保留。")
