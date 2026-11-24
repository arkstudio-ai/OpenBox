"""Film the reference commercial as 28 independently editable, fully moving shots."""

import asyncio
from dataclasses import replace
from io import BytesIO
import hashlib
import json
import math
import re
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
from pydantic import BaseModel, Field, ValidationInfo, create_model, model_validator

from light import commercial_frame_audit
from light import commercial_plan as recipe
from light import product_story, reference_quality
from light.product_story_continuity import EndpointCheck, ReviewRejected, internal_jump
from light.product_story_post import ff
from light.reference_film import speech_window
from light.remake import RemakeStopped, _digest, _existing_job, _job, _parallel, _reference_asset
from light.remake_lettering import _has_cjk
from light.shortcut_review import FrameCheck
from light.wan_remake import _still
from tool import video_production as vp
from tool.video_runninghub import MODEL


GUIDES = Path(__file__).parent / "remake_assets" / "commercial-reference" / "visual"
MATCH_IN = {12: 11, 18: 17, 24: 23}
# Dependencies use real photographed and reviewed frames, never a proposed storyboard frame.
DEPENDENCIES = {3: (2,), 9: (8,), 12: (11,), 18: (17,), 24: (23,), 26: (25,), 27: (26,), 28: (22, 27)}
OUT_POSE = {11, 17, 23, 26}
DANCE = {10, 13, 18, 19, 20, 21, 22, 28}
SCENE_JOINS = {"home": tuple(range(1, 11)), "track": tuple(range(12, 17)),
               "street": tuple(range(18, 23)), "office": tuple(range(24, 27))}
PROFILES = {"home": "daylight interior", "track": "outdoor training route",
            "street": "exterior street corner", "office": "workplace interior"}
WIDE = "1536x864"
REVIEW_SIZE = (384, 216)
MAX_TAKES = 3
MAX_JOIN_REPAIRS = 2
STYLE = ("横幅16:9、单幅真实摄影广告定帧，真实有质感的场景和人物，色彩浓郁，电影自然光和明暗关系。"
         "一张完整画面铺满，保留商品原有识别标记。无新增字卡、字幕、边框、水印、示意图、分栏或标注。")
REVIEW_LANGUAGE = {
    "vi": "Viết tất cả lý do, nhận xét và yêu cầu sửa bằng tiếng Việt. ",
    "en": "Write the reason and all requested corrections in English. ",
    "zh": "用中文写审片意见和补拍要求。",
}
PLAYBACK_REVIEW = "normal-speed-v1"
PLAYBACK_STANDARD = (
    "按普通观众正常速度看这条广告的标准裁定。实拍中同一人用同一手，接触同一件商品的同一可辨区域，"
    "动作方向连贯、剪接无明显跳位，就予通过。中央、前缘等描述指可辨的区域；"
    "不能因指腹没有压在几何中线、极小的落点偏差、发丝、肤色或光照、自然短暂遮挡而要求补拍。"
    "区分点在同一面内的小偏差和明显落在另一端或另一部件。手物交叠遮住几帧时结合前后实际位置判断。"
    "只在画面可指出的实质问题影响正常观看时退回，例如动作没发生或方向突变、按错部件、明显换手/换人、"
    "错货多件、失去支撑、断形、场次不对或交接前后谁持有商品错误。合格画面不提美化或改善构图的建议。"
)


class Selection(FrameCheck):
    row: int = Field(ge=1, le=4, description="1-based row of the selected chronological strip")


class PlaybackDecision(BaseModel):
    visible_problem: Literal["none", "missing_action_or_broken_cut", "wrong_identity_or_quantity",
                             "product_in_wrong_position", "impossible_support_or_shape", "wrong_scene_or_role",
                             "unusable_dialogue"] = Field(
        default="none", description="A concrete advertising continuity defect apparent at normal playback speed. "
        "Small placement, cosmetic, exposure, framing or fleeting occlusion differences within a readable action are none.")

    @model_validator(mode="after")
    def require_material_problem(self):
        if (not self.ok and self.visible_problem == "none"
                and getattr(self, "product_state", "") == "visibly_displaced_or_duplicated"):
            self.visible_problem = "product_in_wrong_position"
        if self.ok != (self.visible_problem == "none"):
            raise ValueError("Pass a readable shot with no material playback defect. A rejection needs a visible core problem.")
        return self


class FirstMatchEntry(BaseModel):
    entry_path: str = Field(min_length=1, max_length=380,
        description="Locate the active finger in the first image, describe how it approaches, and locate its contact at the cut.")
    has_clear_approach: bool = Field(description="The active hand enters from offscreen or a visibly distant resting position. If it supports the item, the free finger starts retracted and visibly travels. It is not already poised beside the target.")
    reached_feature: bool = Field(description="The intended fingertip has visibly reached the actual named feature at the cut.")

    @model_validator(mode="after")
    def verify_entry(self):
        if self.ok and not (self.has_clear_approach and self.reached_feature):
            raise ValueError("The first match needs a distinct hand entrance and actual arrival on the feature.")
        return self


class FirstMatchSelection(FirstMatchEntry, Selection):
    pass


class FirstWearSelection(Selection):
    wear_site: str = Field(min_length=1, max_length=250,
                           description="What is actually at the actor's wear site before pickup or first use? If it is outside frame, say so.")
    competing_item: bool = Field(description="A second item of the same category can be seen already worn or layered with the actual product.")

    @model_validator(mode="after")
    def no_second_item(self):
        if self.ok and self.competing_item:
            raise ValueError("A visible competing wearable before first use must be rejected")
        return self


class BeforePickup(BaseModel):
    product_before_cut: Literal["out_of_frame", "at_rest", "already_lifted_or_worn"] = Field(
        description="The one product's observed state in the selected opening, before shot 2 first picks it up.")

    @model_validator(mode="after")
    def leave_first_pickup_to_the_next_shot(self):
        if self.ok and self.product_before_cut == "already_lifted_or_worn":
            raise ValueError("The opening cannot already show the product in hand or being worn before the first pickup.")
        return self


class DepartureSelection(Selection):
    product_state: Literal["out_of_frame", "in_planned_position", "closing_the_product_itself",
                           "visibly_displaced_or_duplicated"] = Field(
        description="Inspect the product, the actor's visible wear site and the gear being closed in the selected interval.")

    @model_validator(mode="after")
    def keep_departure_item_in_place(self):
        if self.ok and self.product_state == "visibly_displaced_or_duplicated":
            raise ValueError("Do not pack a worn, carried or installed product that should still be in its established position.")
        return self


class ConfrontationSelection(Selection):
    control_at_cut: str = Field(min_length=1, max_length=300,
        description="Who physically controls or wears the one product, or occupies the installed position, at the actual end of shot 25?")
    advanced_to_transfer: bool = Field(description="The handoff or change of wearer/operator has already occurred during this approach.")

    @model_validator(mode="after")
    def end_before_handoff(self):
        if self.ok and self.advanced_to_transfer:
            raise ValueError("The confrontation must end with the protagonist still in control before shot 26")
        return self


class Join(FrameCheck):
    shot: int = Field(ge=1, le=28, description="Shot to correct if the edit fails")


class SignatureJoin(Join):
    shot: Literal[11, 12, 14, 17, 18, 22, 23, 24, 27, 28]


class WardrobeJoin(Join):
    shot: Literal[11, 12, 17, 18, 23, 24]
    wardrobe_sequence: str = Field(min_length=1, max_length=450,
        description="Actual clothing and visible non-product luggage across all three pairs, mentioning any obscured area.")
    wardrobe_continuous: bool

    @model_validator(mode="after")
    def require_intentional_changes(self):
        if self.ok and not self.wardrobe_continuous:
            raise ValueError("The recorded outfit changes must follow the three scene transitions")
        return self


class SceneJoin(FrameCheck):
    after: int | None = Field(default=None, ge=1, le=27, description="Outgoing shot of the broken cut")
    shot: int | None = Field(default=None, ge=1, le=28, description="One of the two shots to correct")
    outgoing: str = Field(default="", max_length=250)
    incoming: str = Field(default="", max_length=250)

    @model_validator(mode="after")
    def identify_visible_conflict(self):
        if not self.ok and (self.after is None or self.shot not in (self.after, self.after + 1)
                            or not self.outgoing.strip() or not self.incoming.strip()):
            raise ValueError("Identify the incompatible visible states on both sides and the one shot to repair")
        return self


class ContactStaging(EndpointCheck):
    reference_item: str = Field(min_length=1, max_length=200, description="What the user photo itself depicts, independent of the script")
    filmed_item: str = Field(min_length=1, max_length=200, description="What the opening and ending pictures actually show")
    same_item: bool = Field(description="True only if the visible subject matches the user photo, or no item is expected on screen")
    support: str = Field(min_length=1, max_length=280, description="Describe support surfaces and clearance for protruding parts")
    plausible_support: bool
    consistent_hands: bool

    @model_validator(mode="after")
    def ground_approval(self):
        if self.ok and not (self.same_item and self.plausible_support and self.consistent_hands):
            raise ValueError("All three physical checks must pass before the endpoints are approved")
        return self


class FirstMatchStaging(FirstMatchEntry, ContactStaging):
    pass


class InsertOpenings(BaseModel):
    frames: list[str] = Field(min_length=5, max_length=5)


def _edit_note(note):
    stripped = note.strip()
    if stripped.casefold().startswith(("không cần sửa", "không cần chỉnh", "no correction needed", "no change needed",
                                        "no changes needed", "no edit needed", "无需修改", "无需重画", "不需要修改")):
        return ""
    return stripped


class UnwornCast(BaseModel):
    ok: bool
    reason: str = Field(min_length=1, max_length=550)
    protagonist_correction: str = Field(default="", max_length=360)
    authority_correction: str = Field(default="", max_length=360)

    @model_validator(mode="after")
    def identify_competing_costume(self):
        self.protagonist_correction = _edit_note(self.protagonist_correction)
        self.authority_correction = _edit_note(self.authority_correction)
        if self.ok == bool(self.protagonist_correction.strip() or self.authority_correction.strip()):
            raise ValueError("Specify exactly which clean portrait needs a correction when a competing item is visible")
        return self


async def _verify_unworn_cast(ctx, prefix, run, plan, portraits, materials):
    roles = {"protagonist": 1, "authority": 25}
    originals = {role: portraits[role] for role in roles}
    repairs = _requested_repairs(run)
    for attempt in (1, 2):
        pictures = [portraits[role] for role in roles]
        changed = [role for role in roles if portraits[role] != originals[role]]
        key = f"{recipe.REVIEW}:unworn-cast:{_fingerprint(pictures, materials, originals)}"
        decision = (run.get("director_decisions") or {}).get(key)
        if not decision:
            answer = await reference_quality.ask(
                ctx, "commercial_unworn_cast", UnwornCast, REVIEW_LANGUAGE[plan.language] +
                "图1是主人公尚未取得商品的基础服装，图2是权威人物尚未接手时的基础服装。之后 product_views 张是用户原商品。"
                "只检查未使用状态：两人是否手持/穿戴该商品、类似替代件，或在相同佩戴部位另有本会与商品重叠的同类物。"
                "同类物即使颜色、款式不同也会导致第一次取用时出现第二件；指出需从哪一张移除的具体物品。"
                "正常服装、发型、职业标记、与实际穿戴兼容的底层、空携具都可以保留。不要以第一张和第二张互不相像为错误。"
                "若还有 original_portraits 参考，分别与自己未修图时比较，身份和其余服装仍应相同。"
                "仅在有可见问题的角色字段填写修正，否则该角色字段留空。",
                {"product": plan.product, "appearance": plan.appearance, "language": plan.language,
                 "product_views": len(materials), "original_portraits": changed},
                [*pictures, *materials, *(originals[role] for role in changed)], max_tokens=1000, timeout=60)
            decision = await _record(ctx, prefix, run, 1, key, answer)
        check = UnwornCast.model_validate(decision["report"])
        if check.ok:
            return
        corrections = {role: note for role in roles if (note := getattr(check, f"{role}_correction"))}
        if attempt == 2:
            role = next(iter(corrections))
            raise _rejected(plan, recipe.shots(plan)[roles[role] - 1], check.reason, corrections[role], staging=True)

        async def remove(role):
            revision, note = repairs.get(roles[role], (0, ""))
            prompt = (STYLE + "保留这张单人定妆的原演员、脸、发型、原服装、光线、姿态及构图。"
                      "拍的是此人第一次使用或接收商品之前。只移除独立审片指出与待使用商品冲突的物件，"
                      f"还原其下面应有的自然外观，其他细节不改。修正：{corrections[role]}。{note}")
            image = originals[role]
            name = f"commercial-bare-{role}-{_fingerprint(image, prompt, revision)}"
            portraits[role] = await _still(ctx, prefix, name, prompt, [image], size=WIDE, quality="medium",
                                          language=plan.language)
        await asyncio.gather(*(remove(role) for role in corrections))


class CarriedLook(FrameCheck):
    actor_matches: bool
    product_matches: bool
    support_matches: bool
    position_matches: bool
    contact_point_reachable: bool = Field(description="The intended hand can reach the visible feature without moving the supported product. Apply a body-level constraint to this feature only when that feature's height is explicitly specified.")
    feature_xy: list[int] | None = Field(default=None, min_length=2, max_length=2, description="Observed center of the intended touch on the visible product, x,y in 0..1000")
    landmark_xy: list[int] | None = Field(default=None, min_length=2, max_length=2, description="Position at the named feature's explicitly prescribed body level in this photograph, x,y in 0..1000. Null when the instructions specify only the position of the hand or whole product.")
    single_product: bool = Field(description="Only the one intended item is present, including lookalike objects in the carrier")
    product_at_feet: bool = Field(description="The actual product or its carrier is on the actor's foot or ankle")

    @model_validator(mode="after")
    def wear_is_real(self):
        if self.ok and not all((self.actor_matches, self.product_matches, self.support_matches,
                                self.position_matches, self.contact_point_reachable, self.single_product)):
            raise ValueError("The actual actor, product, support and body position must all match")
        if any(not 0 <= value <= 1000 for xy in (self.feature_xy, self.landmark_xy) if xy for value in xy):
            raise ValueError("Coordinates must be measured within the photograph")
        return self


class MeasuredCarryLook(CarriedLook):
    anatomical_sides_match: bool = Field(description="The actual carrier or supporting hand and intended reachable surface are on the prescribed anatomical sides.")
    named_feature_visible: bool = Field(description="The actual named product feature is visible, identifiable and measurable on this photograph.")
    naturally_reachable: bool = Field(description="The intended hand could naturally reach this same visible feature in its measured position without raising, tilting or detaching the product.")
    only_height_differs: bool = Field(description="The sole material mismatch is the body HEIGHT of this very feature, independently of the hand supporting the product.")
    height_offset: Literal["above", "below", "level", "uncertain"]
    feature_height_quote: str = Field(default="", max_length=350, description=(
        "Exact words that explicitly position the named touch surface at a body landmark. "
        "Leave empty if only the whole product, carrier or supporting hand is positioned there."))
    observed_feature_height: str = Field(min_length=1, max_length=220,
                                         description="Name the observed feature, its real body landmark, and the actual means of support.")

    @model_validator(mode="after")
    def measure_actual_contact(self, info: ValidationInfo):
        quoted = self.feature_height_quote.strip()
        if self.only_height_differs and not quoted:
            raise ValueError("A feature-height mismatch needs an explicit direction about that feature itself. "
                             "Judge the carrier's position and natural reach separately.")
        if quoted and info.context:
            words = [info.context.get("carry", ""), info.context.get("contact", ""),
                     json.dumps(info.context.get("merchant", {}), ensure_ascii=False)]
            if not any(quoted in direction for direction in words):
                raise ValueError("Quote the exact wording specifying the touch surface's body level. "
                                 "When only the supporting hand or whole item has a specified level, leave the quote empty.")
        if self.ok and (not all((self.anatomical_sides_match, self.named_feature_visible, self.naturally_reachable))
                        or self.only_height_differs or self.height_offset != "level"):
            raise ValueError("A passing carried position requires the actual accessible feature at the intended body height")
        return self

    def alignable_direction(self):
        if self.ok or not all((self.actor_matches, self.product_matches, self.support_matches, self.single_product,
                               self.anatomical_sides_match, self.named_feature_visible, self.naturally_reachable,
                               self.only_height_differs, self.feature_xy, self.landmark_xy)):
            return None
        delta = self.feature_xy[1] - self.landmark_xy[1]
        if (self.height_offset == "above" and delta < -10) or (self.height_offset == "below" and delta > 10):
            return self.height_offset
        return None


def _measure_carry(run, plan):
    return plan.mode == "carry" and bool(run.get("commercial_auto_staging"))


def _carry_grade_key(run, plan, photograph, product):
    if _measure_carry(run, plan):
        signature = _fingerprint(photograph, product, plan.carry, plan.contact,
                                 run.get("_carry_requirements"), "measured-height-v2")
    else:
        signature = _fingerprint(photograph, product)
    return f"{recipe.REVIEW}:carry-look:{signature}"


async def _target_guide(ctx, source, target, current=None):
    if not target:
        return None
    with TemporaryDirectory(prefix="commercial-contact-map-") as directory:
        path = Path(directory) / "pose.png"
        await product_story._download(ctx, source, path)
        with Image.open(path) as photo:
            guide = photo.convert("RGB")
    draw = ImageDraw.Draw(guide)
    w, h = guide.size
    size = max(8, round(min(w, h) * .015))
    x, y = target[0] * w / 1000, target[1] * h / 1000
    if current:
        a, b = current[0] * w / 1000, current[1] * h / 1000
        draw.line((a, b, x, y), fill="#ffe42d", width=max(2, size // 4))
        draw.ellipse((a - size / 2, b - size / 2, a + size / 2, b + size / 2), outline="#ffe42d", width=3)
    draw.ellipse((x - size, y - size, x + size, y + size), outline="#00dcff", width=max(3, size // 4))
    name = f"commercial-contact-map-{_fingerprint(source, target, current)}"
    return await _reference_asset(ctx, name, _jpeg(guide, quality=94))


class TrackContactLocation(BaseModel):
    same_cast_support_and_front_camera: bool
    only_fingertip_is_misplaced: bool
    identifiable_landmark: str = Field(min_length=1, max_length=430)
    visible_tip: tuple[int, int] | None = Field(description="X,Y of the actual fingertip in image 1 only, each from 0 to 1000.")
    actual_target: tuple[int, int] | None = Field(description="X,Y of the named feature in image 1, accounting for real perspective. Null if unobservable.")
    finger_root: tuple[int, int] | None = Field(default=None, description="The visible base knuckle of this same active finger in image 1, in the same coordinates.")

    def local_shift(self):
        if (self.same_cast_support_and_front_camera and self.only_fingertip_is_misplaced
                and self.visible_tip and self.actual_target
                and all(0 <= coordinate <= 1000 for xy in (self.visible_tip, self.actual_target) for coordinate in xy)
                and math.dist(self.visible_tip, self.actual_target) >= 8):
            return self.visible_tip, self.actual_target
        return None


def _finger_patch(photo, current, target, root):
    w, h = photo.size
    points = [(int(x * w / 1000), int(y * h / 1000)) for x, y in (current, target)]
    tip, goal = points
    if root and all(0 <= n <= 1000 for n in root):
        knuckle = (int(root[0] * w / 1000), int(root[1] * h / 1000))
        if math.dist(knuckle, tip) < .23 * min(w, h):
            points.append(knuckle)
    cx = (min(x for x, _ in points) + max(x for x, _ in points)) // 2
    cy = (min(y for _, y in points) + max(y for _, y in points)) // 2
    span = max(max(x for x, _ in points) - min(x for x, _ in points),
               max(y for _, y in points) - min(y for _, y in points))
    size = min(w, h, max(round(min(w, h) * .40), span + round(min(w, h) * .21)))
    left, top = min(max(0, cx - size // 2), w - size), min(max(0, cy - size // 2), h - size)
    box = (left, top, left + size, top + size)
    crop = photo.crop(box).convert("RGB")
    guide = crop.copy()
    drawing = ImageDraw.Draw(guide)
    a, b = tip[0] - left, tip[1] - top
    x, y = goal[0] - left, goal[1] - top
    radius = max(4, round(size * .023))
    drawing.line((a, b, x, y), fill="#ffe13a", width=max(1, radius // 4))
    drawing.ellipse((a - radius, b - radius, a + radius, b + radius), outline="#ffe13a", width=2)
    drawing.ellipse((x - radius, y - radius, x + radius, y + radius), outline="#00dcff", width=2)
    mask = Image.new("L", crop.size, 0)
    part = ImageDraw.Draw(mask)
    radius = max(3, round(min(w, h) * .034))
    route = ([points[2]] if len(points) == 3 else []) + [tip, goal]
    route = [(x - left, y - top) for x, y in route]
    part.line(route, fill=255, width=2 * radius)
    for x, y in route:
        part.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return box, crop, guide, mask.filter(ImageFilter.GaussianBlur(max(1, radius / 4)))


async def _edit_track_finger(ctx, prefix, plan, frame, frozen, located, correction, revision):
    current, goal = located.local_shift()
    with TemporaryDirectory(prefix="commercial-track-gesture-") as directory:
        src, patch = Path(directory) / "front.png", Path(directory) / "hand.png"
        await product_story._download(ctx, frame, src)
        with Image.open(src) as image:
            original = image.convert("RGB")
        box, close, marked, mask = _finger_patch(original, current, goal, located.finger_root)
        identity = _fingerprint(frame, frozen, current, goal, box)
        crop, guide = await asyncio.gather(
            _reference_asset(ctx, f"commercial-track-close-{identity}", _jpeg(close, 96)),
            _reference_asset(ctx, f"commercial-track-aim-{identity}", _jpeg(marked, 96)))
        refs = [crop, guide, frame]
        prompt = ("单幅正方形实拍局部。图1是真实正面照片上切下的放大局部，输出仍是这个完全相同的局部、视角和尺寸关系。"
                  "图2是图1的定位副本；黄圈指尖当前位置，蓝圈中心才是指腹要盖住的真实部件。黄蓝之间需要明确移动。"
                  "只调整这只手的手指与关节，让指腹盖住蓝圈中心并真实接触。保留所有商品表面与轮廓，不能把商品移向手。"
                  "图3是图1的完整原片，仅供光线和身体身份参考，不能缩小取景或重拍整个身体。"
                  f"据原图可见：{located.identifiable_landmark}。实物动作：{plan.contact}。{correction}。"
                  "摄影输出不留圈线、文字；原位置不能残留第二根指尖。")
        name = f"commercial-track-fingertip-{_fingerprint(refs, prompt, revision)}"
        edited = await _still(ctx, prefix, name, prompt, refs, size="1024x1024", quality="medium", language=plan.language)
        await product_story._download(ctx, edited, patch)
        with Image.open(patch) as adjusted:
            local = adjusted.convert("RGB").resize(close.size, Image.Resampling.LANCZOS)
        original.paste(local, box, mask)
        output = BytesIO()
        original.save(output, format="PNG")
        return await _reference_asset(ctx, f"commercial-track-local-{_fingerprint(identity, edited)}", output.getvalue(),
                                      mime="image/png")


async def _retarget_track_landing(ctx, prefix, run, plan, shot, frame, library, product, correction, revision):
    frozen = library.get("track_contact")
    if not frozen or not shot["product_visible"] or _scene(plan, shot) != "track":
        return None
    key = f"{recipe.REVIEW}:track-landing-location:{_fingerprint(frame, frozen, product, plan.contact, 'finger-root')}"
    proof = (run.get("director_decisions") or {}).get(key)
    if not proof:
        answer = await reference_quality.ask(
            ctx, "commercial_track_landing_location", TrackContactLocation, REVIEW_LANGUAGE[plan.language] +
            "图1是已经拍出的跑道正面中景末帧，图2是同演员和同商品核准触点的不同视角，图3本场跑道，其后原商品。"
            "只在图1中定位。先从真实轮廓、部件交界、可见纹理或身体标志指认文字要触碰的部件，"
            "解释为何能在当前透视里识别它。再分别标实际指尖和应接触的位置，只使用图1自己的坐标。"
            "弯曲表面不要平均两个图像边缘像素估中心；不同视角不复制像素坐标。若只能猜被挡住的部件、"
            "不确定哪只手、商品错或支撑和机位也错，就不把它当成可做的局部移动。"
            "已经实触正确区域时也不安排移动；两张静帧不能证明活动手到达之前的方向。",
            {"language": plan.language, "contact": plan.contact, "carry": plan.carry, "product": plan.product,
             "appearance": plan.appearance}, [frame, frozen, library["track"], *product], max_tokens=1050, timeout=65)
        proof = await _record(ctx, prefix, run, 17, key, answer)
    located = TrackContactLocation.model_validate(proof["report"])
    if not located.local_shift():
        return None
    return await _edit_track_finger(ctx, prefix, plan, frame, frozen, located, correction, revision)


async def _grade_carried_photo(ctx, prefix, run, plan, candidate, cast, product):
    key = _carry_grade_key(run, plan, candidate, product)
    schema = MeasuredCarryLook if _measure_carry(run, plan) else CarriedLook
    data = {"language": plan.language, "carry": plan.carry, "contact": plan.contact, "product": plan.product,
            "appearance": plan.appearance, "mode": plan.mode}
    if run.get("_carry_requirements"):
        data["merchant"] = run["_carry_requirements"]
    proof = (run.get("director_decisions") or {}).get(key)
    if not proof:
        answer = await reference_quality.ask(
            ctx, "commercial_carried_look", schema, REVIEW_LANGUAGE[plan.language] +
            "图1是同一演员和实际商品的合体定装，图2是演员原定装，图3起是用户原始商品图。"
            "核对脸、服装、商品外形、真实承重、相对演员本人左右/高度与carry一致。"
            "承重先看商品重量实际上落在哪一侧的手、携具或身体部位，以及姿态是否稳当。若商家未指定握法，"
            "同一只手在同一支撑区域自然地托、握或调整指头，只要能稳住实物、保留预定用法和可及触点，都算真实承重。"
            "区分手或整件商品的携带位置，与被碰部件本身的指定高度。先读原文看高度指的是谁。"
            "只有明确要求接触面本身在某人体标志高度，才把该部件对准这个标志；此时只看到承重的手在标志处不够。"
            "仅说整件贴着身体哪侧、手托在髋旁，保留物件真实尺寸，凸起部件自然高出支撑点。"
            "只要承重及物件位置符合原文、指定的手无须抬落商品就能自然够到该可见面，contact_point_reachable为真。"
            "接受自然透视与人体活动的小幅余地；不要求一张定妆真的把指尖碰上去。"
            "实测feature_xy指向图1可看到的商品目标部件，landmark_xy指向同一照片里该部件应落到的人体位置，"
            "两者使用整幅图0到1000坐标。若目标看不见请用null，别猜测看不见的部件。"
            "不能把在桌上看起来和腰一样高当成腰间携带。自然被包覆盖的部位不用硬露出，但该露出的颜色和结构必须一致。"
            "独立观察整幅照片，主体在台面或掌中同时携具中另有小复制品、不同材质商品或模拟色块应拒绝。"
            "按用户商品实际成套的部件数量核对；原本是一对或一套的组成部分不算多一件。"
            "product_at_feet只有实际商品/专用携具在脚或踝的位置才为true。只在确有可见错位或错货时要求针对性改图。"
            + ("另外以图1本身独立观察：不要求现在伸出接触手，但应能确认真实承重手/携具在指定身体侧，"
               "且按剧本指定的那只手能自然够到这次确实看得见的同一部位。前者是anatomical_sides_match，后者是naturally_reachable。"
               "在observed_feature_height用成片语种写具体可见部件、它相对于真人的高度及真实承重；只在唯一错误是这个部件的"
               "高度、外形/承重/左右/数量均正确时only_height_differs=true。height_offset比较该部件与剧本要求："
               "若商家或剧本明确规定所碰部件本身的身体高度，feature_height_quote逐字引用限定该部件的原文。"
               "上方来手、握持部位或整件被携于髋旁的描述不是对商品顶部定高，此时引用为空；"
               "获得真实支撑且自然够到时height_offset=level，landmark_xy留空，记录实际feature_xy供后续三次切点锁定。"
               "明确指定了该部件高度时above高、below低、level在该真实标志和自然视差容差内。被遮挡或无法辨别返回uncertain。"
               if schema is MeasuredCarryLook else ""),
            data, [candidate, cast, *product],
            max_tokens=1240, timeout=65)
        proof = await _record(ctx, prefix, run, 10, key, answer)
    return schema.model_validate(proof["report"], context=data)


async def _carried_actor(ctx, prefix, run, plan, cast, clean, product):
    from light import commercial_calibration

    revision, correction = _requested_repairs(run).get(10, (0, ""))
    stored = run.get("commercial_carry_calibration")
    tried = set()

    async def accept_setup(photo, check):
        if check.ok:
            return photo, check.product_at_feet, plan
        if (stored or not _measure_carry(run, plan) or not check.alignable_direction() or photo in tried):
            return None
        tried.add(photo)
        # This one real pose is already usable. The merchant brief and all three gesture directions are independently
        # checked against the exact photograph before the camera can use it. Another exposure adds no evidence of fit.
        report = check.model_dump()
        aligned = await commercial_calibration.align_carry(ctx, prefix, run, plan, photo, cast, product,
            {**report, "checks": [{"photograph": photo, "report": report}]})
        if aligned is None:
            return None
        measured = await _grade_carried_photo(ctx, prefix, run, aligned, photo, cast, product)
        if measured.ok:
            return photo, measured.product_at_feet, aligned
        raise _rejected(aligned, recipe.shots(aligned)[9], measured.reason, measured.correction, staging=True)

    if stored:
        if (stored["cast"] != cast or stored["source_ids"] != list(product)
                or stored["after_plan_digest"] != commercial_calibration._digest(plan)):
            raise RemakeStopped({"zh": "已校准的演员、商品或分镜与本次参考不一致。",
                                 "vi": "Diễn viên, sản phẩm hoặc phân cảnh đã hiệu chỉnh không khớp với bản đang dựng.",
                                 "en": "The calibrated performer, product or storyboard differs from the current edit."}[plan.language])
        candidate = stored["photograph"]
        verdict = await _grade_carried_photo(ctx, prefix, run, plan, candidate, cast, product)
        if verdict.ok:
            return candidate, verdict.product_at_feet, plan
        if not revision:
            raise _rejected(plan, recipe.shots(plan)[9], verdict.reason, verdict.correction, staging=True)
        # A new customer-requested correction may re-photograph this rejected plate under the already fixed plan.
        correction = " ".join(filter(None, (verdict.correction, correction)))
    elif _measure_carry(run, plan):
        # Older drafts sometimes rejected a supported item because its top extended above the carrying hand.
        # Re-read only plates belonging to these exact directions, actor and source photos.
        previous = list((run.get("director_decisions") or {}).items())
        for old_key, old in previous:
            evidence = old.get("evidence") or []
            if not old_key.startswith(f"{recipe.REVIEW}:carry-look:") or evidence[1:] != [cast, *product]:
                continue
            photo = evidence[0]
            old_signature = _fingerprint(photo, product, plan.carry, plan.contact, "measured-height-v1")
            if old_key not in {f"{recipe.REVIEW}:carry-look:{old_signature}", _carry_grade_key(run, plan, photo, product)}:
                continue
            verdict = await _grade_carried_photo(ctx, prefix, run, plan, photo, cast, product)
            if accepted := await accept_setup(photo, verdict):
                return accepted
    candidate, marked = None, None
    verified_shape = next((v.get("identification", {}).get("visible_identifiers")
                           for k, v in (run.get("director_decisions") or {}).items()
                           if k.startswith("commercial-product:neutral-photograph:") and v.get("approved")
                           and v.get("photograph") in clean), None)
    for attempt in (1, 2):
        refs = ([candidate] if candidate else []) + ([marked] if marked else []) + [cast, *clean]
        prompt = (STYLE + "拍一张真人已实际佩戴/携带这件商品的定装参考。素色摄影棚、单幅、没有台面或其他人物。"
                  f"演员按人物定妆：{plan.protagonist}。携带方式必须具体按：{plan.carry}。"
                  "图中脸、服装、产品和真正承重的携具体现在同一个自然站姿里。从脸拍到实际支撑部位，"
                  "按演员自身的左右和高度确定位置。应由包、腰套、手或穿戴位置承重就照实拍，不能漂浮、放桌上或另造托架。"
                  f"后续指尖要触碰的位置是：{plan.contact}。" +
                  ("定妆时尚未触碰。明确指定了某个部件本身的身体高度，就按实拍出该部件；只指定手、携具或整件所在位置，"
                   "就按正常尺寸握持，让较高或凸出的部件自然高出支撑点，保持另一只手能直接够到该部件。"
                   if plan.mode == "carry" else "定妆时尚未碰它，但商品上指名的那个具体部位已经在指定的人体高度，"
                   "不是只把商品中心、底端、承重的手或袋放到该高度。") +
                  "保留真实尺寸，调整同一只手或携具到自然支撑处；"
                  "取景必须能辨认这个具体部件和相邻身体标志，不必拍到脚以致触点太小。"
                  "人物原定装里的空袋现在按实际方式装好；商品按干净外形卡保留可见部分，"
                  "原本遮在袋中或穿戴部位背后的部分照常遮挡。整个画面只出现这同一件商品，属于同一套的必要部件按真实数量保留，"
                  "它已在指定位置承重；携具上不再加第二个商品、仿制小件或色块代替商品。此图不表演后面触碰或交接的动作。")
        if verified_shape:
            prompt += ("所附商品摄影图的轮廓已与同一上传的多个角度独立对照。按实际可见且已核准的部件、开口和边缘取形："
                       f"{verified_shape}。未知背面保持遮挡，不把已移除的原图前景当作商品的缺口或附属物。")
        if correction:
            prompt += "上次明确需改的地方：" + correction
        if marked:
            prompt += ("第2张是上一图的定位标记。黄标是商品具体部件当前的位置，蓝圈是该部件应有的人体高度。"
                       "在第1张干净底片上按两标之间的位移移动整件商品与真正承重的手或携具，物件尺寸相同，"
                       "让这个部件到达蓝圈，不要只改变手指、缩短商品或移动演员身体。成图不带标线。")
        name = f"commercial-carried-{_fingerprint(refs, prompt, revision)}"
        candidate = await _still(ctx, prefix, name, prompt, refs, size=WIDE, quality="medium", language=plan.language)
        verdict = await _grade_carried_photo(ctx, prefix, run, plan, candidate, cast, product)
        if accepted := await accept_setup(candidate, verdict):
            return accepted
        correction = verdict.correction
        if attempt == 1 and not verdict.contact_point_reachable and verdict.feature_xy and verdict.landmark_xy:
            marked = await _target_guide(ctx, candidate, verdict.landmark_xy, verdict.feature_xy)
    raise _rejected(plan, recipe.shots(plan)[9], verdict.reason, verdict.correction, staging=True)


class FingerPose(FrameCheck):
    actual_product: bool
    actual_contact: bool
    anatomical_hand: bool
    support_and_height: bool

    @model_validator(mode="after")
    def require_visible_pose(self):
        if self.ok and not all((self.actual_product, self.actual_contact, self.anatomical_hand, self.support_and_height)):
            raise ValueError("The actual performer must make a visible, physically supported contact")
        return self


async def _freeze_contact(ctx, prefix, run, plan, cast, product):
    key = _carry_grade_key(run, plan, cast, product)
    measurements = ((run.get("director_decisions") or {}).get(key) or {}).get("report") or {}
    marker = await _target_guide(ctx, cast, measurements.get("feature_xy"))
    revision, correction = _requested_repairs(run).get(11, (0, ""))
    candidate = None
    for attempt in (1, 2):
        refs = [cast, *([marker] if marker else []), *([candidate] if candidate else [])]
        prompt = (STYLE + "图1是已独立核准的演员、商品、穿戴/承重和高度。在同一素色摄影棚只拍一张指尖已到位的定妆。"
                  f"按演员本人左右，仅执行这个真实触点：{plan.contact}。持续按{plan.carry}承重，商品和相邻人体部位不挪。"
                  "从能同时看到脸、身体侧别和这个部件的同侧中近景取景，手和前臂从正确肩膀自然伸来。"
                  "指腹与指定的可见外沿或表面真正相触，其余手指自然；只需要足够辨认的部位，不强迫隐蔽细节露出。")
        if marker:
            prompt += "图2是图1的定位副本，蓝圈中心是产品上该碰的位置。只有手向这一点移动，成片中没有蓝圈、标记和线。"
        if correction:
            prompt += "保留已核准的商品位置和身份，只修：" + correction
        name = f"commercial-matched-contact-{_fingerprint(refs, prompt, revision)}"
        candidate = await _still(ctx, prefix, name, prompt, refs, size=WIDE, quality="medium", language=plan.language)
        key = f"{recipe.REVIEW}:frozen-contact:{_fingerprint(candidate, cast, product)}"
        decision = (run.get("director_decisions") or {}).get(key)
        if not decision:
            verify = await reference_quality.ask(
                ctx, "commercial_freeze_contact", FingerPose, REVIEW_LANGUAGE[plan.language] +
                "图1是待锁定的实际触点姿态，图2是未触碰的正确携带状态，之后是原商品图。辨认同一实际演员、衣着与商品、"
                "同一支撑和商品部件相对人体高度，判断是正确一只手的指尖向可见的目标实际触碰。"
                "触点已在同一个可辨认部位时不比较毫米差异，无需展示背后隐藏的结构；"
                "若实际是另一只手、明显另一部件、目标远离指定身体高度、手与物仍有空隙、穿透或有指导标记残留，须退回。"
                "只从此单幅图审已到位的姿态。手指轴向或手腕相对高度不能证明手在前一瞬间从哪里运动过来；"
                "除非图中真的碰了指定表面的另一面，不可仅因推测接近轨迹拒绝已正确到位的接触。"
                "真实接近方向留给随后起止两图与动态实拍核验。"
                "识别解剖左右时看手臂与自身肩部相连的方向，不能按画面左右猜。只给具体可执行的一条修改。",
                {"language": plan.language, "contact": plan.contact, "carry": plan.carry, "appearance": plan.appearance},
                [candidate, cast, *product], max_tokens=1050, timeout=65)
            decision = await _record(ctx, prefix, run, 11, key, verify)
        verdict = FingerPose.model_validate(decision["report"])
        if verdict.ok:
            return candidate
        correction = verdict.correction
    raise _rejected(plan, recipe.shots(plan)[10], verdict.reason, verdict.correction, staging=True)


class TrackCast(BaseModel):
    ok: bool
    reason: str = Field(min_length=1, max_length=580)
    neutral_correction: str = Field(default="", max_length=360)
    pose_correction: str = Field(default="", max_length=360)
    shoes_box: list[int] | None = Field(default=None, min_length=4, max_length=4,
        description="Actual footwear on image 1, tight left,top,right,bottom in 0..1000 coordinates including both feet.")

    @model_validator(mode="after")
    def validate_uniform(self):
        self.neutral_correction = _edit_note(self.neutral_correction)
        self.pose_correction = _edit_note(self.pose_correction)
        if self.ok == bool(self.neutral_correction or self.pose_correction):
            raise ValueError("Identify the specific wardrobe picture to correct")
        if self.ok and self.shoes_box is None:
            raise ValueError("A full-length approved running portrait must show the actual footwear")
        if self.shoes_box is not None:
            left, top, right, bottom = self.shoes_box
            if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                raise ValueError("Footwear bounds must lie inside the image")
        return self


async def _shoe_detail(ctx, source, box, *, lower_body=False):
    with TemporaryDirectory(prefix="commercial-running-shoes-") as directory:
        path = Path(directory) / "wardrobe.png"
        await product_story._download(ctx, source, path)
        with Image.open(path) as photo:
            w, h = photo.size
            left, top, right, bottom = box
            margin = 0 if lower_body else max(3, round((right - left) * .06))
            crop = photo.convert("RGB").crop((int(max(0, left - margin) * w / 1000), int(max(0, top - margin) * h / 1000),
                                              math.ceil(min(1000, right + margin) * w / 1000), math.ceil(min(1000, bottom + margin) * h / 1000)))
    detail = "lower-body" if lower_body else "shoes"
    return await _reference_asset(ctx, f"commercial-track-{detail}-{_fingerprint(source, box)}", _jpeg(crop, 94))


async def _running_cast(ctx, prefix, run, plan, library, material):
    from light import commercial_wardrobe

    wardrobe = getattr(plan, "track_wardrobe", "")
    if not wardrobe:
        return
    original_base = library.get("carried_actor") or library["protagonist"]
    base = library.get("resting_carried_actor") or original_base
    pose = library.get("contact_actor")
    repairs = _requested_repairs(run)

    async def keep(neutral, touched, check):
        library["track_actor"] = neutral
        if touched:
            library["track_contact"] = touched
        library["track_feet"] = await _shoe_detail(ctx, neutral, check.shoes_box)

    for key, proof in (run.get("director_decisions") or {}).items():
        pictures = proof.get("evidence") or []
        split = 2 if pose else 1
        if (key.startswith(f"{recipe.REVIEW}:track-wardrobe:") and proof.get("report", {}).get("ok")
                and len(pictures) > split and pictures[split] in (base, original_base)
                and pictures[split + 1:] == [*([pose] if pose else []), *material]
                and key == f"{recipe.REVIEW}:track-wardrobe:{_fingerprint(pictures, wardrobe)}"):
            await keep(pictures[0], pictures[1] if pose else None, TrackCast.model_validate(proof["report"]))
            return  # Its resting hand is checked independently below; preserve the photographed clothing and contact.
    if await commercial_wardrobe.resolve(ctx, prefix, run, plan, library, material):
        return

    async def dress(frozen, number, counterpart=None, prior=None, correction=""):
        revision, note = repairs.get(number, (0, ""))
        refs = [frozen, *([counterpart] if counterpart else []), *([prior] if prior else [])]
        clothing = ("沿用图2已经确定的运动服、鞋和保留下来的必要携具，严格保留图1已核定的同手同部位的实际触点。"
                    if counterpart else "拍同一演员正常站立的全身定装，头、实际承重点和鞋均入画；保持图1未开始触碰的自然姿态。")
        prompt = (STYLE + "图1是通勤造型。保留同一个人的脸、肤色、发型、体态，只替换跑道场次需要改变的非商品服装。"
                  f"跑道规定的服装与携具：{wardrobe}。{clothing}该商品是{plan.product}。"
                  "若商品自己就是服装、随身包或鞋，保留这一件真商品；只换其他衣物。只移除规定的非商品通勤物，"
                  "必要的承重方式、物件和身体侧别不变。固定设备不出现在室外定妆。素色摄影背景，只有一个人。"
                  f"{correction} {note}".strip())
        name = f"commercial-track-wardrobe-{_fingerprint(prompt, refs, revision)}"
        return await _still(ctx, prefix, name, prompt, refs, size=WIDE, quality="medium", language=plan.language)

    neutral = await dress(base, 12)
    touched = await dress(pose, 17, neutral) if pose else None
    for attempt in (1, 2):
        charts = [neutral, *([touched] if touched else []), base, *([pose] if pose else []), *material]
        key = f"{recipe.REVIEW}:track-wardrobe:{_fingerprint(charts, wardrobe)}"
        proof = (run.get("director_decisions") or {}).get(key)
        if not proof:
            response = await reference_quality.ask(
                ctx, "commercial_running_cast", TrackCast, REVIEW_LANGUAGE[plan.language] +
                "图1是跑道全身定装，有触点图时图2是同场已触碰的姿态照。之后依序是对应的通勤原型与真实商品原图。"
                "两种运动姿态必须是同一人同一套运动造型，和通勤时明显按要求换了非商品服饰，脸与身体身份相同。"
                "同一件实际商品、必要承重点、高度和左右不应随服装改变；只有不是商品的通勤附属物才按剧本拿掉。"
                "固定商品不出现在跑道。若有触点图，与通勤已核定实际接触的手、部件和身体相对位置一致，"
                "允许视角和轻微遮挡，不推测单张照片的运动轨迹。分别指出非改不可的一端；"
                "无需改图的correction字段必须返回空字符串，不填写‘不用修’字样。从图1量取实际鞋的像素范围。",
                {"language": plan.language, "wardrobe": wardrobe, "product": plan.product, "mode": plan.mode,
                 "carry": plan.carry, "contact": plan.contact, "has_contact_photo": bool(touched)},
                charts, max_tokens=1100, timeout=65)
            proof = await _record(ctx, prefix, run, 12, key, response)
        check = TrackCast.model_validate(proof["report"])
        if check.ok:
            await keep(neutral, touched, check)
            return
        if attempt == 1:
            if check.neutral_correction:
                neutral = await dress(base, 12, prior=neutral, correction=check.neutral_correction)
            if pose and check.pose_correction:
                touched = await dress(pose, 17, neutral, touched, check.pose_correction)
    if await commercial_wardrobe.resolve(ctx, prefix, run, plan, library, material):
        return
    target = 12 if check.neutral_correction or not pose else 17
    raise _rejected(plan, recipe.shots(plan)[target - 1], check.reason, check.neutral_correction or check.pose_correction,
                    staging=True)


class RestingLook(FrameCheck):
    actual_posture: str = Field(min_length=1, max_length=460,
        description="Describe each visible hand, what physically supports the item, and where the gesture finger is relative to the named feature.")
    same_person_item_and_support: bool
    gesture_hand_at_rest: bool

    @model_validator(mode="after")
    def keep_appearance_and_unperformed_gesture(self):
        if self.ok != (self.same_person_item_and_support and self.gesture_hand_at_rest):
            raise ValueError("The actor and supported product must match, and the scheduled touch must still be unperformed.")
        return self


async def _resting_costumes(ctx, prefix, run, plan, library, product, clean, *, role=None):
    if plan.mode == "stationary" or getattr(plan, "score_revision", 1) != 2:
        return
    roles = [("carried_actor", 10)]
    if library.get("track_actor"):
        roles.append(("track_actor", 16))
    if role:
        roles = [item for item in roles if item[0] == role]

    async def neutral(role):
        name, number = role
        base = library[name]
        candidate = base
        uniform = getattr(plan, "track_wardrobe", "") if name == "track_actor" else plan.protagonist
        revision, note = _requested_repairs(run).get(number, (0, ""))
        decisions = run.setdefault("director_decisions", {})
        seal = f"{recipe.REVIEW}:resting-approved:{name}:{_fingerprint(base, library['protagonist'], product, uniform, plan.carry, plan.contact, revision)}"
        if prior := decisions.get(seal):
            grade = decisions.get(prior.get("proof"), {})
            if grade.get("evidence", [])[:1] == [prior.get("picture")] and RestingLook.model_validate(grade["report"]).ok:
                return name, prior["picture"]
        for attempt in range(3):
            key = f"{recipe.REVIEW}:resting-{name}:{_fingerprint(candidate, base, plan.carry, plan.contact, product)}"
            proof = (run.get("director_decisions") or {}).get(key)
            if not proof:
                refs = [candidate, *([base] if candidate != base else []), library["protagonist"], *product]
                answer = await reference_quality.ask(
                    ctx, "commercial_resting_pose", RestingLook, REVIEW_LANGUAGE[plan.language] +
                    "请看图1实际姿态。这张图供尚未开始转场手势之前的正常穿戴、站立/跑步使用。"
                    "明确说出两只手分别在哪里。无须承重的活动手自然垂下或在正常跑步摆臂位置，远离要碰的部件；"
                    "它若已抬到触点旁、指着商品、扶着本可自行穿戴的商品或已经碰到指定面，须退回。"
                    "若活动手本身必须提、托、握商品，其他手指/手掌继续真实承重，只需活动指自然收回、有清楚的伸展余地。"
                    "明确指出能看见的手腕与所碰部件的相对位置；手垂下因近景落在画外可以接受，"
                    "若却看到前臂已朝触点举起与指部重叠，不能推测它已自然放下。"
                    "若有原定妆，对比同人的衣着、产品所在部位、左右、实际承重及目标部件高度，均须保留。"
                    "这是专门检查活动手有没有提前做动作。原合体的支撑和身体位置已经独立验收；future_gesture仅定位做动作的手和部件，"
                    "原样姿态不再以文字推算部件应该移动到何处。若有修图，再依原照片逐项核对手以外的位置和承重。"
                    "只有这两个判断决定ok，两项均成立必须通过。"
                    "基础人物照只供辨认身份，允许按本场衣装换造型。原商品图核对同款，同一画面不能多一件。",
                    {"language": plan.language, "carry": plan.carry, "future_gesture": plan.contact,
                     "costume": uniform, "roles": ["candidate", *(["previous costume and support"] if candidate != base else []),
                                                  "base actor identity", *["real product"] * len(product)]},
                    refs, max_tokens=1150, timeout=65)
                proof = await _record(ctx, prefix, run, number, key, answer)
            check = RestingLook.model_validate(proof["report"])
            if check.ok:
                kept = await _record(ctx, prefix, run, number, seal, {"picture": candidate, "proof": key})
                return name, kept["picture"]
            if attempt == 2:
                raise _rejected(plan, recipe.shots(plan)[number - 1], check.reason, check.correction, staging=True)
            refs = [base, library["protagonist"], *clean[:1]] if attempt == 0 else [library["protagonist"], *clean[:1], base]
            direction = ("先依图1原定妆保持衣着、脸、真实商品、承重与身体侧别，只把做标志动作的那只手复位。"
                         if attempt == 0 else
                         "重新拍站立全身肖像。第一张取演员身份，末张只取本场服装、商品及支撑；不要复演末张的手势。")
            prompt = (STYLE + direction + f"本场衣着：{uniform}。商品按{plan.carry}保持同一承重、方向及高度。"
                      "做手势的这一只手放松，在画面里清楚垂在自己身体一侧，手腕与指尖和已摆好的目标相隔一段手臂行程。"
                      "若该手还要承重，其余指和手掌保持原有必要的握持，单独收回活动指至自然的未伸展状态。"
                      "全画只有一个演员和一份已穿戴或携带的商品。其他配件、表情、光线、鞋、背景不变。"
                      f"按真实样片只处理这处：{check.correction}。{note}")
            camera = f"commercial-resting-{name}-{_fingerprint(base, refs, prompt, attempt, revision)}"
            candidate = await _still(ctx, prefix, camera, prompt, refs, size=WIDE, quality="medium", language=plan.language)

    for name, picture in await _parallel(roles, neutral, concurrency=2):
        library["resting_" + name] = picture


def _wear_reference(shot, library, ending):
    idx = shot["id"]
    if idx == 13 and library.get("track_feet"):
        return library["track_feet"]
    if 12 <= idx <= 17 and library.get("track_actor"):
        contact = idx == 12 and not ending or idx == 17 and ending
        return (library.get("track_contact") if contact else None) or library.get("resting_track_actor") or library["track_actor"]
    if idx in (18, 24) and not ending and library.get("track_contact"):
        return library["contact_actor"]
    if ending and idx in MATCH_IN.values() and library.get("contact_actor"):
        return library["contact_actor"]
    if idx in (13, 19) and not library.get("carry_at_feet"):
        return None
    if 9 <= idx <= 24 or idx == 25 and not ending:
        return library.get("resting_carried_actor") or library.get("carried_actor")
    return None


async def _legacy_insert_openings(ctx, prefix, run, plan, shots, material):
    if len(getattr(plan, "insert_openings", [])) == 5:
        return
    key = f"{recipe.REVIEW}:insert-openings:{_fingerprint(plan.inserts)}"
    decision = (run.get("director_decisions") or {}).get(key)
    if not decision:
        answer = await reference_quality.ask(
            ctx, "commercial_insert_openings", InsertOpenings,
            "为旧版已保存的五个短插镜逐一提取动作刚发生之前的单幅真实画面状态。"
            "每条只写已经入画的具体器物、手所在的位置和当时开合/盛装状态。不预演下一步，不描述过程或结果。"
            "保持前一插镜已完成的状态，与同一生活流程相连，用language指定语言输出五条。",
            {"language": plan.language, "routine": plan.routine, "actions": plan.inserts}, material,
            max_tokens=1000, timeout=55)
        decision = await _record(ctx, prefix, run, 4, key, answer)
    for shot, state in zip(shots[3:8], decision["report"]["frames"], strict=True):
        shot["frame"] += " " + state


class ActionArea(FrameCheck):
    box: list[int] = Field(min_length=4, max_length=4,
                           description="left, top, right, bottom of the moving region in 0..1000 coordinates")

    @model_validator(mode="after")
    def visible_box(self):
        x, y, r, b = self.box
        if not (0 <= x < r <= 1000 and 0 <= y < b <= 1000):
            raise ValueError("The action region must lie within the actual frame.")
        return self


class Enacted(FrameCheck):
    opening: str = Field(min_length=1, max_length=230)
    middle: str = Field(min_length=1, max_length=230)
    ending: str = Field(min_length=1, max_length=230)
    same_item: bool
    before_contact: bool = Field(description="In the real first frame, the touch, fitting, transfer or change of user has not yet happened")
    continuous_action: bool
    supported: bool
    reached: bool

    @model_validator(mode="after")
    def require_performance(self):
        if self.ok and not all((self.same_item, self.before_contact, self.continuous_action, self.supported, self.reached)):
            raise ValueError("The photographed action must start before contact and reach its supported end in a single motion")
        return self


class EnactedSelection(Enacted, Selection):
    """The chosen paid interval and its full physical action share one viewing."""


def _same_plate(first, last, box):
    first = first.convert("RGB")
    last = last.convert("RGB").resize(first.size, Image.Resampling.LANCZOS)
    width, height = first.size
    left, top, right, bottom = box
    mask = Image.new("L", first.size, 0)
    ImageDraw.Draw(mask).rectangle((left * width // 1000, top * height // 1000,
                                    right * width // 1000, bottom * height // 1000), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, width / 190)))
    output = BytesIO()
    Image.composite(last, first, mask).save(output, "PNG")
    return output.getvalue()


async def _lock_handoff_background(ctx, prefix, run, plan, first, last):
    key = f"{recipe.REVIEW}:motion-area:{_fingerprint(first, last)}"
    decision = (run.get("director_decisions") or {}).get(key)
    if not decision:
        answer = await reference_quality.ask(
            ctx, "commercial_handoff_edit_region", ActionArea, REVIEW_LANGUAGE[plan.language] +
            "这是同一固定机位的一次交接的首尾图。请在两张图同一套0到1000坐标中框出完成实际交接所需的运动区，"
            "包含原来和最终的手、前臂、移动主商品的位置及两者之间的路径。明确固定的附件及桌角应尽量在这个框外。"
            "按transfer_mode判断：fit包含实际穿戴部位，access包含更换使用位置的身体部位；保留商品真实支撑。"
            "若两张的相机或人物身体位置相差太远、运动和静物区域无法分离，请ok=false并给修正。"
            "不要凭空造另一个动作。", {"action": plan.handoff, "mode": plan.mode, **_transfer(plan)}, [first, last],
            max_tokens=800, timeout=55)
        decision = await _record(ctx, prefix, run, 26, key, answer)
    area = ActionArea.model_validate(decision["report"])
    if not area.ok:
        raise _rejected(plan, recipe.shots(plan)[25], area.reason, area.correction)
    with TemporaryDirectory(prefix="commercial-handoff-area-") as directory:
        a, b = Path(directory) / "first.png", Path(directory) / "last.png"
        await asyncio.gather(product_story._download(ctx, first, a), product_story._download(ctx, last, b))
        with Image.open(a) as plate, Image.open(b) as movement:
            image = _same_plate(plate, movement, area.box)
    return await _reference_asset(ctx, f"commercial-locked-handoff-{_fingerprint(first, last, area.box)}",
                                  image, mime="image/png")


def _reason(language, reason):
    if language in {"vi", "en"} and _has_cjk(reason):
        return {"vi": "Chưa xác nhận được động tác hoặc tính liên tục của cảnh sau các lần quay lại tự động.",
                "en": "The action or continuity could not be verified after automatic retakes."}[language]
    return reason


def _rejected(plan, shot, reason, correction, *, staging=False):
    label = ({"vi": "Kiểm tra ảnh trước khi quay", "zh": "开拍前定装检查", "en": "Pre-shoot reference check"}[plan.language]
             if staging else f"{shot['id']:02} · {shot['title']}")
    description = f"{label} — {_reason(plan.language, reason)}"
    if plan.language in {"vi", "en"} and (not correction or _has_cjk(correction)):
        correction = ({"vi": "Chụp lại ảnh tham chiếu với đúng diễn viên, sản phẩm, cách cầm hoặc cách đeo.",
                       "en": "Retake the reference with the same performer, product and physical support."} if staging else
                      {"vi": "Giữ nguyên góc máy và bối cảnh của cảnh này. Dùng đúng sản phẩm trong ảnh tham chiếu, "
                             "thực hiện động tác một lần từ tư thế mở đầu đến điểm cắt đã duyệt.",
                      "en": "Keep the shot's camera angle and setting. Use the product in the reference and perform "
                            "the action once from the approved opening pose to the cut point."})[plan.language]
    return ReviewRejected(shot["id"], description, correction, kind="commercial")


def _requested_repairs(run):
    latest = {}
    for repair in run.get("commercial_repairs") or []:
        target, revision = repair.get("target"), repair.get("revision")
        if isinstance(target, int) and isinstance(revision, int) and revision > latest.get(target, (0, ""))[0]:
            note = (repair.get("report") or {}).get("correction") or repair.get("note") or ""
            latest[target] = (revision, note)
    return latest


def _mode(plan, visible):
    if plan.mode == "stationary" and not visible:
        return "固定商品在既定使用场地，外景仅以人物空手动作相接。"
    if visible:
        fixed = "保持原安装位置、真实比例和支撑。" if plan.mode == "stationary" else ""
        return f"本镜看清一件真实商品。{fixed}外形依据用户图：{plan.appearance}。"
    if plan.mode == "wear":
        return f"穿戴状态连续，若穿戴部位入画则严格沿用真实外形：{plan.appearance}。"
    return "产品可按前镜状态留在画外，本镜以指定动作、景别为主。"


def _product_ref_needed(plan, shot):
    return (shot["product_visible"] or
            plan.mode in {"carry", "wear"} and shot["id"] in {6, 7, 8, 9, 10, 14, 16, 20, 21, 22, 25, 28})


def _carry_state(plan, shot, *, ending=False):
    i = shot["id"]
    if i == 9:
        if plan.mode == "stationary":
            return ("出门前只合上一件随身装备的现有扣件。固定商品留在原安装处，不能把它缩小放进包。"
                    "只需看见手、拉链或搭扣和邻近布料。")
        location = ("已佩戴的商品一直戴在同一部位。若佩戴部位入画仍应看见它，切勿把它放上桌或塞进包。"
                    if plan.mode == "wear" else
                    "按原有拿持或明确指定的携具继续承重；只有明确写在该包收纳才可在该处，不能自行改成入包。")
        return (f"已使用的商品按指定实际位置继续穿戴/携带：{plan.carry}。{location}"
                "镜中只合上现有出门装备的扣或拉链。它与商品不是同一物时，收紧到手、扣件与少量装备表面，"
                "商品和人物相应部位都可以留在画外。若商品自身就是包、服装或带扣装备，则只合它自己的原有扣件，"
                "保持同一件、真实位置和支撑，不另造容器或复制品。")
    if plan.mode not in {"carry", "wear"} or not (10 <= i <= 24 or i == 25 and not ending):
        return ""
    phase = "本镜首帧进入工作场所时仍按" if i == 25 else "本镜全程按"
    return (f"{phase}已指定的实际携带、穿戴与支撑位置：{plan.carry}。"
            "维持演员自身的左右、部位和实际承重点；按镜头大小自然可见，不能用新桌面、矮台代替指定的身体、"
            "携具或手持支撑。整幅只有这一件，画面其他位置或携具里不能多出缩小的复制品、不同材质的替代品。"
            + ("已经走到同一室内通向门口的一侧，构图围绕演员及随身位置；初始陈列面在画外。"
               "若极远处碰巧看得到陈列位，该处已经清空。" if i in (10, 11) else "")
            + ("只有之后发生明确取出动作才能改变状态。" if i == 25 else ""))


TRANSFER_PHASES = {
    "pass": ("主人公独自持有和承重，接收者仍空手且与接管处有可见空隙。",
             "接收者用同一只或双手接管并承重，主人公的手已撤开。",
             "沿用接收者已经独自持有的姿态，再开始体验。"),
    "fit": ("商品仍由主人公自己实际穿戴承重，主管还没有穿戴这一件。",
            "主人公已把同一件商品戴到主管对应身体部位，主管穿戴承重，主人公可轻扶调整。",
            "从上镜真实状态切入，若主人公仍轻扶，在特写开头撤手；主管已穿戴并露出反应。"),
    "access": ("固定商品在原安装处，主人公占用使用位，主管尚未使用。",
               "商品始终留在同一安装处，主人公让开，主管已接替其使用位置。",
               "主管沿用上镜已接替的使用位置体验；固定商品和支撑位置不动。"),
}


def _transfer(plan):
    mode = getattr(plan, "transfer_mode", "") or ("access" if plan.mode == "stationary" else "pass")
    opening, ending, next_opening = TRANSFER_PHASES[mode]
    return {"transfer_mode": mode, "opening_phase": opening, "ending_phase": ending, "next_opening": next_opening}


def _office_shot_phases(plan, index):
    phases = _transfer(plan)
    if index == 25:
        preparation = getattr(plan, "handoff_preparation", "")
        if not preparation and phases["transfer_mode"] == "pass":
            preparation = getattr(plan, "handoff_state", "")
        return {"preparation_during_rebuke": preparation, "required_cut_before_transfer": phases["opening_phase"]}
    if index == 26:
        return {"transfer_mode": phases["transfer_mode"], "opening_phase": phases["opening_phase"],
                "ending_phase": phases["ending_phase"]}
    if index == 27:
        return {"reaction_after_transfer": phases["next_opening"]}
    return {}


def _dance_partner(plan, shot, *, moving=False):
    if shot["id"] in {21, 22, 28}:
        motion = "演员和装置的布臂随风交替摆动。" if moving else ""
        return f"同一街角固定基座上的鼓风充气舞者：{plan.street_anchor}。{motion}画面里只有一位人类舞者。"
    return ""


MATCH_ENTRY_OPENING = ("入点照先留出手的行动空间。目标和它的实际支撑已在画内；做手势的整只手仍在画缘外，"
                       "较宽机位也可以让它自然垂在远离目标的画缘。此刻目标四周清楚，活动手没有抬到目标旁，"
                       "不能用已经对准目标、仅留一条细缝的指尖充当起始状态。若这只手本身必须承重，"
                       "其余手指继续真实握持，仅做动作的手指自然收回，给它留出可辨的伸展行程。"
                       "门口只是地点；门把不进入手的动作路径。")
MATCH_ENTRY_REVIEW = ("第11镜参照原片14帧的第一次接点。首格只有就位的目标和真实支撑，活动手可完全在画外，"
                      "也可自然垂在较远的画缘。接着手从边缘进入、向指定部件伸出，末格确实触及。"
                      "若手同时承重，保留正常握持，能看见活动指从收回状态伸向目标也成立。"
                      "开场手臂已经举到触点旁、全程只挪指尖或悬停，不构成这一次伸手。"
                      "首格手尚在画外无需判断它的左右；从随后进入的手确认实际侧别。"
                      "仅凭最后指尖的指向不能反推它在空中走过的方向；依实际位置变化和真实末落点判断。")
BEFORE_PICKUP = ("这条开场只拍日常干扰和人物反应：镜头沿肩绕到脸，目光在剪出时可落向画外。"
                 "下一镜才拿商品。商品可以一直在画外；若入画仍搁在原位，尚未由手提离支撑或穿戴在身。"
                 "手可开始朝其方向移动，未把这件商品拿起来就能接下一镜。")


def _contact_framing(index):
    if index in (11, 12):
        return ("默认鼻尖朝画面右边缘，从实际触点所在身体侧布置相机；同时容纳脸和真实触点高度。"
                "若触点在身体另一侧或另一只手跨过来，可让肩与脸略错开，用略带正面的侧机位看清真正的接触。"
                "保留演员解剖学左右与原文字方向，不能为近景而移动产品或把手势抬到脸旁。")
    if index == 23:
        return ("保留低机位、应答的脸部表情和方向。若实际触点远离脸，改为腰至头的中近景，"
                "也可在说完整句话后沿手势轻摇到触点。手可从画缘进入，末帧须把真接触拍清。")
    if index == 24:
        return ("承接上镜实际剪出的景别、同一手与触点。首帧可以仍在接触，也可以已经沿原方向刚刚松开、"
                "指尖仍紧邻同一部件。这是同一次释放的相邻相位，手不能已远离或重新伸向它。")
    return ""


def _first_beat(idx, plan=None):
    from light.commercial_post import score_for

    score = score_for(plan)
    start = sum(b.frames for b in recipe.BEATS[:idx - 1]) / recipe.FPS
    # The reprise has its own phase. Legacy cuts landed on the wide shot's
    # first frame; the reference recording has its downbeat just after it.
    anchor = (score.get("reprise_beat_seconds", start) if idx == 28 else
              score.get("first_beat_seconds", score["music_in_frame"] / recipe.FPS))
    period = 60 / score["bpm"]
    phase = (anchor - start) % period
    return 0. if math.isclose(phase, period, abs_tol=1e-8) else phase


def _wardrobe_note(plan, shot):
    sport = getattr(plan, "track_wardrobe", "")
    if not sport:
        return ""
    idx = shot["id"]
    if 12 <= idx <= 17 and _scene(plan, shot) == "track":
        return f"跑道专用运动造型：{sport}。与通勤造型是同一演员；若衣、包、鞋是商品本身，保留这同一件商品。"
    if idx in (11, 18, 23, 24):
        return "主人公穿通勤定妆的衣服，商品保留既定支撑。街头与办公室之间不换装。"
    return ""


def _beat_note(shot, plan=None):
    if shot["id"] not in DANCE:
        return ""
    from light.commercial_post import score_for

    score = score_for(plan)
    bpm = score["bpm"]
    beat = 60 / bpm
    first = _first_beat(shot["id"], plan)
    performance = "随配乐律动" if "first_beat_seconds" in score else " funk律动表演"
    return (f"以{bpm:g} BPM{performance}，剪辑入点后约{first:.2f}秒为重拍，随后每{beat:.3f}秒一拍。"
            "以脚跟、肩膀或手臂的清楚落点演出，动作之间连贯有弹性，响声由后期合成。")


def _fingerprint(*items):
    return _digest(json.dumps(items, sort_keys=True, ensure_ascii=False))[:16]


class WorkplaceChoice(BaseModel):
    location: int | None = Field(ge=1, le=4, description="One photographed location on the contact sheet, or null for a custom location.")
    merchant_requires_specific_site: bool
    empty_of_people_and_featured_goods: bool
    setting_matches: bool
    entry_and_transfer_possible: bool
    reason: str = Field(min_length=1, max_length=420)

    @model_validator(mode="after")
    def require_usable_location(self):
        if self.location is not None and (self.merchant_requires_specific_site or not all((
                self.empty_of_people_and_featured_goods, self.setting_matches, self.entry_and_transfer_possible))):
            raise ValueError("Use a stock location only when the real room visibly satisfies the merchant and the action")
        return self


async def _scout_workplace(ctx, prefix, run, plan, form, has_user_location):
    if (getattr(plan, "score_revision", 1) != 2 or "commercial_auto_staging" not in run or has_user_location
            or plan.mode == "stationary"):
        return None
    rooms = GUIDES / "workplaces"
    catalogue = json.loads((rooms / "sources.json").read_text())
    sheet = (rooms / "contact-sheet.jpg").read_bytes()
    sheet_hash = hashlib.sha256(sheet).hexdigest()[:20]
    directions = {"language": plan.language, "office": plan.settings.office, "street": plan.settings.street,
                  "handoff": plan.handoff, "product": plan.product, "appearance": plan.appearance,
                  "merchant": {key: value for key in ("brief", "notes", "promotion_brief", "reference_copy")
                               if (value := (form or {}).get(key))}}
    key = f"{recipe.REVIEW}:workplace:{_fingerprint(sheet_hash, directions)}"
    decision = (run.get("director_decisions") or {}).get(key)
    if not decision:
        if run.get("wan_cuts") or any(":take:" in item for item in run.get("director_decisions", {})):
            return None  # An existing shoot keeps its already established custom location.
        board = await _reference_asset(ctx, f"commercial-workplace-scout-{sheet_hash}", sheet)
        try:
            result = await reference_quality.ask(
                ctx, "commercial_workplace_scout", WorkplaceChoice, REVIEW_LANGUAGE[plan.language] +
                "为空间连续的工作场次勘景。图为四张独立空景照：左上1，右上2，左下3，右下4。"
                "依真实所见选出与剧本工作环境、临街/楼层、出入口和光源方向、工作区功能最相符的一张。"
                "演员需要能从门口到负责人处，两人有足够空间当面交接。货架和工作工具可以是自然背景，"
                "但不能有人、参考广告耳机、与本品混淆的现成商品、明显标志或用途相矛盾的大型设备。"
                "若商家要特定真实店址、按自己的门店/照片重现，或任何一处都不能满足具体空间与功能，返回null，交定制布景。"
                "仅描述通用地点类型可选符合的空景。允许调整可移动的小摆件，不可假设能挪门窗、改楼层、加收银台或转换行业。"
                "陈述画面内看得到的进门路线、主管位置和光线作为理由。", directions, [board], max_tokens=780, timeout=55)
        except RemakeStopped:
            # Scouting an existing empty set is optional. The ordinary custom-location request retains its original idempotency key.
            return None
        decision = await _record(ctx, prefix, run, 25, key, result)
    chosen = WorkplaceChoice.model_validate(decision["report"]).location
    if chosen is None:
        return None
    room = catalogue[chosen - 1]
    photo = (rooms / room["file"]).read_bytes()
    if hashlib.sha256(photo).hexdigest() != room["sha256"]:
        raise RuntimeError("The bundled location photograph differs from its reviewed source.")
    return await _reference_asset(ctx, f"commercial-workplace-{room['sha256'][:20]}", photo)


async def _cast_and_sets(ctx, prefix, plan, run=None, *, form=None):
    from light.commercial_product_reference import filming_inputs
    from light.remake_case import reference_copy

    run = run if run is not None else {}
    if form and _measure_carry(run, plan):
        run["_carry_requirements"] = {key: form[key] for key in ("brief", "notes", "promotion_brief") if form.get(key)}
        if words := reference_copy(form):
            run["_carry_requirements"]["reference_copy"] = words
    product, clean, actor, setting = await filming_inputs(ctx, plan, prefix=prefix, run=run)
    clean = list(dict.fromkeys(clean))  # Several source views can support the same verified studio photograph.
    setting_scene = getattr(plan, "setting_scene", "")
    wardrobe = (f"随身固定方式是：{plan.carry}。只在该描述确有袋、背带或卡位时穿上指定的空载携具，"
                "按照演员自身的左右、高度复现。手持商品就不另造袋，本张定装不展示商品。"
                if plan.mode in {"carry", "wear"} else "")
    before = (f"该演员尚未使用或接到这款商品：{plan.product}。保持必要的穿戴或手持位置可用，"
              "不搭配另一个同类别商品、其他颜色的替代款或会叠穿冲突的配件。"
              if plan.mode in {"wear", "carry"} else "")
    briefs = {
        "protagonist": (f"选角定装照。主人公独自全身偏三分之二侧面站立，脸、双手、鞋清晰，"
                        f"正脸局部细节可辨。{plan.protagonist}。{wardrobe}{before}没有商品或别的演员。"
                        "若附人物图只取真实五官、肤色、发型、体态。使用素色背景和自然站姿，原图的场地和手势不进入定妆。", actor),
        "authority": (f"第二位演员，单人三分之二侧面全身定装照，脸、双手、鞋和工作身份标记清楚。"
                      f"{plan.authority}。{before}没有商品或其他演员。", []),
    }
    for scene, kind in PROFILES.items():
        anchor = (f"街角可律动的地标：{plan.street_anchor}。固定基座和街对面相机位置，"
                  "留出旁边一个成年人全身跳舞的空间，画面左右有清楚可辨的非对称地标。") if scene == "street" else ""
        installed = plan.mode == "stationary" and scene in {"home", "office"}
        place = setting if scene == setting_scene else []
        dressing = (f"空场广角无人，最后{len(clean)}张是无场景的真实商品外形卡。按实际尺寸与安装方式保留同一件商品。"
                    if installed else "空景广角，没人和商品。")
        briefs[scene] = (f"实景选景底片，单一地点 {kind}。{getattr(plan.settings, scene)}。{anchor}"
                         f"{dressing}若附用户场地图，仅当匹配以上具体地点时保留其建筑与格局。",
                         [*place, *clean] if installed else place)

    async def generate(name):
        if name == "office":
            user_workplace = setting_scene == "office" and bool(setting or getattr(plan, "setting_asset", ""))
            location = await _scout_workplace(ctx, prefix, run, plan, form, user_workplace)
            if location:
                return name, location
        if name == "protagonist" and (aligned := run.get("commercial_carry_calibration")):
            return name, aligned["cast"]
        prompt, refs = briefs[name]
        identity = _fingerprint(name, prompt, refs)
        return name, await _still(ctx, prefix, f"commercial-{name}-{identity}", STYLE + prompt, refs,
                                  size=WIDE, quality="medium", language=plan.language)

    shared_installation = plan.mode == "stationary"
    base_room = "office" if setting and setting_scene == "office" else "home"
    reverse_room = "home" if base_room == "office" else "office"
    names = [key for key in briefs if not (key == reverse_room and shared_installation)]
    identities = dict(await _parallel(names, generate, concurrency=3))
    if shared_installation:
        description, extras = briefs[reverse_room]
        briefs[reverse_room] = (description + "以图1已建立的同一安装场地为实景，换拍本镜要求的角度，"
                                "保留门窗、墙体、固定安装位和路线。", [identities[base_room], *extras])
        _, identities[reverse_room] = await generate(reverse_room)
    else:
        await _verify_unworn_cast(ctx, prefix, run, plan, identities, product)
        identities["carried_actor"], identities["carry_at_feet"], plan = await _carried_actor(
            ctx, prefix, run, plan, identities["protagonist"], clean, product)
        identities["contact_actor"] = await _freeze_contact(ctx, prefix, run, plan, identities["carried_actor"], product)
        await _resting_costumes(ctx, prefix, run, plan, identities, product, clean, role="carried_actor")
    await _running_cast(ctx, prefix, run, plan, identities, product)
    await _resting_costumes(ctx, prefix, run, plan, identities, product, clean, role="track_actor")
    identities["product_cards"] = clean
    return identities, product, plan


def _scene(plan, shot):
    if plan.mode == "stationary" and shot["id"] == 15:
        return "home"
    return "street" if shot["scene"] == "return" else shot["scene"]


def _setup_refs(plan, shot, library, material, cuts, guide, first=None, *, reverse=False, adjacent=(), ending=False):
    idx = shot["id"]
    scene = _scene(plan, shot)
    refs = []

    def add(role, asset):
        if asset and asset not in (item[1] for item in refs):
            refs.append((role, asset))

    if idx == 17 and ending and library.get("track_contact"):
        add("画内人物和真实商品的主参考：跑道衣着、手指确已碰到的具体部件、人体侧别和支撑都从这张已核准照片重现。"
            "按同一实际部件转到规定的正面中景，而非照抄侧视图的平面横坐标；将棚拍背景换成本段跑道",
            library["track_contact"])
    if first:
        prior_role = ("本镜已核定尾帧，在同一现场倒推至动作尚未开始的时刻" if reverse else
                      "本镜核定首帧，保持人物与地点，推进到要求的动作末相位")
        if idx == 11 and reverse:
            prior_role += "。仅保留取景、目标和支撑位置，把做手势的手撤回画缘以外；若同手承重则收回活动指"
        add(prior_role, first)
    if idx in MATCH_IN:
        pose = ("上镜实际剪出帧，只继承脸和空手手势相对身体的位置、相位与角度。固定商品只在安装地，"
                "不要把它、安装台或柜体复制到外景" if plan.mode == "stationary" else
                "上镜实际剪出帧，精确沿用脸、手、接点、商品的画面位置、大小与朝向，只更换目标场景")
        if idx in (12, 18) and getattr(plan, "track_wardrobe", ""):
            pose = ("上镜实际剪出帧用来精确接同一演员、手、身体相对位置、商品与触点相位；"
                    "切到新场的第一帧已经穿上该场定妆，非商品服装和包按目标场次改变。")
            if plan.mode == "stationary":
                pose += "固定商品留在安装处，现场只继承同一身体手势。"
        if idx == 24:
            pose += "办公室首帧允许沿实拍接点刚刚释放，同一指尖还在同一部件旁。"
        add(pose, cuts[MATCH_IN[idx]]["review_tail"])
    elif idx == 28:
        add("第22镜实际街角，一比一继承相机、街景、地标位置、尺寸、光向，仅将舞者替换成权威人物",
            cuts[22]["review_head"])
        add("权威人物已接手后的实拍，仅继承人物与衣着", cuts[27]["review_tail"])
    elif idx == 9:
        if getattr(plan, "score_revision", 1) == 2 and cuts.get(8):
            add("前一镜真实剪出的商品状态，仅继承它的穿戴/拿持或安装位置。改拍出门扣件的大特写，"
                "不要把上一镜的日常道具、商品或饮品放进包", cuts[8]["review_tail"])
    elif idx == 19:
        add("本镜只拍脚步和裤脚。按通勤定妆原样拍裤子的颜色、材质、剪裁和同一双鞋。参考片仅供低角度机位，"
            "不要沿用参考演员的下装或运动场的衣服。", library["protagonist"])
    elif idx in DEPENDENCIES:
        prior, = DEPENDENCIES[idx]
        add("上镜实拍尾帧，继承演员、商品状态与左右位置，按本镜规定改变景别", cuts[prior]["review_tail"])
    for role, picture in adjacent:
        add(role, picture)
    by_door = plan.mode in {"carry", "wear"} and idx in (10, 11)
    set_role = ("同一房间的光色和材质，人物现已走到出入口，机位对着门口人物；起初陈列商品的台面不入画"
                if by_door else
                "本镜目标实景，只继承布景和光向；按本镜镜位重构视角")
    if not by_door:
        add(set_role, library[scene])
    carried = _wear_reference(shot, library, ending) if scene == shot["scene"] else None
    if carried:
        if idx == 9:
            role = ("出门装备插镜的场记参考。上镜已使用的商品仍保持图中随身位置；只拍手与要合上的现有扣件，"
                    "无须让整个人或商品入画，不要把商品从身上拿来收进行李")
        elif carried == library.get("track_feet"):
            role = "同一跑道定妆的真实鞋子裁图，仅用于贴地脚步镜"
        elif carried in (library.get("contact_actor"), library.get("track_contact")):
            role = "本场已核准的指尖到位定妆，继承该场衣着、真实侧别、商品特征、支撑、落点与手姿；重拍本场机位和环境"
        elif carried in (library.get("resting_track_actor"), library.get("resting_carried_actor")):
            role = "本场已核准的活动手自然放松定妆，继承真实演员、衣着、商品和随身支撑；起始构图中保留尚未做转场手势的状态"
        elif carried == library.get("track_actor"):
            role = "跑道独立全身定妆，衣料、鞋和保留的必要支撑按这张图；固定安装商品留在室内"
        else:
            role = "已核准的实际随身定装：同一演员、衣着、商品、支撑部位及左右位置；只改现场和规定机位"
        add(role, carried)
    elif idx < 27:
        add("主人公定妆，只供实际入画的脸、发型、衣着、鞋、手参考", library["protagonist"])
    if idx in (14, 16, 17) and library.get("track_lower_body"):
        add("同一运动造型的实拍下装和鞋，仅用于同一演员入画的下身，手的开场或动作看姿态图", library["track_lower_body"])
    if by_door:
        add(set_role, library[scene])
    if idx >= 25:
        add("权威人物未接手的基础定妆，仅用于脸和工作服；若前镜已实际戴上或持有商品，承接实拍的穿戴和持有状态",
            library["authority"])
    views = (material[:1] if shot["product_visible"] else []) if carried else material
    for photo in (views if _product_ref_needed(plan, shot) else []):
        add("真实商品形态卡；核对外形、材质和尺寸。裁去原遮挡物留下的空隙不是商品断裂，结合其他真实视角，未知部分不做特写"
            if shot["product_visible"] else
            "商品只在既定穿戴/持有的位置自然露出时供辨认。仅合装备扣件，产品可以始终在画外" if idx == 9 else
            "商品外观备查，只有自然露出的部分才照此还原；允许全程在包里或画外，不主动陈列", photo)
    add("最后的低分辨率灰阶明暗图仅供景别、视点、地平线和人物占比，不沿用图里的物品和人物",
        guide)
    return refs


async def _frames(ctx, prefix, plan, shot, library, material, cuts, guide, correction, run, *,
                  adjacent=(), user_revision=0, office_retake=False, observed_start=None):
    i = shot["id"]

    async def make(ending, previous=None, advice=""):
        roles = _setup_refs(plan, shot, library, library["product_cards"], cuts, guide, previous,
                            reverse=not ending, adjacent=adjacent, ending=ending)
        purpose = "动作已经完成的最后瞬间" if ending else "真实摄影的第一瞬间，保留接下来表演的余地"
        moment = shot["end"] if ending else shot["frame"]
        # A picture of an actor about to move must not be prompted with the
        # subsequent completed movement. The video request owns that action.
        action = ""
        if i == 9:
            moment = ("极近景对着出门装备的扣或拉链，已接近合拢，只剩最后一个短促的闭合动作。"
                      "手已经捏住扣件，装备内容已收拾完，接下来无需放入任何东西。")
        if i in MATCH_IN.values() and not ending:
            action = ("只拍接触以前的状态：活动指尖尚未到达触点，两者之间能看见合理空隙。"
                      "手持商品时其余手指、掌心或另一手仍按实际拿法承重；不要把商品悬停等手指来接。")
            if i == 11:
                action = MATCH_ENTRY_OPENING
            if i == 23:
                action = ("手还没接触标志位置。留出指尖从画缘进入的合理路径，开口应答时先看清表情。"
                          "商品有真实支撑，移动手不必已在起始画面中央。")
        if i in MATCH_IN.values() and ending:
            moment += f"只在尾帧明确碰到这个具体位置：{plan.contact}。姿态可稳定地接入下一场。"
            if i == 17 and library.get("track_contact"):
                moment += ("跑者已减速站稳，优先照已核准跑道触点拍同一演员。"
                           "用商品本身真实形状、可见纹理与人体承重位置认准手下的部件，以原定装为人像，"
                           "换成同一跑道背景、正面中景和本场自然光，保留一次动作的末相位。")
            if plan.mode == "stationary" and not shot["product_visible"]:
                moment = f"空手在和原接点相同的身体高度与朝向停住：{plan.contact}。本场不出现安装处或商品。"
        if i == 25 and not ending:
            action = "权威人物仍坐在远端，主人公刚从门口进入。"
            if plan.mode != "stationary":
                action += f"商品还在原先的携带位置：{plan.carry}。"
        if i == 25 and ending:
            action = ("摄影机跟入工作区后停在同一动线的双人位置，权威人物已经站起，主人公已经停步。"
                      "商品或使用位置仍由主人公控制，对方尚未接手。")
            phases = _office_shot_phases(plan, i)
            action += f"本镜内只准备到：{phases['preparation_during_rebuke']}。剪出时仍是：{phases['required_cut_before_transfer']}"
        if i == 1 and plan.mode in ("carry", "wear"):
            action += BEFORE_PICKUP
        elif plan.mode == "wear" and i in (2, 3):
            action += ("主人公的实际穿戴部位按未使用商品时的基础定妆：在初次戴上前该处没有同品类替代件；"
                       "若已按本镜相位开始穿戴，只保留待用的这同一件。若对应身体部位本来在画外，保持规定景别。")
        product_state = "" if i == 1 and plan.mode in ("carry", "wear") else _mode(plan, shot['product_visible'])
        text = (f"{STYLE}现在拍{purpose}。{shot['camera']}。构图和可见状态：{moment}。"
                f"本镜全程在此实景：{getattr(plan.settings, _scene(plan, shot))}。场次固定。"
                f"{action}。{_contact_framing(i)}{product_state}"
                f"{_carry_state(plan, shot, ending=ending)}{_wardrobe_note(plan, shot)}{_dance_partner(plan, shot)}"
                "按所选镜位拍，特写只露该部位，人物和道具自然接触，手指数量与关节正确。"
                "画面出现的每个人、衣服、实景及商品以相应彩色参考为准。"
                + " ".join(f"图{n}：{role}。" for n, (role, _) in enumerate(roles, 1)))
        if i in MATCH_IN:
            text += "让换场发生在硬切本身。新场景从第一帧已经完全出现。"
        if i == 26:
            text += "画内只有一份商品或一个固定使用位置。"
            if state := getattr(plan, "handoff_state", ""):
                text += (f"交接规定的物理状态为：{state}。从第一张开始就按其中的开合、安装状态和附件位置摆好，"
                         "两张图中只改变控制商品或使用位置的人，不另拍开合或拆附件。")
            text += _transfer(plan)["ending_phase" if ending else "opening_phase"]
        if i == 27 and not ending:
            text += _transfer(plan)["next_opening"]
        if i == 28:
            text += "结尾实景构图：上一位舞者完全离开，只有工作造型的权威人物全身与原地标共舞。"
        if correction:
            text += f"根据实际审片重拍：{correction}"
        if advice:
            text += f"只修这张图的动作相位和实物支撑：{advice}"
        asset_ids = [asset for _, asset in roles]
        key = _fingerprint(i, ending, text, asset_ids) + (f"-r{user_revision}" if user_revision else "")
        return await _still(ctx, prefix, f"commercial-{i:02}-{key}", text, asset_ids,
                            size=WIDE, quality="medium", language=plan.language)

    first = None
    if office_retake and observed_start:
        key = f"{recipe.REVIEW}:office-opening:{_fingerprint(observed_start)}"
        proof = (run.get("director_decisions") or {}).get(key)
        if not proof:
            result = await reference_quality.ask(
                ctx, "commercial_office_opening", FrameCheck, REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD +
                "图1是上一条实拍真实第0帧，图2为本场实景，后面为商品原素材。判断这一帧本身是否可作为重新开拍的开头："
                "主人公刚入门，商品/随身装备处在指定开场状态，权威在远端尚未起身。商品可以因角度或携带位置不显露。"
                "若人已在交接位置、物已提前准备完或人站错，须拒绝这张实拍起点。",
                {"language": plan.language, "opening": shot["frame"], "carry": plan.carry, "mode": plan.mode},
                [observed_start, library["office"], *material], max_tokens=850, timeout=55)
            proof = await _record(ctx, prefix, run, i, key, result)
        if FrameCheck.model_validate(proof["report"]).ok:
            first = observed_start
    first = first or await make(False)
    if office_retake:
        last = await make(True, first)
        for attempt in range(2):
            key = f"{recipe.REVIEW}:office-pair:{_fingerprint(first, last)}"
            proof = (run.get("director_decisions") or {}).get(key)
            if not proof:
                result = await reference_quality.ask(
                    ctx, "commercial_office_path", EndpointCheck, REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD +
                    "前两张是同一真实工作区的一条连续跟拍的起止状态，之后是商品实图。"
                    "首帧刚入门、权威尚坐；末帧摄影机已跟进，权威站起且主人公已停步。人物不能预演之后才做的动作。"
                    "正确商品或使用权仍在主人公手上，按给出的状态为后续交接准备就绪，不能已经交给对方。"
                    "附件按要求留在画外，只核对明确可见的错处。指出应改的首端或末端，保留已通过的那端。",
                    {"language": plan.language, "opening": shot["frame"], "ending": shot["end"],
                     "carry": plan.carry, **_office_shot_phases(plan, i)},
                    [first, last, *material], max_tokens=900, timeout=55)
                proof = await _record(ctx, prefix, run, i, key, result)
            path = EndpointCheck.model_validate(proof["report"])
            if path.ok:
                return first, last
            if attempt == 0:
                if path.first_correction:
                    first = await make(False, advice=path.first_correction)
                if path.last_correction:
                    last = await make(True, first, path.last_correction)
        raise _rejected(plan, shot, path.reason, " ".join(filter(None, (path.first_correction, path.last_correction))))
    if i not in OUT_POSE:
        return first, None
    last = await make(True, first)
    approved_opening = None
    grounded_opening = None
    located_track_landing = False
    for attempt in range(4):
        contract = "first-match-entry-v1" if i == 11 else "physical-v2"
        key = f"{recipe.REVIEW}:endpoints:{i}:{_fingerprint(first, last)}:{contract}"
        proof = (run.get("director_decisions") or {}).get(key)
        if (getattr(plan, "score_revision", 1) == 2 and proof and not proof["report"]["ok"]
                and proof.get("playback_review") != PLAYBACK_REVIEW):
            # Keep every already approved photograph. A legacy rejection only
            # needs one independent playback-standard assessment of these same pixels.
            key += f":{PLAYBACK_REVIEW}"
            proof = (run.get("director_decisions") or {}).get(key)
        base = FirstMatchStaging if i == 11 else ContactStaging
        schema = (create_model(f"Playback{base.__name__}", __base__=(PlaybackDecision, base))
                  if getattr(plan, "score_revision", 1) == 2 else base)
        if not proof:
            answer = await reference_quality.ask(
                ctx, "commercial_contact_staging", schema,
                REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD + (MATCH_ENTRY_REVIEW if i == 11 else "") +
                "图1首帧、图2尾帧，图3及以后才是用户商品照片。先单独目视图3填reference_item，再单独目视图1/2填filmed_item。"
                "若product_views为0，则无图3，只检查规定的空手姿势和场内物件；无需商品入画时填未提供、same_item=true。"
                "文字剧本不能代替原图；若实拍商品与图3明显不是同类同款，即使动作漂亮也填same_item=false并退回。"
                "检查两张即将用来生成视频的同一场内起止剧照。首张必须是动作之前，末张必须是已到达的相位，"
                "两张看过去应有可见且可信的一次动作。手/物不能已经接触两次，不能悬空、融进对方或换物。"
                "触点镜起始手还没有接触，最后一张指尖须碰到指定位置。第23镜首张允许专拍应答的脸、"
                "指尖由画缘或画外进入，末张可稍宽或已沿动作下摇，末帧必须看清指尖实际接触。"
                "两张剧照之间的手可以走一段自然弧线。只凭末张指尖本身的指向不能断言来路；"
                "先核实首张未触与末张确触的位置，运动的实际方向看后续动态片。"
                "部件的中部、前缘等是可辨认的真实接触面。以接触处指腹的可见宽度和商品在本机位上的曲面透视来核对；"
                "指腹已经覆到这个部位时即成立。不要用指尖像素没压在数学中线上作为退件理由。"
                "若隔着可见空气、只是手指其他指节搭到商品、或离开目标数个指宽落到另一端，仍须退回。"
                "若只动了头或表情而手已在那里，两图不成立。"
                "若carrying_state有指定承重点，首尾必须按这个位置核对。腰套等身体携具不能被桌台当成等价支撑；"
                "核对整幅是否多一件商品，或携具里长出错色小复制品/仿造球而真正的商品还在陈列台上；"
                "看得见的重复与错件都必须退回。门口接点的原陈列面就算可见也已清空。"
                "手持时只需活动指尖在首图尚未落到指定点，其余手指正常握持是正确的。不要为了容易出片建议换支撑物。"
                "第11/12镜优先鼻尖向画右。若唯一差别是整机位成套改到另一侧，可暂留给两镜实拍后核查；"
                "真实左右手、相对身体的触点高度、产品文字绝不镜像，伴随高度或手错误须退回。"
                "固定商品在运动场或街头用空手姿势呼应，同样位置的空手动作成立即可，不要求接触实物。"
                "交接镜严格按transfer_mode和所给opening_phase/ending_phase判断。"
                "pass要求接收者空手至独自承重；fit从主人公实际穿戴转到主管身上，主管已穿戴承重时原主轻扶是正确的。"
                "access交让固定使用位，商品不移动；接收者不用伸手接物。"
                "检查当前角度实际可见的商品面与实图一致。朝下的外表面、因自身、手或台面被挡的附属特征看不见是正常的，"
                "不能因未看见一个本应在背面的结构判错货，更不能要求在另一面硬画出来。"
                "但遮挡不能抹掉它占用的空间：硬质凸起即使在物件背面，放下时也应有相应净空或支撑；"
                "若刚性凸起一侧紧贴平面、只有扁平替代品才能如此平贴，这属于明确的机械矛盾，plausible_support=false。"
                "若有手接的动作，先伸一手、最后改成另一手且无一次连贯承接的依据，consistent_hands=false。"
                "在画外或小到无法分辨的附件只有出现其他明确可见矛盾时才判错；主商品和控制权必须读得清。"
                "接受镜头视角及人体的正常透视。"
                "如有问题，first_correction/last_correction只填写须重画的一端，另一端留空；用指定成片语言说明。",
                {"shot": i, "scene": shot["scene"], "language": plan.language, "mode": plan.mode,
                 "product_views": len(material) if shot["product_visible"] else 0, "opening": shot["frame"],
                 "ending": shot["end"], "action": shot["action"], "carrying_state": _carry_state(plan, shot),
                 **({"handoff": plan.handoff, **_transfer(plan)} if i == 26 else {"contact": plan.contact})},
                [first, last, *(material if shot["product_visible"] else [])], max_tokens=1450 if i == 11 else 1000, timeout=65)
            proof = await _record(ctx, prefix, run, i, key, {**answer, "playback_review": PLAYBACK_REVIEW})
        check = schema.model_validate(proof["report"])
        if check.ok:
            return first, last
        if not check.first_correction and check.last_correction:
            approved_opening = first
            if (getattr(plan, "score_revision", 1) == 2 and i in MATCH_IN.values() and check.same_item
                    and check.plausible_support and check.consistent_hands):
                if grounded_opening == first:
                    # Two independent pairs accepted this exact start. Don't
                    # keep redrawing an uncertain still endpoint. Film once
                    # from the good start and judge the real moving hand.
                    return first, None
                grounded_opening = first
        if attempt < 2:
            if check.first_correction:
                first = await make(False, None if check.last_correction else last, check.first_correction)
            if check.last_correction:
                targeted = None
                if (i == 17 and not located_track_landing and check.same_item
                        and check.plausible_support and check.consistent_hands):
                    located_track_landing = True
                    targeted = await _retarget_track_landing(ctx, prefix, run, plan, shot, last, library,
                                                             material, check.last_correction, user_revision)
                last = targeted or await make(True, first, check.last_correction)
        elif attempt == 2:
            # Both individually edited frames may have inherited incompatible
            # occlusion or blocking. Give the same action one clean setup, then
            # inspect that actual new pair independently before buying motion.
            staging = ("在原场景换成更容易实拍的摆位，保留规定景别、相机一侧、光向和角色身份。"
                       f"沿用实际承重方式。{_carry_state(plan, shot)}可遮挡的小附件留在同一真实位置，"
                       "可两端始终都在同一画缘外，不要新增配件或在被挡的一面绘制看不见的特征。")
            if i == 26:
                phases = _transfer(plan)
                staging += (f"仍是完整交接的双人中景，两人略错开，让视线看得到同一真实主体。{phases['opening_phase']}"
                            f"{phases['ending_phase']}固定相机与所有不参与的物件，真实动作在清楚的运动区域内。"
                            "避开不参与的静物；用同一摄影底片只更新参与这次交接的真实区域。")
            else:
                staging += ("在动作轴同侧安排指尖接近可见的目标，保留清楚的负空间和支撑。"
                            "首图尚未接触，末图真正到位。两张之间动作不靠相机推拉冒充。")
            first = await make(False, advice=staging)
            last = await make(True, first, staging)
            if i == 26:
                try:
                    last = await _lock_handoff_background(ctx, prefix, run, plan, first, last)
                except ReviewRejected:
                    if not approved_opening:
                        raise
                    break
    if approved_opening:
        key = f"{recipe.REVIEW}:single-opening:{i}:{_fingerprint(approved_opening, material)}"
        proof = (run.get("director_decisions") or {}).get(key)
        if not proof:
            result = await reference_quality.ask(
                ctx, "commercial_single_opening", FrameCheck, REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD +
                (MATCH_ENTRY_OPENING if i == 11 else "") +
                "现在只评第一张真实开场图；后续原图仅用于核对商品。确认本场正确，动作确实尚未完成，演员、物品和"
                "开合状态能开始一次自然接触或交接，商品有真实支撑。若仅依这张首帧无法可信地开拍或明显错货，"
                "必须退回。小附件不可见与实际支撑物理矛盾要分别判断。",
                {"language": plan.language, "mode": plan.mode, "shot": shot, "carrying_state": _carry_state(plan, shot),
                 **({"handoff": plan.handoff, **_transfer(plan)} if i == 26 else {"contact": plan.contact})},
                [approved_opening, *(material if shot["product_visible"] else [])], max_tokens=900, timeout=60)
            proof = await _record(ctx, prefix, run, i, key, result)
        opening = FrameCheck.model_validate(proof["report"])
        if opening.ok:
            return approved_opening, None
        raise _rejected(plan, shot, opening.reason, opening.correction)
    raise _rejected(plan, shot, check.reason, " ".join(filter(None, (check.first_correction, check.last_correction))))


def shot_request(prefix, plan, shot, first, last, correction, take, *, user_revision=0):
    idx, line = shot["id"], shot["spoken_line"]
    language = {"zh": "普通话", "vi": "tiếng Việt", "en": "English"}[plan.language]
    timing = ("第一帧已经处于接点，马上释放；保持同一演员、手、落点和背景。" if idx in MATCH_IN
              else "在片末抵达所给尾帧，最后约0.25秒保持准确接触姿态。" if idx in OUT_POSE else "")
    if idx == 1 and plan.mode in ("carry", "wear"):
        timing = BEFORE_PICKUP
    if idx == 9:
        timing = "极近景内只拍闭合扣件的一次重音，其他物和身体位置保持；不另拍移走商品的动作。"
    if idx == 11:
        timing = ("目标先留空，活动手从画缘进入并一气落到指定位置。这一次伸手约半秒完成，随后稳定抵住；"
                  "不要把手放在紧挨目标处等候，或把一次干脆的动作摊长至整条素材。手若同时承重，仅活动指从收回状态伸出。")
    if idx == 24:
        timing = "第一帧承接同一手已到位或刚开始松开的瞬间，继续这一次释放；不要停住后重新触碰。"
    if idx == 26:
        phases = _transfer(plan)
        timing = f"一个连续动作完成这次交接。真实首帧要求：{phases['opening_phase']}剪出时：{phases['ending_phase']}"
    if idx == 27:
        timing = _transfer(plan)["next_opening"]
    if idx in OUT_POSE and not last:
        timing += ("从核定首帧出发，把动作的开始、中途和结果都真实拍在同一镜内，清楚到达指定剪出动作，"
                   "末姿势自然维持一拍，原手或承重方式能被看清。")
        timing = timing.replace("在片末抵达所给尾帧，", "")
    if idx == 28:
        timing = "从第一帧权威人物就在原街角跳舞，演员与地标同步，保持固定广角至切出。"
    if idx == 25 and last:
        timing = ("用一个连续背跟机位：从门口跟入，权威在前方起身并质问，主人公停步。"
                  "在所给尾帧到达交接前的真实状态，物或位置仍由主人公控制。不要重复取出或打开。")
    if line:
        speaker = "权威人物" if idx == 25 else "主人公"
        speech = f"只由{speaker}以{language}自然完整说指定台词一次，清楚且口型同步；其他人闭口。"
    else:
        speech = "角色不说话。"
    action = (f"单一真实电影广告镜头，横幅16:9。机位及运动：{shot['camera']}。从首帧状态开始：{shot['frame']}。"
              f"真人动作：{shot['action']}。剪出姿态：{shot['end']}。{timing}{_contact_framing(idx)}{_beat_note(shot, plan)}")
    product_state = "" if idx == 1 and plan.mode in ("carry", "wear") else _mode(plan, shot['product_visible'])
    notes = f"{product_state}{_carry_state(plan, shot)}{_wardrobe_note(plan, shot)}{_dance_partner(plan, shot, moving=True)}"
    finish = ("继承首帧的演员、衣着、道具结构和光向。"
              f"{speech}自然环境音与动作同步，后期另配音乐。无配乐、字幕、字卡、旁白或水印。"
              "同一镜内连续真实运动，无内切、叠化、闪帧、重复动作或物件凭空出现。")
    if correction:
        finish += f"审片重拍要求：{correction[:280]}。"
    if len(action + notes + finish) + len(line or "") + 16 > 2048:
        # The provider counts the verbatim spoken line it appends. Keep all of
        # the movement, edit point, retake and words; the inputs already show
        # the detailed object, wardrobe, carrying state and dance partner.
        compact = ("指定随身支撑：" + plan.carry + "。" if _carry_state(plan, shot) else "") + "保持同一真实商品和布景。"
        available = max(0, 2048 - len(action + finish) - len(line or "") - 16)
        notes = compact[:available]
    prompt = action + notes + finish
    inputs = [vp.VideoInputRef(asset_id=first, role="first_frame")]
    if last:
        inputs.append(vp.VideoInputRef(asset_id=last, role="last_frame"))
    return vp.VideoGenerateArgs(action="submit", model=MODEL, resolution="768p", ratio="adaptive",
                                duration=max(5, math.ceil(shot["frames"] / recipe.FPS)), generate_audio=True,
                                watermark=False, shot=idx, input_assets=inputs, spoken_line=line or None,
                                no_speech=not line, prompt=prompt,
                                idempotency_key=f"{prefix}:shot{idx}:c{_fingerprint(first, last, correction)}:{take}"
                                + (f":revision{user_revision}" if user_revision else ""))


def _motion_deltas(frames):
    thumbs = [picture.resize((80, 45)).convert("RGB") for picture in frames]
    return [sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 for a, b in zip(thumbs, thumbs[1:])]


def _windows(shot, total, deltas, speech=None, plan=None):
    """Keep editorial duration fixed, selecting motion within a longer provider take."""
    target, idx = shot["frames"], shot["id"]
    if total < target * .78 or total < 2:
        return []
    size = min(target, total)
    if idx in OUT_POSE and idx not in (23,):
        sizes = {size, min(total, math.ceil(size * 1.45))}
    else:
        sizes = {size}
    ranges = []
    if getattr(plan, "score_revision", 1) == 2:
        if idx == 1 and plan.mode in ("carry", "wear"):
            # A longer take may begin a pickup after the reaction. Keep its
            # earlier moving head as an alternative with modest retiming.
            ranges.extend((0, min(size, math.ceil(target * pace))) for pace in (1., .9, .8))
        elif idx == 9:
            # If only the last zipper movement is clean, let the editor see
            # those frames independently from the packing action earlier on.
            ranges.extend([(total - size, size), (max(0, total - size - size // 2), size)])
        elif idx == 12:
            # The action must start on the held touch, which can last several
            # frames. Offer a later opening in that hold; the editor checks its
            # actual first image against the previous cut before selecting it.
            sizes.update(min(total, round(size * factor)) for factor in (1.4, 1.8))
            reach = min(total, round(size * 1.5))
            ranges.append((min(size // 2, total - reach), reach))
        elif idx == 16:
            # A native high wide can turn into a close-up later in the take.
            # Don't let the dissolve's high pixel energy hide the clean wide.
            ranges.extend([(0, size), (min(total - size, size // 2), size)])
    if speech:
        cues, left, right = speech
        start, stop = math.floor(cues[0]["from_sec"] * recipe.FPS), math.ceil(cues[-1]["to_sec"] * recipe.FPS)
        length = min(total, max(size, stop - start + 2))
        lo, hi = max(0, math.ceil(left * recipe.FPS), stop - length), min(total - length, math.floor(right * recipe.FPS) - length, start)
        if length <= target * 1.28 and lo <= hi:
            ranges.append((hi if idx in OUT_POSE else lo, length))
    if idx in (1, 25, 27) and total <= target * 1.23:
        # The full arc of an orbit, confrontation or facial reaction can span
        # both ends of these long takes. Let the editor inspect it alongside
        # the native-speed excerpts instead of always cutting off one phase.
        ranges.append((0, total))
    for count in sorted(sizes):
        last_start = total - count
        if idx in MATCH_IN or idx == 28:
            starts = [0]
        elif idx in OUT_POSE and idx != 26:
            starts = [last_start, max(0, last_start - 4)]
        elif idx == 26:
            # The whole physical transfer has to fit into a short beat. Keep an alternative
            # covering the entire source, subject to actual continuity / performance review.
            starts = [0, last_start // 2, last_start]
            if total <= target * 2.05:
                ranges.append((0, total))
        elif last_start:
            # In each third pick the most active interval, without rewarding a single flash.
            energy = [0.]
            for delta in deltas:
                energy.append(energy[-1] + min(delta, 18.))
            starts = []
            if idx in DANCE:
                from light.commercial_post import score_for
                period = recipe.FPS * 60 / score_for(plan)["bpm"]
                first_onset = _first_beat(idx, plan) * recipe.FPS
                onsets = [round(first_onset + beat * period) for beat in range(count)]
                onsets = [frame for frame in onsets if frame < count - 1]

            def energy_at(s):
                motion = (energy[s + count - 1] - energy[s]) / (count - 1)
                if idx in DANCE and onsets:
                    # Score accelerated real movement at musical accents, so cutting
                    # into a longer take does not discard the requested phase.
                    accents = [max(deltas[s + max(0, t - 1):s + min(count - 1, t + 2)]) for t in onsets]
                    motion += .45 * sum(min(delta, 26.) for delta in accents) / len(accents)
                return motion

            for section in range(3):
                a, b = last_start * section // 3, last_start * (section + 1) // 3
                starts.append(max(range(a, b + 1), key=energy_at))
        else:
            starts = [0]
        ranges.extend((start, count) for start in starts)
    return list(dict.fromkeys(ranges))[:4]


def _jpeg(image, quality=86):
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=quality)
    return stream.getvalue()


def _picks(start, count):
    return [start + round((count - 1) * fraction / 4) for fraction in range(5)]


def _board(frames, ranges, number):
    width, height = REVIEW_SIZE
    board = Image.new("RGB", (width * 5, (height + 20) * len(ranges)), "#17191b")
    draw = ImageDraw.Draw(board)
    for row, (start, count) in enumerate(ranges):
        y = (height + 20) * row
        for column, frame in enumerate(_picks(start, count)):
            left = column * width
            board.paste(frames[frame], (left, y + 20))
            draw.text((left + 6, y + 3), f"SHOT {number}   ROW {row+1}   FRAME {frame}", fill="white")
    return _jpeg(board)


async def _actual_frame(ctx, source, filename, index):
    data = await ff("-threads", "1", "-i", filename, "-vf",
                    f"fps={recipe.FPS},select=eq(n\\,{index}),scale=1024:-2", "-frames:v", "1",
                    "-an", "-threads:v", "1", "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "-")
    if not data:
        raise RemakeStopped("实拍接点没有可用帧。", vi="Không đọc được khung hình tại điểm nối đã quay.",
                            en="The frame at the photographed cut point could not be read.")
    return await _reference_asset(ctx, f"commercial-frame-{source}-{index}", data)


def _pose_distances(frames, intended):
    """Compare overall pose and the parts of the image that changed most, e.g. the touching hand."""
    thumbnails = [frame.resize((64, 36)).convert("RGB") for frame in frames]
    targets = [thumbnails[-1]]
    if intended is not None:
        targets.append(intended.convert("RGB").resize((64, 36)))

    def difference(image, target):
        blocks = ImageChops.difference(image, target).convert("L").resize((8, 4), Image.Resampling.BOX)
        errors = sorted(blocks.tobytes())
        return sum(errors) / len(errors) + sum(errors[-5:]) / 5

    # The delivered end image is usually aligned to the take. The authored end
    # also supplies a target when the generator briefly hits it before deviating.
    return [min(difference(frame, target) for target in targets) for frame in thumbnails]


def _arrival_windows(frames, shot, deltas, authored_end, preferred):
    """Find the actual arrival at the gesture even when a long motionless hold follows it."""
    count = min(len(frames), shot["frames"])
    if not preferred or count < 2:
        return []
    positions = _pose_distances(frames, authored_end)
    still_limit = max(4., sorted(positions)[round((len(positions) - 1) * .85)] * .45)
    jumps = {i + 1 for i, delta in enumerate(deltas)
             if delta >= max(27, 3.5 * median((deltas[max(0, i - 3):i] + deltas[i + 1:i + 4]) or [0]))}
    sizes = sorted({n for _, n in preferred})
    options = []
    for n in sizes:
        for start in range(len(frames) - n + 1):
            end = start + n - 1
            if positions[end] > still_limit or any(start < j <= end for j in jumps):
                continue
            move = deltas[start:end]
            activity = sum(move) / len(move)
            if activity < .32:
                continue
            approach = max(0., max(positions[start:start + max(1, n // 2)]) - positions[end])
            settling = sum(move[-max(1, n // 6):]) / max(1, n // 6)
            score = .6 * min(activity, 28.) + .5 * approach - .35 * positions[end] - .14 * min(settling, 28.)
            options.append((score, start, n))
    chosen = []
    for _, start, n in sorted(options, reverse=True):
        if all(abs((start + n) - (s + k)) >= max(3, count // 3) or abs(start - s) >= count // 2
               for s, k in chosen):
            chosen.append((start, n))
        if len(chosen) == 4:
            break
    return chosen


def _physical_options(frames, shot, speech, authored_end=None, single_start=False, plan=None):
    deltas = _motion_deltas(frames)
    options = _windows(shot, len(frames), deltas, speech, plan)
    if shot["id"] in OUT_POSE and options:
        arrivals = _arrival_windows(frames, shot, deltas, authored_end, options)
        # Also retain the dialogue-aligned option and the complete physical
        # transfer, so a visually similar end pose never erases their start.
        pinned = options[:1] if speech or shot["id"] == 26 else []
        if single_start and not pinned:
            middle = _windows({**shot, "id": 0}, len(frames), deltas, plan=plan)
            pinned = middle[1:2]
        if shot["id"] == 11 and len(frames) > shot["frames"]:
            # A subtle camera move continues after the fingertip lands. Whole-image
            # similarity to the final hold alone can miss the actual hand entrance.
            # Include the burst of real movement and the approach immediately before it.
            energy = [sum(deltas[max(0, i - 2):i + 3]) / len(deltas[max(0, i - 2):i + 3])
                      for i in range(len(deltas))]
            peak = max(range(len(energy)), key=energy.__getitem__)
            short = shot["frames"]
            lengths = (short, math.ceil(short * 1.45), min(short * 2, len(frames)))
            pinned = []
            for length in lengths:
                if length <= len(frames):
                    start = min(max(0, peak - round(length * .27)), len(frames) - length)
                    pinned.append((start, length))
        options = list(dict.fromkeys([*pinned, *arrivals, *options]))
    return [(start, count) for start, count in options
            if sum(deltas[start:start + count - 1]) >= max(2., (count - 1) * .32)
            and internal_jump(frames[start:start + count]) is None][:4]


def _performance_board(frames, shot, start, count, *, row=1):
    w, h = REVIEW_SIZE
    slate = Image.new("RGB", (w * 3, (h + 20) * 3), "#222")
    draw = ImageDraw.Draw(slate)
    positions = [start + round((count - 1) * j / 8) for j in range(9)]
    for j, idx in enumerate(positions):
        x, y = j % 3 * w, j // 3 * (h + 20)
        slate.paste(frames[idx], (x, y + 20))
        draw.text((x + 6, y + 3), f"SHOT {shot['id']}   ROW {row}   FRAME {idx}", fill="white")
    return _jpeg(slate), positions


async def _single_performance(ctx, plan, shot, first, source, frames, start, count, material):
    picture, positions = _performance_board(frames, shot, start, count)
    evidence = await _reference_asset(ctx, f"commercial-single-performance-{source}-{start}-{count}", picture)
    answer = await reference_quality.ask(
        ctx, "commercial_single_performance", Enacted, REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD +
        (MATCH_ENTRY_REVIEW if shot["id"] == 11 else "") +
        "独立复查真实成片区间。图1是九个实拍帧（逐行从左向右，左上是实际剪入、右下是实际剪出）。"
        "图2是开机参考，之后才是原商品。必须从实拍图分别描述opening、middle、ending，"
        "在同一次动作中真的完成接触或交接，最后姿态清楚、手与支撑合理、货品与原图相符。"
        + ("特别看左上实际首帧按transfer_mode核对。pass要有可见空隙，接收者第一帧已经抓住商品即使主人公还托底，"
           "也属于从动作中段开始。fit首格商品仍由原主穿戴，末格已戴于主管，原主可以轻扶新位置。"
           "access首格固定使用位仍归原主，末格主管到位，设备全程保持安装状态。已经进入动作中段则before_contact=false。"
           if shot["id"] == 26 else
           "本镜只拍主人公的手势，先有可见空隙，再移到同一可见的指定点；商品由其余手指正常握持仍合理。") +
        "若整个动作只见开始接近、已提前松手且落点始终无法确认、明显换手跳位、物品穿透/漂浮、换商品，则须退回。"
        "固定安装商品在外景仅匹配同一空手姿势。不要因为导演意图或已核定首图而批准未在实拍画面出现的结果。",
        {"language": plan.language, "shot": shot, "mode": plan.mode, "actual_source_frames": positions,
         "carrying_state": _carry_state(plan, shot),
         **({"handoff": plan.handoff, **_transfer(plan)} if shot["id"] == 26 else {"contact": plan.contact})},
        [evidence, first, *(material if shot["product_visible"] else [])], max_tokens=1350, timeout=65)
    return {**answer, "board": evidence, "frames": positions}


class MatchPlayback(FrameCheck):
    clear_first: bool
    seen_entering: bool
    continuous_reach: bool
    natural_timing: bool
    arrived_at_cut: bool
    first_entry: int | None = Field(ge=0, le=13)
    first_contact: int | None = Field(ge=1, le=13)
    observed_path: str = Field(min_length=1, max_length=420)

    @model_validator(mode="after")
    def require_photographed_travel(self):
        if self.ok and (not all((self.clear_first, self.seen_entering, self.continuous_reach,
                                 self.natural_timing, self.arrived_at_cut)) or self.first_entry is None
                        or self.first_contact is None or self.first_entry >= self.first_contact):
            raise ValueError("The rendered 14-frame edit must retain the entrance, intervening motion and contact.")
        return self


async def _match_playback(ctx, folder, plan, shot, source, window, material):
    start, count = window
    cut = {"source": source, "shot": 11, "start": start, "frames": count, "output_frames": shot["frames"],
           "speed": count / shot["frames"]}
    key = _fingerprint(source, start, count, "first-match-playback-v1")
    movie = await commercial_frame_audit._edited_cut(ctx, folder, cut, key)
    frames = await commercial_frame_audit._frames(movie, 0, shot["frames"] - 1, small=True, language=plan.language)
    sheet, head, tail = await asyncio.gather(
        _reference_asset(ctx, f"commercial-first-match-playback-{key}", commercial_frame_audit._sheet(frames, 0)),
        _actual_frame(ctx, f"first-match-output-{key}", movie, 0),
        _actual_frame(ctx, f"first-match-output-{key}", movie, shot["frames"] - 1))
    answer = await reference_quality.ask(
        ctx, "commercial_first_match_playback", MatchPlayback, REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD + MATCH_ENTRY_REVIEW +
        "图1是最终实际输出的14个相邻帧，按左到右、上到下，每格1/24秒。图2、3分别放大同一成片的首、尾帧，"
        "用来读清指尖、物件高度及接触部件，其余图是原商品。"
        "逐格记下手第一次从画缘进入、或开始伸展的相对帧号及第一次确实触到目标的相对帧号。"
        "必须在中间不同帧看清手走过的路径，末格接触到位。若突然从画外跳在商品上、切入手已悬在目标旁、"
        "手/商品变形、只擦过别的部件、或表演明显抽搐和不自然加速，必须退回。实际节奏短而干脆是合理的。"
        "在observed_path用帧号描述可见的入点、中途、到位；证据不足的帧号填null。",
        {"language": plan.language, "target": plan.contact, "carry": plan.carry, "mode": plan.mode,
         "output_fps": recipe.FPS, "frame_numbers": list(range(shot["frames"]))},
        [sheet, head, tail, *(material if shot["product_visible"] else [])], max_tokens=1200, timeout=60)
    return {**answer, "board": sheet, "head": head, "tail": tail, "window": commercial_frame_audit.window(cut)}


def _finish_entry_windows(start, count, total, target, verdict):
    """Try at most two continuous alternate intervals from the same take after a verified entrance."""
    if not (verdict.clear_first and verdict.seen_entering and verdict.continuous_reach
            and verdict.natural_timing and not verdict.arrived_at_cut):
        return []
    longest = min(total, target * 2)
    later = min(start + max(1, target // 4), total - longest)
    choices = [(start, min(total - start, longest)), (later, longest)]
    return [span for span in dict.fromkeys(choices) if span != (start, count)]


def _dialogue(cut, transcript, verified):
    left = cut["start"] / recipe.FPS
    right = (cut["start"] + cut["frames"]) / recipe.FPS
    if verified:
        cues, clean_start, clean_end = verified
        if (clean_start <= left + .001 and clean_end + .001 >= right
                and cues[0]["from_sec"] >= left - .001 and cues[-1]["to_sec"] <= right + .001):
            return {"cues": cues}
    spans = [(max(left, token["from_sec"]), min(right, token["to_sec"]))
             for token in (transcript.get("words") or transcript.get("sentences") or [])
             if token.get("text") and token.get("from_sec") is not None and token.get("to_sec") is not None
             and min(right, token["to_sec"]) > max(left, token["from_sec"])]
    return {"cues": [], **({"speech_range": [min(a for a, _ in spans), max(b for _, b in spans)]} if spans else {})}


async def _inspect(ctx, folder, plan, shot, source, inputs, library, material, cuts):
    from light.soda_story import _transcript
    from video.media_probe import ensure_meta

    idx, line = shot["id"], shot["spoken_line"]
    filename = folder / f"{source}.mp4"
    await product_story._download(ctx, source, filename)
    meta, raw = await asyncio.gather(
        ensure_meta(source, user_id=ctx.user_id),
        ff("-threads", "1", "-i", filename, "-vf", f"fps={recipe.FPS},scale={REVIEW_SIZE[0]}:{REVIEW_SIZE[1]}",
           "-an", "-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"),
    )
    if not meta:
        raise RemakeStopped("镜头媒体信息尚未就绪。", vi="Chưa có thông tin kỹ thuật của cảnh quay.",
                            en="The technical details of the filmed shot are not ready.")
    width, height = REVIEW_SIZE
    stride = width * height * 3
    if not raw or len(raw) % stride:
        raise RemakeStopped("实际镜头无法完整逐帧解码。", vi="Chưa giải mã được tất cả khung hình của cảnh quay.",
                            en="Some frames in the photographed shot could not be decoded.")
    frames = [Image.frombytes("RGB", REVIEW_SIZE, raw[offset:offset + stride]) for offset in range(0, len(raw), stride)]
    del raw
    transcript = await _transcript(ctx, source, language=plan.language) if line and meta.get("has_audio") else {}
    speech = speech_window(line, transcript, len(frames) / recipe.FPS) if line else None
    intended = None
    if idx in OUT_POSE and inputs[1]:
        reference = folder / f"{inputs[1]}.img"
        await product_story._download(ctx, inputs[1], reference)
        with Image.open(reference) as photo:
            intended = photo.convert("RGB").resize(REVIEW_SIZE)
    single_start = idx in OUT_POSE and not inputs[1]
    options = await asyncio.to_thread(_physical_options, frames, shot, speech, intended, single_start, plan)
    if not options:
        reason, correction = {
            "vi": ("Không tìm được đoạn đủ dài có chuyển động liên tục và không bị cắt đột ngột.",
                   "Giữ nguyên góc máy và bối cảnh, để diễn viên thực hiện một động tác liên tục trong khuôn hình."),
            "en": ("No continuous moving interval long enough for this shot was found.",
                   "Hold one camera setup and have the performer complete one continuous action in frame."),
            "zh": ("镜头没有足够长、持续运动且无跳切的实拍区间。", "保持画面构图与同一场景，真人在镜内完成一次连续动作。"),
        }[plan.language]
        evidence = await _reference_asset(ctx, f"commercial-rejected-motion-{source}",
                                          _board(frames, [(0, len(frames))], idx))
        return {"ok": False, "reason": reason, "correction": correction, "evidence": [evidence],
                "source": source, "observed_frames": len(frames)}
    current_standard = getattr(plan, "score_revision", 1) == 2
    graded_performance = current_standard and idx != 11 and (idx == 26 or single_start)
    performance_boards = []
    if graded_performance:
        # The editor used to choose from five frames, then a separate viewer
        # often found the handoff had already begun. Offer the real nine-frame
        # intervals, including the earlier complete take, to the editor once.
        pictures = [_performance_board(frames, shot, start, count, row=row)
                    for row, (start, count) in enumerate(options, 1)]
        boards = await asyncio.gather(*(
            _reference_asset(ctx, f"commercial-performance-choice-{source}-{start}-{count}-r{row}", picture)
            for row, ((start, count), (picture, _)) in enumerate(zip(options, pictures), 1)))
        performance_boards = [{"board": asset, "frames": positions} for asset, (_, positions) in zip(boards, pictures)]
    else:
        choice_board = await _reference_asset(ctx, f"commercial-choices-{source}-{_fingerprint(options)}",
                                              _board(frames, options, idx))
    refs = [("真人起始图", inputs[0])]
    scene = _scene(plan, shot)
    refs.append(("本场已锁定实景，按分镜角度与景别呈现", library[scene]))
    if inputs[1]:
        refs.append(("设计末状态", inputs[1]))
    if idx in MATCH_IN:
        refs.append(("前镜实际接点，核对第一格姿势", cuts[MATCH_IN[idx]]["review_tail"]))
    if idx == 9 and current_standard and cuts.get(8):
        refs.append(("前镜实际剪出，若能看到商品或相应穿戴部位，只核对持续状态。镜位可以变为只拍扣件", cuts[8]["review_tail"]))
    if idx == 28:
        refs.extend([("第22镜实际街角", cuts[22]["review_head"]), ("现在的舞者", library["authority"])])
    if outfit := _wardrobe_note(plan, shot):
        uniform = (_wear_reference(shot, library, False) or library["protagonist"])
        refs.append((f"本场冻结造型：{outfit}。只核对实际入画衣物与必要携具", uniform))
        if idx in (14, 16, 17) and library.get("track_lower_body"):
            refs.append(("同一演员实际运动下装和鞋的参考，只核对确实入画部分", library["track_lower_body"]))
    first_wear = plan.mode == "wear" and idx <= 3
    refs.extend(("应可见的原始商品" if shot["product_visible"] else "只核对自然入画部分，允许全程在画外的原始商品", photo)
                for photo in material if _product_ref_needed(plan, shot) or first_wear or idx == 9)
    if first_wear:
        whole = await _reference_asset(ctx, f"commercial-first-wear-{source}", _board(frames, [(0, len(frames))], idx))
        refs.extend([("角色未取得商品时的已核准基础定装，仅作同类替代件核对", library["protagonist"]),
                     ("该条原始实拍五帧全景；先独立核对真实穿戴部位与商品数量，再看最后一张选段", whole)])
    if graded_performance:
        refs.extend((f"备选区间{row}的九个实际连续时序采样，逐行左至右", item["board"])
                    for row, item in enumerate(performance_boards, 1))
    else:
        refs.append(("实际时间顺序，每行是一个备选剪辑区间，逐行从左到右", choice_board))
    base = (EnactedSelection if graded_performance else
            {9: DepartureSelection if current_standard else Selection, 11: FirstMatchSelection,
             25: ConfrontationSelection}.get(idx, FirstWearSelection if first_wear else Selection))
    before_pickup = idx == 1 and current_standard and plan.mode in ("wear", "carry")
    if before_pickup:
        base = create_model(f"Opening{base.__name__}", __base__=(BeforePickup, base))
    allowed_rows = create_model(f"Commercial{base.__name__}Of{len(options)}",
                                __base__=(PlaybackDecision, base) if current_standard else base,
                                row=(int, Field(ge=1, le=len(options))))
    proof = await reference_quality.ask(
        ctx, "commercial_take_selection", allowed_rows,
        REVIEW_LANGUAGE[plan.language] + PLAYBACK_STANDARD + (MATCH_ENTRY_REVIEW if idx == 11 else "") +
        ("每件标ROW的独立九格图是一种已付实拍选段。先找有完整主动作的任一段；"
         "仅对此段的真实首、中、末填opening/middle/ending与物理判断。首格须在接触/移交之前，"
         "最后确实到位。若一段从交接中途开始但更早的备选有完整动作，直接选完整那段。"
         if graded_performance else "") +
        "逐行核对同一条动态拍摄素材的备选区间，用row指出动作和商品最清楚的一行，按该行审。"
        + (BEFORE_PICKUP + "独立填写product_before_cut。如果后段拿起商品，但更早的一行已拍到足够清楚的窗边反应，选早的一行。"
           "未入画就填out_of_frame，不要求商品可见。保留范围内已由手托起，即使演员演出了正确表情也要退回。"
           if before_pickup else "") +
        ("第9镜是出门装备关闭的近景，仅评实际剪进的区间。先检查可见穿戴部位和包口内外。"
         "若看见正常应该佩戴/拿持的同一件转移到了包内、桌上，或人物相应的部位明显空了，须退回。"
         "真正辨清同一物件、或上一镜戴着而现在明显露出空部位，才填visibly_displaced_or_duplicated与product_in_wrong_position。"
         "只凭包内一点相近颜色、未成形的布料、难辨的局部，不足以认定是原商品；对照用户图中可辨轮廓、结构或固定标记。"
         "若只见手合扣而商品及该身体部位均在画外，可以通过，不猜包里有什么；商品本身就是装备时，"
         "关闭它自己的扣子且保持原承重可通过。固定商品仍留安装处。"
         if idx == 9 else "") +
        "这是节奏型电视商业片；短插镜只需拍到动作重音或身体局部，非交接镜不要求完整展示长动作。"
        "先确认本场地点。能看到环境时须与片场场景图和locked_set一致；前段日常、运动场、街头和工作区不能提前串场。"
        "有该场冻结造型时也核对实际看得见的衣料和随身物。第12换为跑道装、第18换回通勤装，"
        "这是预定硬切；23到24中间仍穿通勤装。不要要求商品本身在换装时被换掉。"
        "微距插镜只需与地点相容，无须看见完整布景。检查演员、机位景别、运动、物理接触、商品材质造型和位置。"
        + ("第12镜的17帧只需读到承接指触、松手、肩或身体顺势向原方向起动；接下来的第13、14镜才展开跑步。"
           "不用看到全身已经迈完一步，也不要求脸部近景里出现双脚。首格完全缺失上镜手势仍要退回。"
           if idx == 12 else
           "第16镜只取连续的高位大全景，能看清跑道、主人公确实沿它移动即可，不要求完整穿过场地或演完下一镜的减速。"
           "有一行能剪出完整高位跑动就用它，并舍弃素材后面可能叠入的特写。"
           if idx == 16 else "") +
        "carrying_state明确身体承重点时，核对指定的演员侧别、身体高度和携具；不把新桌台视作合格替代。"
        "穿戴商品的拿取/首次使用及给主管戴上时，检查实际演员身上是否原已戴着同类替代件；"
        "两件不同颜色在同处叠穿、在同一部位凭空多出第二份都要退回，真实商品本身原有的分层部件照常保留。"
        "若有first_wear检查，用全程了解前后状态，优先选实际剪入到剪出没有穿帮的一行，回答wear_site："
        "主体何时还在台上或手中，这时画内实际佩戴部位上是什么？"
        "若在保留区间已可见同类替代物或商品叠戴，competing_item=true；不能因拿取动作正确而忽略。"
        "仅存在于未剪入的废片、且不影响保留区间的错误，不据此否决已经拍好的干净选段。"
        "若低机位只见手、桌和商品，人体佩戴部位从未入画，这本身符合拿取特写，不能猜有多一件。"
        "第3镜允许已拿起的同一件商品按剧本完成首次使用。"
        "可见商品必须符合原图；遮在掌下、桌下或背面的结构不要求露出。扎眼的变形、漂浮、跳位、"
        "人脸改变、无动作或与指定场景及动作无关应退回。"
        "不要因未入画细节、摄影透视、光照、正常运动模糊或插镜取景范围而退回。"
        "仅在有match_contact的接点镜检查统一手势。其他镜头按本镜自己的动作验收，正常拿取也应接纳。"
        "接点切出镜必须在所选行最后一格看到手到达指定触点且姿态能够接拍；单首图实拍按剧本末状态核对实际最后一格。"
        "固定安装商品在外景只匹配身体手势，"
        "不能选只看到开始靠近或已经放开的行。"
        + ("本镜只拍进门和权威起身质问。剪出时商品或使用位置仍由主人公控制，核对required_cut_before_transfer，"
           "点明末格谁实际穿戴、承重或使用。在preparation_during_rebuke允许范围里为下一镜作准备。"
           "若仍戴在主人公身上正是正确剪点；若已经戴于主管或交接已成，则提前演了下一镜。不能以尚未交给主管为由退片。"
           if idx == 25 else
           "本镜才是转换控制权的动作。按transfer_mode读首、中、尾：pass要从接收者空手到独自承重；"
           "fit从主人公自己穿着到主管已戴着，原主可轻扶；access是安装位置的操作者交替。"
           if idx == 26 else
           "本镜是主管接手后的反应特写。真实首帧接上已经完成的交接，核对reaction_after_transfer，禁止再次移交。"
           if idx == 27 else "") +
        "第11/12镜优先原片屏幕朝向；仅单纯的成套拍摄侧别变化可暂交终审检验，不能换演员左右手、抬高触点或反转文字。"
        "第24镜的第一格可以是同一指尖已经沿原方向刚开始离开触点；手仍紧邻同一部件，随后继续松开。"
        "出现dance_partner的镜里只有一个真人与一件鼓风街头装置；另一位真人或者静止路牌不能充当会跳舞的装置。"
        "第28镜须能辨认是同一个街角而真人舞者已经换人。"
        "失败只给一条可拍的具体修正，以成片语言写reason及correction。",
        {"shot": shot, "language": plan.language, "mode": plan.mode, "product": plan.product, "appearance": plan.appearance,
         "locked_set": getattr(plan.settings, scene), "carrying_state": _carry_state(plan, shot),
         **({"wardrobe": outfit} if outfit else {}),
         **({"first_wear": True} if first_wear else {}),
         **({"allowed_camera": _contact_framing(idx)} if idx in (23, 24) else {}),
         **({"dance_partner": plan.street_anchor} if idx in {21, 22, 28} else {}),
         **({"match_contact": plan.contact} if idx in MATCH_IN or idx in MATCH_IN.values() else {}),
         **_office_shot_phases(plan, idx),
         "reference_order": [label for label, _ in refs],
         "available_rows": list(range(1, len(options) + 1))}, [asset for _, asset in refs],
        max_tokens=1450 if idx == 11 else 1200, timeout=65,
    )
    proof = {**proof, "playback_review": PLAYBACK_REVIEW}
    choice = allowed_rows.model_validate(proof["report"])
    if idx == 11:
        proof = {**proof, "entry_window_version": 2}
    if not choice.ok:
        return {**proof, **choice.model_dump()}
    start, count = options[choice.row - 1]
    if idx == 11 and shot["frames"] == recipe.BEATS[10].frames:
        playback = await _match_playback(ctx, folder, plan, shot, source, (start, count), material)
        inspected = MatchPlayback.model_validate(playback["report"])
        playbacks = [playback]
        for alternate in _finish_entry_windows(start, count, len(frames), shot["frames"], inspected):
            offset, length = alternate
            if internal_jump(frames[offset:offset + length]) is not None:
                continue
            start, count = alternate
            playback = await _match_playback(ctx, folder, plan, shot, source, alternate, material)
            playbacks.append(playback)
            inspected = MatchPlayback.model_validate(playback["report"])
            if inspected.ok:
                break
        proof = {**proof, "entry_playback": playback, "entry_checks": playbacks, "candidate_row": proof["report"],
                 "report": {**proof["report"], "ok": inspected.ok, "reason": inspected.reason,
                            "correction": inspected.correction, "entry_path": inspected.observed_path,
                            "has_clear_approach": inspected.clear_first and inspected.seen_entering and inspected.continuous_reach,
                            "reached_feature": inspected.arrived_at_cut}}
        if not inspected.ok:
            return {**proof, **inspected.model_dump()}
    if graded_performance:
        observed = {**performance_boards[choice.row - 1], "report": choice.model_dump()}
        proof["performance_checks"] = [observed]
    elif single_start and current_standard and (rendered := proof.get("entry_playback")):
        # The actual rendered 14 frames already show the whole first reach,
        # including the cut. A second nine-frame sample of the same motion
        # cannot add more evidence to its approved entry.
        observed = rendered
        proof["performance_checks"] = [observed]
    elif single_start or idx == 26:
        spans = [(start, count)]
        if idx == 26:
            # The five-frame row can miss that we cut in part-way through a
            # handoff. Inspect the longest earlier interval of the same paid
            # take before asking the cast to perform another take.
            full = min(options, key=lambda window: (window[0], -window[1]))
            if full != (start, count):
                spans.append(full)
        checks = []
        for start, count in spans:
            observed = await _single_performance(ctx, plan, shot, inputs[0], source, frames, start, count, material)
            checks.append(observed)
            check = Enacted.model_validate(observed["report"])
            if check.ok:
                break
        proof = {**proof, "performance_checks": checks}
        if not check.ok:
            return {**proof, **check.model_dump()}
    board = _board(frames, [(start, count)], idx)
    selected_board = _reference_asset(ctx, f"commercial-selected-{source}-{start}-{count}", board)
    if playback := proof.get("entry_playback"):
        evidence, head, tail = await selected_board, playback["head"], playback["tail"]
    else:
        evidence, head, tail = await asyncio.gather(selected_board, _actual_frame(ctx, source, filename, start),
                                                  _actual_frame(ctx, source, filename, start + count - 1))
    (folder / f"{evidence}.jpg").write_bytes(board)
    cut = {"shot": idx, "source": source, "start": start, "frames": count, "output_frames": shot["frames"],
           "speed": count / shot["frames"], "has_audio": bool(meta.get("has_audio")), "review_version": recipe.REVIEW,
           "review_board": evidence, "review_head": head, "review_tail": tail, "cues": []}
    if proof.get("entry_playback"):
        cut["entry_playback"] = proof["entry_playback"]["board"]
    if line:
        cut.update(_dialogue(cut, transcript, speech))
    if single_start or idx == 26:
        cut["motion_performance"] = observed["board"]
        if single_start:
            cut["single_performance"] = observed["board"]
    return {"ok": True, "cut": cut, **proof}


async def _record(ctx, prefix, run, index, key, decision):
    from light.production import save_director_decision

    committed = await save_director_decision(ctx, prefix, index, key, decision)
    run.setdefault("director_decisions", {})[key] = committed
    return committed


def _obsolete_selection(plan, number, decision):
    report = (decision or {}).get("report") or {}
    if decision and "row" in report and decision.get("evidence"):
        if number == 9 and getattr(plan, "score_revision", 1) == 2 and "product_state" not in report:
            return "departure-position"
        if number == 11 and getattr(plan, "score_revision", 1) == 2 and "has_clear_approach" not in report:
            return "first-match-entry"
        if (number == 11 and getattr(plan, "score_revision", 1) == 2 and decision.get("ok") is False
                and report.get("has_clear_approach") is False and report.get("reached_feature") is True
                and decision.get("entry_window_version") != 2):
            return "first-match-window"
    if decision and decision.get("ok") is False and "row" in report and decision.get("evidence"):
        if number == 25 and "control_at_cut" not in report:
            return "before-handoff"
        if number <= 3 and plan.mode == "wear" and "competing_item" not in report:
            return "first-wear"
        if (number != 11 and getattr(plan, "score_revision", 1) == 2 and decision.get("correction")
                and decision.get("playback_review") != PLAYBACK_REVIEW):
            return "normal-playback"
    return None


async def _camera_key_for_source(ctx, prefix, shot, source, setup):
    """Find a completed local receipt for an older take that only recorded its output and camera inputs."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    camera = f"{prefix}:shot{shot}:"
    async with get_db_session() as db:
        receipts = (await db.scalars(select(VideoJob).where(
            VideoJob.user_id == ctx.user_id, VideoJob.session_id == ctx.session_id,
            VideoJob.kind == "segment", VideoJob.status == "completed", VideoJob.output_asset_id == source,
            VideoJob.idempotency_key.startswith(camera, autoescape=True)).limit(5))).all()
    inputs = [asset for asset in setup if asset]
    for row in receipts:
        match = re.fullmatch(r"c[0-9a-f]{16}:(\d+)(?::revision\d+)?(?::retry:[^:]+)?", row.idempotency_key[len(camera):])
        if match and (row.request_data or {}).get("input_asset_ids") == inputs:
            return row.idempotency_key, int(match[1])
    return None


async def _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material, cuts,
                            key, decision, correction, take, user_revision, *, stage=None):
    """Regrade an exact paid take when its old review used another editorial contract."""
    stage = stage or _obsolete_selection(plan, shot["id"], decision)
    if not stage:
        return decision
    side_key = f"{key}:recut-{stage}-v1"
    if saved := (run.get("director_decisions") or {}).get(side_key):
        if _obsolete_selection(plan, shot["id"], saved) == "normal-playback":
            return await _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material, cuts,
                                           side_key, saved, correction, take, user_revision)
        return saved
    photos = decision["evidence"]
    if decision.get("setup"):
        setups = [decision["setup"]]
    else:
        # Historical reviews keep the true camera start first, the set second,
        # and an optional authored end third. Prove which one was sent from the owned job receipt.
        setups = [[photos[0], None]]
        if len(photos) > 2 and (shot["id"] in OUT_POSE or shot["id"] == 25):
            setups.append([photos[0], photos[2]])
    candidates = [(setup, shot_request(prefix, plan, shot, *setup, correction, take,
                                       user_revision=user_revision).idempotency_key) for setup in setups]
    expected_source = decision.get("source") or (decision.get("cut") or {}).get("source")
    if expected_source:
        paid_key = decision.get("paid_request_key") or ""
        if stage == "departure-position" and not paid_key and decision.get("setup"):
            # Older failed take 2/3 receipts did not save their correction or
            # take number. Match the actual returned asset instead of spending
            # again or guessing which provider request completed.
            receipt = await _camera_key_for_source(ctx, prefix, shot["id"], expected_source, decision["setup"])
            if receipt:
                paid_key, take = receipt
        if decision.get("setup") and paid_key.startswith(f"{prefix}:shot{shot['id']}:"):
            candidates.insert(0, (decision["setup"], paid_key))
        # A separately directed camera had its own correction and immutable ticket.
        # Match its photographed inputs AND exact owned source, even after an app retry.
        for ticket in (run.get("director_decisions") or {}).values():
            paid_key = ticket.get("request_key") or ""
            if not paid_key.startswith(f"{prefix}:shot{shot['id']}:"):
                continue
            setup = ticket.get("setup") or [ticket.get("first"), None]
            if list(setup) in setups and (setup, paid_key) not in candidates:
                candidates.append((setup, paid_key))
    for setup, paid_key in candidates:
        job = await _existing_job(ctx, "segment", paid_key)
        if (not job or job.status != "completed" or not job.output_asset_id
                or (job.request_data or {}).get("input_asset_ids") != [asset for asset in setup if asset]
                or (expected_source and expected_source != job.output_asset_id)):
            continue
        result = await _inspect(ctx, folder, plan, shot, job.output_asset_id, setup, library, material, cuts)
        result.update(source=job.output_asset_id, setup=setup, replaces_review=key, paid_request_key=paid_key,
                      camera_take=take, playback_review=PLAYBACK_REVIEW)
        if result.get("ok"):
            result["cut"]["take"] = take
        elif decision.get("office_start"):
            result["office_start"] = decision["office_start"]
        return await _record(ctx, prefix, run, shot["id"], side_key, result)
    return decision


async def _recut_repeated_pickup(ctx, folder, prefix, run, plan, shot, library, material, cuts, adjacent):
    """Re-edit an observed 1→2 prop reset from its own paid opening before booking a reshoot."""
    if (shot["id"] != 1 or plan.mode not in ("carry", "wear") or getattr(plan, "score_revision", 1) != 2
            or run.get("wan_final_key") or not adjacent):
        return None
    pickup = cuts.get(2) or {}
    if not pickup.get("review_head") or not any(asset == pickup["review_head"] for _, asset in adjacent):
        return None
    decisions = run.get("director_decisions") or {}
    seams = [v for k, v in decisions.items() if k.startswith(f"{recipe.REVIEW}:scene:home:") and v.get("ok") is False
             and v.get("after") == v.get("shot") == 1 and v.get("outgoing") and v.get("incoming")
             and (v.get("cut_assets") or [])[1:2] == [pickup.get("review_board")]]
    boards = {v["cut_assets"][0] for v in seams}
    if not boards:
        return None
    for key, decision in list(decisions.items()):
        old = decision.get("cut") or {}
        if (not key.startswith(f"{recipe.REVIEW}:take:1:") or ":recut-" in key or decision.get("ok") is not True
                or not old.get("source") or old.get("review_board") not in boards or not decision.get("evidence")):
            continue
        stage = f"before-pickup-{_fingerprint(old['review_board'], pickup['review_head'])}"
        result = await _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material, cuts,
                                         key, decision, "", old.get("take", 1), 0, stage=stage)
        if (result is not decision and result.get("ok") and (cut := result.get("cut"))
                and (result.get("report") or {}).get("product_before_cut") in ("out_of_frame", "at_rest")
                and commercial_frame_audit.window(old) != commercial_frame_audit.window(cut)):
            return cut
    return None


async def _historic_phase_cut(ctx, folder, prefix, run, plan, shot, library, material, cuts, correction, user_revision):
    """Try a genuine paid legacy take before the normal camera budget, across a standard app Retry."""
    idx = shot["id"]
    if run.get("wan_final_key"):
        return None
    repairs = run.get("commercial_repairs") or []
    standard_retry = any(r.get("target") == idx and r.get("revision") == user_revision
                         and (r.get("report") or {}).get("kind") == "commercial"
                         and (r.get("report") or {}).get("correction", "") == correction for r in repairs)
    if ((correction or user_revision) and not standard_retry) or (idx not in (9, 11) and not standard_retry):
        return None  # A new creative/adjacent correction must not resurrect an old cut.
    if idx not in (11, 25) and getattr(plan, "score_revision", 1) != 2:
        return None
    decisions = run.get("director_decisions") or {}
    first_camera = f"{recipe.REVIEW}:take:{idx}:{_fingerprint([], '', 1)}"
    for key, item in sorted(decisions.items(), key=lambda pair: not pair[0].startswith(first_camera)):
        if (not key.startswith(f"{recipe.REVIEW}:take:{idx}:") or ":recut-" in key
                or not _obsolete_selection(plan, idx, item)):
            continue
        if parents := DEPENDENCIES.get(idx):
            needed = [cuts[parent]["review_tail"] for parent in parents if parent in cuts]
            if len(needed) != len(parents):
                continue
            if idx == 28:
                needed.append(cuts[22]["review_head"])
            recorded = item.get("continuity_anchors")
            if recorded is None and idx in MATCH_IN and item.get("paid_request_key") and item.get("setup"):
                # Older paid reviews list the outgoing cut immediately after
                # first frame, set, and optional authored end photograph.
                edge = 2 + bool(item["setup"][1])
                recorded = (item.get("evidence") or [])[edge:edge + 1]
            old_departure = idx == 9 and not recorded and _obsolete_selection(plan, idx, item) == "departure-position"
            if recorded != needed and not old_departure:
                continue  # This paid take must still match the exact neighboring footage.
        take = item.get("camera_take") or (item.get("cut") or {}).get("take") or 1
        result = await _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material,
                                         cuts, key, item, "", take, 0)
        if result is not item and result.get("ok") and result.get("cut"):
            return result["cut"]
    return None


def _unpublished_first_match(run, plan, cuts):
    cut = cuts.get(11) or {}
    if getattr(plan, "score_revision", 1) != 2 or not cut.get("review_board") or run.get("wan_final_key"):
        return None
    decisions = run.get("director_decisions") or {}
    for key, decision in decisions.items():
        saved = decision.get("cut") or {}
        if (key.startswith(f"{recipe.REVIEW}:take:11:") and decision.get("ok")
                and saved.get("review_board") == cut["review_board"]
                and commercial_frame_audit.window(saved) == commercial_frame_audit.window(cut)
                and _obsolete_selection(plan, 11, decision) == "first-match-entry"):
            side = decisions.get(f"{key}:recut-first-match-entry-v1") or {}
            if side.get("ok") and commercial_frame_audit.window(side.get("cut") or {}) == commercial_frame_audit.window(cut):
                return None
            return key, decision
    return None


async def _recheck_saved_match(ctx, folder, prefix, run, plan, shot, library, material, cuts, legacy):
    key, old = legacy
    saved = cuts[11]
    checked = await _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material,
                                      cuts, key, old, "", saved.get("take", 1), 0)
    if checked is old:
        raise RemakeStopped("未能对应原动作镜的已付拍摄回执。", vi="Chưa đối chiếu được biên nhận của cảnh chuyển động đã quay.",
                            en="The original camera receipt could not be matched to this shot.")
    new = checked.get("cut") or {}
    if checked["ok"] and commercial_frame_audit.window(new) == commercial_frame_audit.window(saved):
        return
    affected = {11}
    if not checked["ok"] or new.get("review_tail") != saved.get("review_tail"):
        affected.add(12)
    evidence = {"key": f"{key}:recut-first-match-entry-v1", "old_board": saved["review_board"],
                "source": saved["source"], "shot": 11, "reason": checked.get("reason") or checked["report"]["reason"]}
    if any(cut["shot"] in affected for cut in run.get("wan_cuts") or []):
        await commercial_frame_audit.reopen_saved(ctx, prefix, run, affected, [], verified_first_match=evidence)
    for index in affected:
        cuts.pop(index, None)
    if checked["ok"]:
        cuts[11] = new


async def _film(ctx, folder, prefix, run, plan, shot, library, material, cuts, revision, *, adjacent=(), user_revision=0,
                director_restage=False):
    from light.shot_editor import publish_request

    idx = shot["id"]
    if adjacent and not director_restage and (recut := await _recut_repeated_pickup(
            ctx, folder, prefix, run, plan, shot, library, material, cuts, adjacent)):
        return recut
    if not adjacent and not director_restage and (archived := await _historic_phase_cut(
            ctx, folder, prefix, run, plan, shot, library, material, cuts, revision, user_revision)):
        return archived
    guide = await _reference_asset(ctx, f"commercial-blocking-v1-{idx:02}", (GUIDES / f"{idx:02}.jpg").read_bytes())
    parents = () if idx == 9 and getattr(plan, "score_revision", 1) != 2 else DEPENDENCIES.get(idx, ())
    anchors = [cuts[parent]["review_tail"] for parent in parents]
    if idx == 28:
        anchors.append(cuts[22]["review_head"])
    continuity_anchors = list(anchors)
    anchors.extend(picture for _, picture in adjacent)
    if user_revision:
        anchors.append(f"requested-revision-{user_revision}")
    if director_restage:
        anchors.append("director-restage")
    correction = revision
    office_start = None
    for take in range(1, (1 if director_restage else MAX_TAKES) + 1):
        key = f"{recipe.REVIEW}:take:{idx}:{_fingerprint(anchors, correction, take)}"
        prior = (run.get("director_decisions") or {}).get(key)
        if idx in OUT_POSE and not (prior and prior.get("ok")):
            # Previous camera builds only inspected the motionless final few
            # frames. A rejected old interval must not exhaust all three takes
            # without evaluating the corrected endpoint/window contract.
            key += ":contact-v2"
        decision = (run.get("director_decisions") or {}).get(key)
        if decision:
            decision = await _recut_checkpoint(ctx, folder, prefix, run, plan, shot, library, material, cuts,
                                               key, decision, correction, take, user_revision)
            if (missing := _obsolete_selection(plan, idx, decision)) in ("first-match-entry", "departure-position"):
                # A legacy approval without a matching paid receipt cannot authorize
                # this updated opening. Never overwrite it or return its stale cut.
                key += f":{missing}-v1"
                decision = (run.get("director_decisions") or {}).get(key)
        if (idx == 26 and decision and decision.get("ok") and decision.get("cut")
                and not decision["cut"].get("motion_performance")):
            # Resume an in-progress job photographed before the nine-frame
            # handoff check. Recut the exact paid source without filming again.
            original = decision
            key += ":motion-v1"
            decision = (run.get("director_decisions") or {}).get(key)
            if not decision:
                photos = original.get("evidence") or []
                old_cut = original["cut"]
                first = photos[0] if photos else old_cut["review_head"]
                last = None if original.get("single_camera") else (photos[2] if len(photos) > 2 else old_cut["review_tail"])
                result = await _inspect(ctx, folder, plan, shot, old_cut["source"], (first, last), library, material, cuts)
                if result.get("ok"):
                    result["cut"]["take"] = take
                if original.get("single_camera"):
                    result["single_camera"] = True
                decision = await _record(ctx, prefix, run, idx, key, result)
        if not decision:
            setup = await _frames(ctx, prefix, plan, shot, library, material, cuts, guide, correction, run,
                                  **({"adjacent": adjacent} if adjacent else {}),
                                  **({"user_revision": user_revision} if user_revision else {}),
                                  **({"office_retake": True, "observed_start": office_start}
                                     if idx == 25 and (take > 1 or revision or user_revision) else {}))
            single_start = idx in OUT_POSE and not setup[1]
            request = shot_request(prefix, plan, shot, *setup, correction, take, user_revision=user_revision)
            if director_restage:
                # One directed take per shot and explicit user revision, even
                # when a later seam review reopens a different adjacent cut.
                key_for_paid_take = f"{recipe.REVIEW}:recovery-camera:{idx}:r{user_revision}"
                ticket = await _record(ctx, prefix, run, idx, key_for_paid_take,
                                       {"request_key": request.idempotency_key, "setup": list(setup)})
                if ticket["request_key"] != request.idempotency_key:
                    raise _rejected(plan, shot, {
                        "vi": "Cảnh quay bổ sung của đạo diễn đã được sử dụng cho lần dựng này.",
                        "zh": "本次制作已用过该镜的导演补拍。", "en": "This shot already used its directed retake.",
                    }[plan.language], correction)
            if single_start:
                # Only one paid first-frame-only attempt for this shot within
                # the user's render/revision. A worker restart keeps its key.
                ticket_key = f"{recipe.REVIEW}:single-camera:{idx}:r{user_revision}"
                ticket = await _record(ctx, prefix, run, idx, ticket_key,
                                       {"request_key": request.idempotency_key, "first": setup[0]})
                if ticket["request_key"] != request.idempotency_key:
                    prior_note = correction or shot["end"]
                    raise _rejected(plan, shot, {
                        "zh": "已核准首帧的实拍仍未形成可用接点。", "vi": "Cảnh quay từ ảnh đầu chưa có điểm nối phù hợp.",
                        "en": "The take filmed from the approved opening did not produce a usable match.",
                    }[plan.language], prior_note)
            await publish_request(ctx, prefix, request)
            try:
                source = await _job(ctx, request, kind="segment", label=f"{idx:02} · {shot['title']}", run=run)
            except RemakeStopped:
                prior = await _existing_job(ctx, "segment", request.idempotency_key)
                rejection = ((prior.result_data or {}).get("submit_error") or {}) if prior else {}
                if not prior or prior.status != "failed" or not prior.provider_task_id or rejection.get("submission_outcome") == "ambiguous":
                    raise
                reason, note = {
                    "vi": ("Dịch vụ quay chưa trả về video cho cảnh này.",
                           "Quay lại một lần theo đúng động tác, chỉ giữ những nhân vật và đồ vật có trong cảnh."),
                    "en": ("The video service did not deliver this shot.",
                           "Repeat the action once with only the people and objects specified for this shot."),
                    "zh": ("上游拍摄未交付视频。", "按相同动作拍一次，镜头内只出现剧本的角色与物体。"),
                }[plan.language]
                result = {"ok": False, "reason": reason, "correction": note, "provider_failure": True}
            else:
                result = await _inspect(ctx, folder, plan, shot, source, setup, library, material, cuts)
                result.update(source=source, setup=list(setup), paid_request_key=request.idempotency_key, camera_take=take,
                              continuity_anchors=continuity_anchors)
                if result.get("ok"):
                    result["cut"]["take"] = take
                elif idx == 25:
                    result["office_start"] = await _actual_frame(ctx, source, folder / f"{source}.mp4", 0)
            if single_start:
                result["single_camera"] = True
            decision = await _record(ctx, prefix, run, idx, key, result)
        if decision["ok"]:
            return decision["cut"]
        if decision.get("single_camera"):
            break
        office_start = decision.get("office_start")
        correction = " ".join(filter(None, (revision, decision["correction"])))
    failure = _rejected(plan, shot, decision["reason"], decision["correction"])
    if decision.get("provider_failure"):
        failure.review_feedback["provider_failure"] = True
    raise failure


async def _photograph(ctx, folder, prefix, run, plan, shot, library, material, cuts, correction, *, adjacent, user_revision):
    try:
        return await _film(ctx, folder, prefix, run, plan, shot, library, material, cuts, correction,
                           adjacent=adjacent, user_revision=user_revision)
    except ReviewRejected as failure:
        if getattr(plan, "score_revision", 1) != 2 or failure.review_feedback.get("provider_failure"):
            raise
        from light import commercial_recovery

        proposal = await commercial_recovery.restage(
            ctx, prefix, run, plan, shot, library, cuts, failure, user_revision=user_revision)
        if not proposal:
            raise
    direction, evidence = proposal
    return await _film(ctx, folder, prefix, run, plan, shot, library, material, cuts,
                       " ".join(filter(None, (correction, direction))), adjacent=[*adjacent, *evidence],
                       user_revision=user_revision, director_restage=True)


async def _shoot_remaining(work, shots, cuts, *, concurrency=5):
    """Start a dependent shot as soon as its actual predecessor finishes."""
    priority = (11, 17, 23, 25, 22, 26, 27, 28, 12, 18, 24, 2, 3)
    needed = {shot["id"]: shot for shot in shots if shot["id"] not in cuts}
    running = {}
    try:
        while needed or running:
            ready = sorted((i for i in needed if all(p in cuts for p in DEPENDENCIES.get(i, ()))),
                           key=lambda i: (priority.index(i) if i in priority else len(priority), i))
            for i in ready[:concurrency - len(running)]:
                running[asyncio.create_task(work(needed.pop(i)))] = i
            if not running:
                raise RemakeStopped("镜头的接点素材不完整。", vi="Chưa đủ khung hình để nối các cảnh quay.",
                                    en="The frames needed to join these shots are incomplete.")
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                i = running.pop(task)
                cuts[i] = task.result()
    finally:
        # Paid jobs already submitted can be resumed by idempotency key. Do not
        # cancel another active operation halfway through its provider submission.
        if running:
            await asyncio.gather(*running, return_exceptions=True)


def _dependents(index):
    family = {index}
    for child, parents in DEPENDENCIES.items():
        if family.intersection(parents):
            family.add(child)
    return family


def _scene_proofs(decisions, proof, plan=None, cuts=None):
    if not commercial_frame_audit.certified(decisions, proof, plan, cuts):
        return False
    # Earlier completed v4 edits used only the signature-cut certificate.
    if "scene_checks" not in proof:
        return getattr(plan, "score_revision", 1) != 2 and not getattr(plan, "track_wardrobe", "")
    keys = proof["scene_checks"]
    if not isinstance(keys, dict) or set(keys) != set(SCENE_JOINS) or not decisions.get(proof.get("signature_check"), {}).get("ok"):
        return False
    uniform = decisions[proof["signature_check"]].get("wardrobe_check")
    if uniform or getattr(plan, "track_wardrobe", ""):
        check = decisions.get(uniform) or {}
        if not (check.get("ok") is True and check.get("wardrobe_continuous") is True and check.get("sheet")):
            return False
    boards = proof.get("cut_assets") or []
    for scene, pairs in SCENE_JOINS.items():
        check = decisions.get(keys[scene]) or {}
        if (not check.get("ok") or not check.get("sheets") or check.get("pairs") != [[i, i + 1] for i in pairs]
                or check.get("cut_assets") != boards[pairs[0] - 1:pairs[-1] + 1]):
            return False
    return True


def _edit_proof(decisions, plan=None, boards=None, cuts=None):
    base = f"{recipe.REVIEW}:joins"
    options = [decisions.get(base)]
    options.extend(value for key, value in decisions.items() if key.startswith(f"{base}:audited:"))
    return next((proof for proof in options if proof and proof.get("approved") is True
                 and (boards is None or proof.get("cut_assets") == boards)
                 and _scene_proofs(decisions, proof, plan, cuts)), None)


def _approved_edit(run, plan=None):
    """Recover the exact accepted edit after a crash between seam review and the last cut write."""
    decisions = run.get("director_decisions") or {}
    proof = _edit_proof(decisions, plan) or {}
    boards = proof.get("cut_assets") or []
    if len(boards) != len(recipe.BEATS):
        return None
    reviewed = [item["cut"] for item in decisions.values() if item.get("ok") and item.get("cut")]
    by_board = {cut["review_board"]: cut for cut in (*reviewed, *(run.get("wan_cuts") or []))
                if cut.get("review_board") and cut.get("review_version") == recipe.REVIEW}
    accepted = {}
    for index, (board, beat) in enumerate(zip(boards, recipe.BEATS, strict=True), 1):
        cut = by_board.get(board) or {}
        if cut.get("shot") != index or cut.get("output_frames") != beat.frames or not cut.get("source"):
            return None
        accepted[index] = cut
    return accepted if commercial_frame_audit.certified(decisions, proof, plan, accepted) else None


def _seam_sheet(pairs, ends):
    w, h = 320, 180
    sheet = Image.new("RGB", (w * 4, (h + 23) * len(pairs)), "#202020")
    draw = ImageDraw.Draw(sheet)
    for row, left in enumerate(pairs):
        y = (h + 23) * row
        for col, (index, label) in enumerate(((left, "IN"), (left, "OUT"), (left + 1, "IN"), (left + 1, "OUT"))):
            picture = ends[index][0 if label == "IN" else 1]
            sheet.paste(picture.resize((w, h), Image.Resampling.LANCZOS), (col * w, y + 23))
            draw.text((col * w + 5, y + 4), f"CUT {left:02}-{left + 1:02}   SHOT {index:02} {label}",
                      fill="#ffd678" if col in (1, 2) else "white")
    return _jpeg(sheet)


async def _scene_joins(ctx, folder, prefix, run, plan, cuts, material, scene):
    pairs = SCENE_JOINS[scene]
    ids = range(pairs[0], pairs[-1] + 2)
    cut_assets = [cuts[index]["review_board"] for index in ids]
    key = f"{recipe.REVIEW}:scene:{scene}:{_fingerprint(cut_assets)}"
    decisions = run.get("director_decisions") or {}
    normal = getattr(plan, "score_revision", 1) == 2
    if normal and not decisions.get(key, {}).get("ok"):
        key += ":normal-playback-v1"
    decision = decisions.get(key)
    if decision:
        return key, decision

    async def endpoints(index):
        board = cuts[index]["review_board"]
        filename = folder / f"{board}.jpg"
        if not filename.exists():
            await product_story._download(ctx, board, filename)
        with Image.open(filename) as strip:
            # The contact sheet was made directly from this selected take. Its
            # first and fifth columns contain the actual first/last cut frames.
            w = strip.width // 5
            return index, (strip.crop((0, 20, w, strip.height)), strip.crop((4 * w, 20, 5 * w, strip.height)))

    ends = dict(await _parallel(ids, endpoints, concurrency=3))
    sheets = []
    for start in range(0, len(pairs), 5):
        rows = pairs[start:start + 5]
        sheet = _seam_sheet(rows, ends)
        sheets.append(await _reference_asset(ctx, f"commercial-seams-{scene}-{_fingerprint(cut_assets)}-{start}", sheet))
    allowed = create_model(f"CommercialSceneSeams{pairs[0]}To{pairs[-1]}", __base__=SceneJoin,
                           after=(int | None, Field(default=None, ge=pairs[0], le=pairs[-1])))
    directions = recipe.shots(plan)
    transfer = _transfer(plan) if scene == "office" else {}
    editing = ""
    if normal and scene == "home":
        editing = ("1→2是视线切商品特写，允许切角时重复少量伸手或物件刚起动。若第一镜已经明显拿走商品，第二镜又从原处完整拿一次，"
                   "第一镜提前演了第二镜的动作：修第一镜的出点，保留第二镜本来的取用。若第二镜自己把物件放回再拿，才修第二镜。"
                   "快剪的生活插镜，先看主商品及正在操作的主体，动作能否一眼连续读懂。次要布景、垫面或衬托道具在反打时的"
                   "纹理材质与摆位轻微不同可通过；只有它本身是主商品、关键功能部件，或改变导致动作明显无法发生，才退。")
    elif normal and scene == "office":
        editing = ("26→27是两人交接切接收者的反应特写。核对控制权、穿戴或使用状态与反应主角。"
                   "在同一个人身上稳稳戴好、手中接稳或已站到使用位后，切角可以略过轻扶、撤手、顺手调整；"
                   "近景少一只扶手或接收者自己扶稳属于自然动作省略。明确换了持有者、商品、戴回原人或第二次交接才退。")
        transfer.pop("next_opening", None)
    elif normal and scene == "street":
        editing = ("脚步特写受阴影和机位影响，只有可辨的裤脚或鞋确实换了款式或主色，导致普通观看可见的换装才退。"
                   "特写应匹配两侧真实街舞中同一个人的通勤衣着，镜头只拍腿时保持商品状态即可。")
    answer = await reference_quality.ask(
        ctx, "commercial_scene_seams", allowed, REVIEW_LANGUAGE[plan.language] +
        "这些是已获逐镜认可的真实相邻镜头。每行是一个剪点，四列是上镜首帧、上镜末帧、下镜首帧、下镜末帧。"
        "只审实际切口的中间两列，外侧两列说明动作从哪来、到哪去。正常换角度、景别、剪入特写、"
        "轻微姿势变化、物件在画外及时间压缩允许；看不见不等于丢了商品，不推测被遮挡的细节。"
        "日常插镜检查同一器物的盖、容器与明确完成的动作是否直接倒转或凭空重复。"
        "运动和街头检查能同时看到的商品是否换货、换人或从包的一侧突然跳到另一处，考虑实际机位反打。"
        "只在两侧可见状态明确互相矛盾、观众能当场看出的情况下退回，选择一个最直接的剪点及须修的一镜。"
        "outgoing、incoming分别描述实际可见矛盾。真实商品参照在末尾，不能要求每镜强行出现商品。" + editing,
        {"language": plan.language, "scene": scene, "product": plan.product, "carry": plan.carry,
         **transfer,
         "pairs": [(n, n + 1) for n in pairs], "sheets": len(sheets),
         "shots": [{k: directions[i - 1][k] for k in ("id", "title", "action", "product_visible")} for i in ids]},
        [*sheets, *material], max_tokens=1300, timeout=75)
    verdict = allowed.model_validate(answer["report"])
    decision = await _record(ctx, prefix, run, pairs[-1] + 1, key,
                             {**answer, **verdict.model_dump(), "cut_assets": cut_assets, "sheets": sheets,
                              "pairs": [[n, n + 1] for n in pairs]})
    return key, decision


async def _wardrobe_joins(ctx, prefix, run, plan, cuts):
    pairs = [(cuts[out]["review_tail"], cuts[inc]["review_head"]) for inc, out in MATCH_IN.items()]
    key = f"{recipe.REVIEW}:wardrobe-joins:{_fingerprint(plan.track_wardrobe, pairs)}"
    saved = (run.get("director_decisions") or {}).get(key)
    if saved:
        return key, saved
    with TemporaryDirectory(prefix="commercial-wardrobe-cuts-") as directory:
        photos = {photo: Path(directory) / str(i) for i, photo in enumerate(dict.fromkeys(pic for pair in pairs for pic in pair))}
        await asyncio.gather(*(product_story._download(ctx, photo, path) for photo, path in photos.items()))
        width, height = 544, 306
        sheet = Image.new("RGB", (width * 2, (height + 22) * 3), "#222")
        draw = ImageDraw.Draw(sheet)
        for row, (numbers, images) in enumerate(zip(MATCH_IN.items(), pairs, strict=True)):
            arrival, departure = numbers
            for col, (label, photo) in enumerate(zip((f"{departure} OUT", f"{arrival} IN"), images, strict=True)):
                x, y = col * width, row * (height + 22)
                with Image.open(photos[photo]) as picture:
                    sample = picture.convert("RGB")
                    sample.thumbnail((width, height))
                    sheet.paste(sample, (x + (width - sample.width) // 2, y + 22 + (height - sample.height) // 2))
                draw.text((x + 6, y + 4), label, fill="white")
        evidence = await _reference_asset(ctx, f"commercial-wardrobe-seams-{_fingerprint(pairs)}", _jpeg(sheet, 92))
    result = await reference_quality.ask(
        ctx, "commercial_wardrobe_joins", WardrobeJoin, REVIEW_LANGUAGE[plan.language] +
        "这张照片表每行都是一次实际硬切，左列是上镜最后一帧，右列是下镜第一帧。先逐格只写真实可见衣装、"
        "非商品的通勤背带或包及真实商品，分别比较同一行左右。不可用文字剧本补出照片中看不到的颜色或衣服。"
        "第一行11→12通勤换到跑道装，第二行17→18跑道换回通勤，第三行23→24仍是同一套通勤装。"
        "若实际可见的衣服没换到目标造型，或第三行出现明显换装，wardrobe_continuous=false。"
        "跑道换掉非商品包应当认可；若衣物或包即主商品则保留该商品。对被裁切的肩带、衣角不要猜测。"
        "不审核其他镜头，不靠图像内背景名称判断演员穿的是什么。只选真正需要重拍的一侧镜号。",
        {"language": plan.language, "track_wardrobe": plan.track_wardrobe, "commute": plan.protagonist,
         "product": plan.product, "mode": plan.mode}, [evidence], max_tokens=1100, timeout=65)
    verdict = WardrobeJoin.model_validate(result["report"])
    return key, await _record(ctx, prefix, run, verdict.shot if not verdict.ok else 24, key,
                              {**result, **verdict.model_dump(), "frames": pairs, "sheet": evidence})


async def _signature_joins(ctx, prefix, run, plan, cuts, material):
    # Ordinary insert edits cannot change the gesture cuts or the final callback.
    ids = {i for pair in MATCH_IN.items() for i in pair} | {14, 22, 25, 26, 27, 28}
    boards = [cuts[i]["review_board"] for i in sorted(ids)]
    wardrobe_key, uniform = None, None
    if getattr(plan, "track_wardrobe", ""):
        wardrobe_key, uniform = await _wardrobe_joins(ctx, prefix, run, plan, cuts)
        if not uniform["ok"]:
            return wardrobe_key, uniform
        boards.append(wardrobe_key)
    key = f"{recipe.REVIEW}:signature:{_fingerprint(boards)}"
    decisions = run.get("director_decisions") or {}
    normal = getattr(plan, "score_revision", 1) == 2
    if normal and not decisions.get(key, {}).get("ok"):
        # The office seam and performance reviews already see the entire transfer. Here 27 only identifies the next dancer.
        selected = [cuts[i]["review_board"] for i in sorted(ids - {25, 26})]
        key = f"{recipe.REVIEW}:signature:{_fingerprint(selected, wardrobe_key)}:normal-playback-v1"
    decision = decisions.get(key)
    if not decision:
        refs = []
        for arrival, departure in MATCH_IN.items():
            refs.extend([(f"{departure}实际末帧", cuts[departure]["review_tail"]),
                         (f"{arrival}实际首帧", cuts[arrival]["review_head"])])
        refs.extend([("第12镜接点后真正的起跑方向", cuts[12]["review_board"]),
                     ("第14镜同一运动场的实拍冲刺方向", cuts[14]["review_board"])])
        if not normal:
            refs.extend([("第25镜两人发生冲突", cuts[25]["review_board"]),
                         ("第26镜实际交接，由左到右", cuts[26]["review_board"])])
        refs.extend([("第27镜接手后反应的人，结尾应认出这位演员", cuts[27]["review_board"]),
                     ("第22镜原街角、舞者和地标", cuts[22]["review_board"]),
                     ("第28镜回到街角改由权威人物与地标共舞", cuts[28]["review_board"])])
        refs.extend([("真实商品素材", photo) for photo in material])
        continuity = ("跑步方向根据连续几个画面中的实际位移与景深变化，不能单凭视线、脸的朝向或某一帧判断。"
                  "切到迎面或斜向镜头后继续朝摄影机跑、跟拍中人物居中，均可与前镜同段跑步相接；"
                  "只有两条都明确是相反方向的横向位移、连看像原路折返才退。"
                  "第27镜只用于核对回街跳舞的是刚才体验商品的人。办公室另有剪点及动作审查。" if normal else
                  "检查工作场所的人物关系和连续交接，用户实际商品或固定使用位置只交一次，接手的人是后来微笑的人。"
                  "按transfer_mode核对26/27剪点：fit允许26末仍有扶正的手，到27特写再撤；pass已交由接收者承重；access是位置交让。")
        schema = SignatureJoin if normal else Join
        answer = await reference_quality.ask(
            ctx, "commercial_reference_joins", schema,
            REVIEW_LANGUAGE[plan.language] +
            "作为电视广告剪辑师，只检查提交的真实剪出画面。三组动作硬切允许布景和光线改变。"
            "若给定track_wardrobe，在可见衣物的范围内核对：11→12通勤转运动装、17→18恢复通勤装，"
            "23→24通勤造型不变。前两切中换掉非商品通勤包是有意换装，不是穿帮；作为主商品的衣包仍应保留。"
            "若衣物没有入画，不能仅凭颜色不明推断换装错误。"
            "前后应一眼认出同一人的同一触点动作，保持相位、画面朝向和手的位置；忽略手指或构图的小差异。"
            "第23→24刻意剪在释放上：上镜指尖仍碰着、下镜第一帧已略抬起但仍紧邻同一部件，正是原片的连续相位，"
            "应当通过。只有明显换手、手已经离远、重新伸向部件或整段释放消失才退。"
            "首组优先原片向画右。若实际两镜都采用另一摄影侧，只有脸和手在两镜方向一致、接点仍在演员真实指定的身体部位、"
            "第12的起跑与第14的冲刺同向、画面中文字也正常，才可认可整组改机位；不能仅凭单镜姿势放行。"
            "固定商品留在室内同一安装地，户外以空手姿势匹配，场地变化时商品不出现是正确的。"
            + continuity +
            "结尾是第22镜同街角、机位和同一件有固定基座的风动充气舞者。原来的真人舞者换成穿工作造型的权威人物。"
            "人和布臂都在跳动；不能用第二个真人或静止杆替代装置。"
            "楼面纹理小改变不构成穿帮。若要求的硬切读不通、明显错货、换脸、手穿过商品或结尾地点/演员错误，"
            "退回最直接需要重拍的一个镜号并给出单一可执行修正。用指定成片语言给出具体reason/correction。",
            {"language": plan.language, "contact": plan.contact, "carry": plan.carry, "handoff": plan.handoff, "mode": plan.mode,
             "street_anchor": plan.street_anchor, "track_wardrobe": getattr(plan, "track_wardrobe", ""),
             **({} if normal else _transfer(plan)),
             "appearance": plan.appearance, "references": [label for label, _ in refs]},
            [asset for _, asset in refs], max_tokens=1600, timeout=80)
        verdict = schema.model_validate(answer["report"])
        entry = {**answer, **verdict.model_dump()}
        if wardrobe_key:
            entry["wardrobe_check"] = wardrobe_key
            entry["report"] = {**entry["report"], "wardrobe_sequence": uniform["wardrobe_sequence"],
                               "wardrobe_continuous": uniform["wardrobe_continuous"]}
        decision = await _record(ctx, prefix, run, 28, key, entry)
    return key, decision


async def _check_joins(ctx, folder, prefix, run, plan, cuts, material, *, cast=None):
    from light import commercial_playback_review as playback

    cut_assets = [cuts[i]["review_board"] for i in range(1, len(recipe.BEATS) + 1)]
    committed_key = f"{recipe.REVIEW}:joins"
    decisions = run.get("director_decisions") or {}
    if _edit_proof(decisions, plan, cut_assets, cuts):
        return []
    if committed_key in decisions:
        # A previously published legacy aggregate is immutable. Store the
        # newly audited certificate separately without erasing its history.
        committed_key += f":audited:{_fingerprint(cut_assets, getattr(plan, 'track_wardrobe', ''))}"
        if commercial_frame_audit.enabled(plan):
            committed_key += ":" + _fingerprint(playback.VERSION, playback.plan_key(plan),
                                                 [commercial_frame_audit.window(cuts[i]) for i in sorted(cuts)])

    async def check(group):
        if group == "signature":
            return await _signature_joins(ctx, prefix, run, plan, cuts, material)
        return await _scene_joins(ctx, folder, prefix, run, plan, cuts, material, group)

    results = await _parallel(["signature", *SCENE_JOINS], check, concurrency=3)
    issues = [decision for _, decision in results if not decision["ok"]]
    if issues:
        return issues
    frame_proof = {}
    if commercial_frame_audit.enabled(plan):
        key, watch = await playback.inspect(ctx, folder, prefix, run, plan, cuts, material, cast or {}, _record)
        if not watch["ok"]:
            return watch["issues"]
        frame_proof = {"playback_check": key}
    committed = await _record(ctx, prefix, run, 28, committed_key,
                              {"approved": True, "cut_assets": cut_assets, "report": results[0][1]["report"], **frame_proof,
                               "signature_check": results[0][0], "scene_checks": dict(zip(SCENE_JOINS, (k for k, _ in results[1:]), strict=True))})
    if committed.get("cut_assets") != cut_assets or not _scene_proofs(run["director_decisions"], committed, plan, cuts):
        raise RemakeStopped("提交的镜头与已保存的接点审片记录不一致。",
                            vi="Các cảnh gửi dựng chưa khớp với bản đã duyệt tại điểm nối.",
                            en="The submitted shots do not match the approved cut points.")
    return []


def _repair_joins(issues, cuts, corrections, adjacent):
    affected = set()
    # Freeze all neighboring evidence before reopening any shot. Unrelated
    # rooms can be repaired concurrently within this same render.
    for issue in issues:
        target = issue["shot"]
        corrections[target] = " ".join(filter(None, (corrections.get(target), issue["correction"])))
        if (after := issue.get("after")) is not None:
            if target == after:
                edge = ("下镜实际入点，仅用于校正本镜剪出时能同时看到的道具、人物和携带位置",
                        cuts[after + 1]["review_head"])
            else:
                edge = ("上镜实际出点，仅用于校正本镜剪入时能同时看到的道具、人物和携带位置",
                        cuts[after]["review_tail"])
            adjacent[target] = [edge]
        affected.update(_dependents(target))
    for index in affected:
        cuts.pop(index, None)
    return affected


async def render(ctx, form, run, plan):
    from light import commercial_post, commercial_scene_recovery
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_wan_edit

    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, recipe.REVIEW)
    if run.get("commercial_carry_calibration"):
        plan = recipe.Plan.model_validate(run["remake_adaptation"])
    accepted = _approved_edit(run, plan)
    cuts = accepted or {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    if accepted is None:
        cuts.update(await commercial_scene_recovery.restore(ctx, run, plan, cuts))
    legacy_match = _unpublished_first_match(run, plan, cuts)
    with TemporaryDirectory(prefix="openbox-commercial-") as directory:
        folder = Path(directory)
        if accepted is None or legacy_match:
            await remake_stage(ctx, prefix, "remake_frames")
            library, material, plan = await _cast_and_sets(task, prefix, plan, run, form=form)
            shots = recipe.shots(plan)
            if library.get("carry_at_feet"):
                for number in (13, 19):
                    shots[number - 1]["product_visible"] = True
            await _legacy_insert_openings(task, prefix, run, plan, shots, material)
            await remake_stage(ctx, prefix, "wan_shots")
            if legacy_match:
                await _recheck_saved_match(task, folder, prefix, run, plan, shots[10], library, material, cuts, legacy_match)
            requested = _requested_repairs(run)
            corrections = {index: note for index, (_, note) in requested.items()}
            adjacent = {}

            async def photograph(shot):
                return await _photograph(task, folder, prefix, run, plan, shot, library, material, cuts,
                                   corrections.get(shot["id"], ""), adjacent=adjacent.get(shot["id"], ()),
                                   user_revision=requested.get(shot["id"], (0, ""))[0])

            for round_no in range(MAX_JOIN_REPAIRS + 1):
                await _shoot_remaining(photograph, shots, cuts)
                await remake_stage(ctx, prefix, "wan_edit")
                issues = await _check_joins(task, folder, prefix, run, plan, cuts, material, cast=library)
                if not issues:
                    break
                if round_no == MAX_JOIN_REPAIRS:
                    issue = issues[0]
                    raise _rejected(plan, shots[issue["shot"] - 1], issue["reason"], issue["correction"])
                affected = _repair_joins(issues, cuts, corrections, adjacent)
                await commercial_frame_audit.reopen_saved(task, prefix, run, affected, issues)
                await remake_stage(ctx, prefix, "wan_shots")
        else:
            await remake_stage(ctx, prefix, "wan_edit")
        ordered = [cuts[i] for i in range(1, len(recipe.BEATS) + 1)]
        committed = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
        for cut in ordered:
            if committed.get(cut["shot"]) != cut:
                await save_reference_cut(ctx, prefix, cut)
        request = recipe.final_request(ordered, plan)
        key = run.get("wan_final_key") or f"{prefix}:final:{_digest(json.dumps(request, sort_keys=True))}"
        run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
        await remake_stage(ctx, prefix, "compose")
        job = await _existing_job(ctx, "compose", key)
        if not job:
            async def materialize(cut):
                filename = folder / f"source-{cut['shot']:02d}.mp4"
                original = folder / f"{cut['source']}.mp4"
                if original.exists():
                    filename.hardlink_to(original)
                else:
                    await product_story._download(task, cut["source"], filename)

            await _parallel(ordered, materialize, concurrency=5)
            output, measured = await commercial_post.assemble(folder, plan, ordered, ctx=task)
            job = await product_story._publish(task, key, output, plan, request, measured)
    if not recipe.is_complete_cut(run, job) or not await vp._attach_completed(job, ctx):
        raise RemakeStopped("成片未通过最后的帧数、声音及交付核验。",
                            vi="Video chưa vượt qua bước kiểm tra cuối về số khung hình, âm thanh và tệp xuất.",
                            en="The final video did not pass the frame count, audio and file delivery checks.")
    return job.output_asset_id, recipe.DURATION
