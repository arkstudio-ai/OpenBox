"""Restage one exhausted commercial shot using the camera's actual rejected frames."""

from pydantic import BaseModel, Field

from light import commercial_plan, reference_quality
from light.product_story_continuity import ReviewRejected


class Blocking(BaseModel):
    actionable: bool = Field(description="A different staging of the same shot can visibly solve the failure.")
    observed: str = Field(min_length=1, max_length=380)
    direction: str = Field(max_length=650, description="One executable instruction for this camera and cast.")


def _failure_images(decisions, number):
    selected = []
    for key, item in decisions.items():
        parts = key.split(":")
        if len(parts) < 3 or parts[0] != commercial_plan.REVIEW:
            continue
        stage = parts[1]
        if stage in {"office-opening", "office-pair"} and number == 25:
            stage = "endpoints"
        elif stage == "motion-area" and number == 26:
            stage = "endpoints"
        elif parts[2] != str(number) or stage not in {"take", "endpoints", "single-opening"}:
            continue
        report = item.get("report") or {}
        if item.get("ok", report.get("ok")) is not False or not item.get("evidence"):
            continue
        assets = item["evidence"]
        frames = assets[-1:] if stage == "take" else assets[:2] if stage == "endpoints" else assets[:1]
        selected.append((stage, frames, item.get("reason") or report.get("reason", "")))
    # A moving take and its intended endpoints can show different causes of failure.
    ordered = sorted(selected, key=lambda entry: entry[0] != "take")
    staging = next((i for i, entry in enumerate(ordered) if entry[0] == "endpoints"), 0)
    if staging > 1:
        ordered.insert(1, ordered.pop(staging))
    return ordered[:3]


async def restage(ctx, prefix, run, plan, shot, library, cuts, failure, *, user_revision=0):
    from light.commercial_film import MATCH_IN, REVIEW_LANGUAGE, _record, _scene

    number = shot["id"]
    if getattr(plan, "score_revision", 1) != 2 or not isinstance(failure, ReviewRejected):
        return None
    key = f"{commercial_plan.REVIEW}:restage:{number}:r{user_revision}"
    decisions = run.get("director_decisions") or {}
    saved = decisions.get(key)
    if saved is None:
        problems = _failure_images(decisions, number)
        if not problems:
            return None
        anchors = []
        if previous := MATCH_IN.get(number):
            if tail := cuts.get(previous, {}).get("review_tail"):
                anchors.append(("前镜实际剪出帧，承接同一触点相位。", tail))
        elif number in MATCH_IN.values() and library.get("contact_actor"):
            anchors.append(("已核定的演员、真实商品、触点与身体相对位置。", library["contact_actor"]))
        if number == 28 and (wide := cuts.get(22, {}).get("review_head")):
            anchors.append(("街舞第一次亮相的真实机位和左右站位；本镜由上司接替。", wide))
        if number in {26, 27} and (last := cuts.get(number - 1, {}).get("review_tail")):
            anchors.append(("办公室前镜实拍末态；只延续真实所有权和支撑。", last))

        references = list(anchors)
        references.extend(("原商品形态与材质。", asset) for asset in (library.get("product_cards") or [])[:1])
        if scene := library.get(_scene(plan, shot)):
            references.append(("本场固定布景。", scene))
        for stage, frames, _ in problems:
            role = "曾被退回的真实实拍候选，按行从左到右。" if stage == "take" else "曾被退回的动作准备图或终点图。"
            references.extend((role, frame) for frame in frames)
        photos = {}
        for role, asset in references:
            photos.setdefault(asset, role)
        answer = await reference_quality.ask(
            ctx, "restage_commercial_shot", Blocking, REVIEW_LANGUAGE[plan.language] +
            "你是现场重排动作的导演。请看实际退件与参考图，指出使这个镜头失败的可见原因，"
            "给同一机位、演员、商品和镜长一个不同且简明的走位方案。说明开机静态、唯一动作、停机瞬间。"
            "尽可能用同一平面的构图及真实承重，把可见部位留在画内。可改变起步距离、接近速度、手的前后遮挡，"
            "不能改商品用法、接点、转场、景别、台词、镜头顺序、角色或舞伴。"
            "原镜头本来就有动作省略时只拍这一个阶段。切出时必须出现的动作要真的演出来。"
            "只有给出match_contact的镜头才检验跨场指尖动作。舞蹈镜按该镜舞步与商品支撑来拍。"
            "如果证据不足、商品无法辨认或同一镜头客观上无法实现，actionable=false。"
            "不要要求放宽验收，不要照抄之前失败的纠正。",
            {"language": plan.language, "shot": shot, "product": plan.product, "appearance": plan.appearance, "mode": plan.mode,
             "carry": plan.carry, **({"match_contact": plan.contact} if number in MATCH_IN or number in MATCH_IN.values() else {}),
             **({"transfer": plan.transfer_mode} if number in (25, 26, 27) else {}),
             "last_failure": failure.review_feedback["reason"], "observations": [p[2] for p in problems],
             "roles": list(photos.values())}, list(photos), max_tokens=1150, timeout=65,
        )
        saved = await _record(ctx, prefix, run, number, key, {**answer, "anchors": anchors})
    verdict = Blocking.model_validate(saved["report"])
    if not verdict.actionable or not verdict.direction.strip():
        return None
    return verdict.direction, [tuple(anchor) for anchor in saved.get("anchors", [])]
