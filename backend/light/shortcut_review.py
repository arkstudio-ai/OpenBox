"""Review key setups and choose cuts from actual filmed actions."""

import hashlib
import io
import math

import httpx
from PIL import Image
from pydantic import BaseModel, Field, model_validator

from light import reference_quality, wan_edit
from light.remake import RemakeStopped, _reference_asset
from light.shortcut_plan import OPENING, REVEAL, SETUP_ROLES


class FrameCheck(BaseModel):
    ok: bool
    reason: str = Field(min_length=1, max_length=400)
    correction: str = Field(default="", max_length=650)

    @model_validator(mode="after")
    def explain_repair(self):
        if not self.ok and not self.correction.strip():
            raise ValueError("A rejected setup needs a replacement composition")
        return self


class SetupCheck(FrameCheck):
    visible_setup: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def bind_actual_frame(self):
        if self.ok and not self.visible_setup.strip():
            raise ValueError("An approved setup needs a description of the actual composition")
        return self


class KeyFrames(BaseModel):
    opening: SetupCheck
    handoff: SetupCheck
    reveal: SetupCheck


class TakeCheck(FrameCheck):
    first: int | None = Field(default=None, ge=0)
    last: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.ok and (self.first is None or self.last is None or self.last < self.first):
            raise ValueError("An approved take needs the first and last retained frames")
        return self


async def key_frames(ctx, plan, frames, anchor, materials):
    images = [
        (anchor, "三人身份定妆"),
        *[(asset, "用户真实商品") for asset in materials[:1]],
        *[(asset, f"第{index}镜{role}帧")
          for index in SETUP_ROLES
          for role, asset in zip(("首", "尾"), frames[index], strict=True)],
    ]
    guide = (
        "你是广告现场导演，按image_order看实际图片，逐镜证实可以拍再批准。"
        "第1镜是一段交谈。首尾帧中说话人和指定画内listener的眼神、脸和身体朝向要能接上。"
        "第三人自然关注对话或自己的道具，不能像合影一样直望观众，也不能在尾帧跟别人齐看一个空白画外点。"
        "说完后只留在交流中自然应声或看回道具。出发是下一镜的事。"
        "第4镜首尾只有辛苦的两位演员，商品、从容者和揭晓的桌面均在画外。"
        "第4镜尾帧两人自然朝画外同一侧看，视线能引到下一镜；检查是实际所见，不能因分镜写了而放行。"
        "第5镜必须明显换机位：独拍从容者和实际商品，明确能认出角色、成品和专属道具，"
        "另外两人不能仍站在旁边。首尾帧同一机位，演员、道具、产品状态可以靠一个动作自然接起。"
        "不必完全还原文字景别、装饰或微表情；反打、视线、角色人数及商品亮相才是必须保住的条件。"
        "分别返回opening、handoff和reveal的ok与实际证据。失败时correction只正向描述该镜的新构图、机位及道具位置。"
        "通过时visible_setup清楚正向描述实际首帧机位、演员左右位置、主体与实际道具，直接交给摄影模型。"
        "不要改变人设、商品和台词。若视线衔接不可信，指出需要重新构图的一镜。"
    )
    return await reference_quality.ask(
        ctx, "check_shortcut_key_frames", KeyFrames, guide,
        {"cast": plan.setting_and_cast, "product": plan.product,
         **{role: plan.shots[index - 1].model_dump() for index, role in SETUP_ROLES.items()},
         "image_order": [label for _, label in images]},
        [asset for asset, _ in images], max_tokens=3000, timeout=55, reasoning_effort="high",
    )


def apply_range(index, cut, decision, frames, duration, target):
    """Convert an observed frame interval to source seconds without losing dialogue."""
    first, last = decision.first, decision.last
    if not decision.ok or first not in frames or last not in frames:
        raise ValueError("剪点必须是图板上实际存在的帧号。")
    fps = cut["fps"]
    start, end = first / fps, (last + 1) / fps
    length = end - start
    minimum = 1.8 if index == OPENING else target - 1.1
    if (end > duration + 0.002 or not minimum <= length <= target + 0.85
            or index == REVEAL and start > 0.36
            or any(c["from_sec"] < start - 0.001 or c["to_sec"] > end + 0.001 for c in cut["cues"])):
        raise ValueError("剪点需要保留完整对白、在目标时长附近，且揭晓从画面动作起点入镜。")
    if index == OPENING and cut["cues"] and end - cut["cues"][-1]["to_sec"] > 0.85:
        raise ValueError("开场说完后最多保留0.85秒自然反应，切掉多余等待或转头。")
    return {**cut, "first": first, "last": last, "in_sec": round(start, 4),
            "duration_sec": round(length, 4), "reason": decision.reason}


async def filmed_cut(ctx, plan, index, cut, duration, first_reference, incoming):
    last = math.floor(duration * cut["fps"] + 0.01) - 1
    raw = {**cut, "in_sec": 0, "duration_sec": duration, "first": 0, "last": last}
    positions = {0, .045, .12, .23, .34, .46, .58, .69, .78, .86, .93, .98, 1}
    if index == OPENING and cut["cues"]:
        speech_end = cut["cues"][-1]["to_sec"]
        positions.update(min(1, (speech_end + offset) / duration) for offset in (.08, .23, .44, .68))
    async with httpx.AsyncClient(timeout=20) as client:
        samples = await reference_quality._sample_frames(ctx, client, raw, sorted(positions), 280)
    views = {n: Image.open(io.BytesIO(data)).convert("RGB") for n, data in samples}
    board = wan_edit.sheet(views)
    digest = hashlib.sha256(board).hexdigest()[:20]
    evidence = await _reference_asset(ctx, f"shortcut-motion-{digest}", board)
    labels = ["源视频实际抽帧图板", "本镜核定首帧"]
    images = [evidence, first_reference]
    if incoming:
        images.append(incoming)
        labels.append("下一镜首帧，用于判断动作或视线的去处")
    shot = plan.shots[index - 1]
    guide = (
        "你是广告剪辑师，图板数字是本镜原素材帧号。选择可观察到主动作及结果的完整区间，"
        "并给出证据。first和last都从available_frames中选，时长尽量接近target_seconds允许短1.1秒或长0.85秒。"
        "去掉不必要的等待与尾部回弹，但不切断对白，不在动作到一半时切走。"
        "第1镜例外，时长可短至1.8秒。先确认说话人的眼神指向画内listener，能看出在向谁说话，"
        "同伴自然接话或回应；从容者保留自己的道具和自然反应。"
        "避开最早的看镜头或首帧停顿，台词说完保留不超过0.85秒自然反应，话落后尽早接下一镜。"
        "剪掉台词后的集体转头、望向空白画外或明显硬凹姿势；若整个对白都朝错的方向且无法剪掉才退回补拍。"
        "第2、3镜以手机上能看清的一件困难或反应为准；接受等价的简单动作，不要求看清一粒调味料或某个微小零件。"
        "第4镜在两人已把视线带向画外的瞬间切出，保留必要的自然反应，不要在回头后多等。"
        "第5镜从源片0到0.35秒附近入镜。所选区间要看见独拍的从容者、真实商品、归属正确的回扣动作，"
        "如手腕小幅动作已能认出回扣就接受。关键动作缺失、做错人、穿帮无法剪掉才设置ok=false，"
        "并在correction给出用同机位可以拍出的一个更简单的动作。不能用剧本替代实际画面证据。"
    )
    data = {"shot": index, "storyboard": shot.model_dump(), "product": plan.product,
            "image_order": labels, "fps": cut["fps"], "target_seconds": shot.seconds,
            "available_frames": sorted(views), "captions_source_seconds": cut["cues"]}
    for _ in range(2):
        answer = await reference_quality.ask(
            ctx, "choose_shortcut_action_cut", TakeCheck, guide, data, images,
            max_tokens=3200, timeout=60, reasoning_effort="high",
        )
        review = TakeCheck.model_validate(answer["report"])
        if not review.ok:
            return {**answer, "cut": None}
        try:
            revised = apply_range(index, cut, review, views, duration, shot.seconds)
        except ValueError as exc:
            data["validation_feedback"] = str(exc)
        else:
            return {**answer, "cut": {**revised, "review_asset": evidence,
                                      "editor_model": answer["model"]}}
    raise RemakeStopped(f"第{index}镜已保留，视觉审片暂未选出有效剪点。")
