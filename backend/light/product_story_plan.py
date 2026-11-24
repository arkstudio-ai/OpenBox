"""The brand story storyboard and its delivery contract."""

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from light import commercial_plan
from light.reference_plan import Direction, Short, Shot
from light.video_language import END_CARD, Language

TEMPLATE = "product_story"
FORMAT = "product-story-v1"
ADAPTIVE_FORMAT = "product-story-v2"
CONTINUITY_FORMAT = "product-story-v3"
FORMATS = {FORMAT, ADAPTIVE_FORMAT, CONTINUITY_FORMAT, commercial_plan.FORMAT}
VISUAL_REVIEW = "product-story-continuity-v3"
MAX_JOIN_REPAIRS = 2
ASSETS = Path(__file__).with_name("remake_assets") / "product-story"
FPS = 24
FRAME_COUNT = 1296
DURATION = FRAME_COUNT / FPS
FILMED_COUNT = 16
CREDITS = "Dream Culture — Kevin MacLeod (incompetech.com), CC BY 4.0. Excerpted and mixed."

class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    format: Literal["product-story-v1"] = FORMAT
    language: Language = "zh"
    title: str = Field(default="这一张，有你", min_length=1, max_length=20)
    product: str = Field(default="小米 18", min_length=1, max_length=20)
    appearance: str = Field(default="", max_length=320)
    headline: str = Field(default="这一张，有你。", min_length=1, max_length=20)
    opening: str = Field(default="爸，怎么又没你？", min_length=1, max_length=64)
    answer: str = Field(default="我在拍啊。", min_length=1, max_length=48)
    invitation: str = Field(default="爸，往我这边一点。", min_length=1, max_length=64)

    def localized(self, language: Language) -> "Plan":
        defaults = {
            "en": ("In the picture", "Xiaomi 18", "In the picture.",
                   "Dad, where are you?", "I took the photo.", "Dad, come a little closer."),
            "vi": ("Có ba trong ảnh", "Xiaomi 18", "Có ba trong ảnh.",
                   "Ba, sao lại thiếu ba?", "Ba chụp mà.", "Ba ơi, lại gần con chút."),
        }
        fields = ("title", "product", "headline", "opening", "answer", "invitation")
        if language == "zh":
            return self.model_copy(update={"language": language})
        return self.model_copy(update={
            "language": language,
            **{field: value for field, value in zip(fields, defaults[language], strict=True)
               if getattr(self, field) == type(self).model_fields[field].default},
        })


class BrandShot(Shot):
    product_in_first_frame: bool


class BrandPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    format: Literal["product-story-v2"] = ADAPTIVE_FORMAT
    language: Language = "zh"
    title: Short
    product: Short
    product_assets: list[str] = Field(min_length=1, max_length=2)
    appearance: Direction
    setting_and_cast: Direction
    identity_frame: str = Field(default="", max_length=700)
    headline: str = Field(min_length=1, max_length=60)
    shots: list[BrandShot] = Field(min_length=9, max_length=14)

    @model_validator(mode="after")
    def complete_timeline(self):
        if len(set(self.product_assets)) != len(self.product_assets):
            raise ValueError("Use each selected product material at most once.")
        end_card = self.shots[-1]
        if end_card.kind != "product" or end_card.seconds != 4 or not end_card.product_in_first_frame:
            raise ValueError("The last shot must be a 4-second end card with the product visible.")
        if sum(shot.seconds for shot in self.shots) != DURATION:
            raise ValueError("The storyboard must total 54 seconds, including the 4-second end card.")
        if not any(shot.product_in_first_frame for shot in self.shots[:-1]):
            raise ValueError("Show the product in at least one filmed shot before the end card.")
        return self


class SceneState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: Short
    present: list[Short] = Field(max_length=6)
    facts: Direction


_EDIT_COMMAND = re.compile(
    r"切到|切回|切至|转场至|叠化到|\b(?:cut\s+(?:to|back|away|from|on)|match cut|dissolve to)\b"
    r"|\b(?:cắt\s+(?:sang|qua|về|lên|khi|theo|từ)|nối\s+(?:sang|theo|cùng|chuyển động))",
    re.I,
)


class ContinuityShot(BrandShot):
    before: Short
    after: Short
    first_cast: list[Short] = Field(max_length=6)
    last_cast: list[Short] = Field(max_length=6)
    last_frame: str = Field(default="", max_length=700)
    product_in_last_frame: bool
    transition: Literal["cut", "match", "ellipsis", "end_card"] = "cut"
    edit_note: str = Field(default="", max_length=320)

    @model_validator(mode="after")
    def camera_action(self):
        if _EDIT_COMMAND.search(self.action):
            raise ValueError("Move edit instructions out of action into edit_note. Keep a single camera setup in action.")
        if self.transition == "ellipsis" and not self.edit_note:
            raise ValueError("An ellipsis needs an edit_note that explains the change in time or place.")
        return self


class ContinuityPlan(BrandPlan):
    format: Literal["product-story-v3"] = CONTINUITY_FORMAT
    cast: dict[Short, Direction] = Field(max_length=6)
    locations: dict[Short, Direction] = Field(min_length=1, max_length=14)
    states: dict[Short, SceneState] = Field(min_length=1, max_length=28)
    shots: list[ContinuityShot] = Field(min_length=9, max_length=14)

    @model_validator(mode="after")
    def motivated_changes(self):
        for state in self.states.values():
            if state.location not in self.locations or not set(state.present) <= self.cast.keys():
                raise ValueError("Every state must use a declared location and cast.")
        for index, shot in enumerate(self.shots):
            final = index == len(self.shots) - 1
            if shot.before not in self.states or shot.after not in self.states:
                raise ValueError("Each before/after must refer to a declared state.")
            before, after = self.states[shot.before], self.states[shot.after]
            if not set(shot.first_cast) <= set(before.present) or not set(shot.last_cast) <= set(after.present):
                raise ValueError("Only actors already present in the frame's state may appear.")
            if shot.speaker and shot.speaker not in shot.first_cast:
                raise ValueError("The speaking actor must be visible at the start of their shot.")
            if shot.line and shot.seconds < 5:
                raise ValueError("Allow at least five seconds for takes with dialogue.")
            if final:
                if shot.transition != "end_card" or shot.last_frame or shot.first_cast or shot.last_cast or shot.before != shot.after:
                    raise ValueError("Keep the end card as one independent product still without actors.")
                continue
            if not shot.last_frame or shot.transition == "end_card" or before.location != after.location:
                raise ValueError("Film each shot in one place with an explicit final frame.")
            if index == 0:
                if shot.transition != "cut":
                    raise ValueError("Start the storyboard with a normal opening shot.")
                continue
            previous = self.shots[index - 1]
            if shot.transition != "ellipsis" and shot.before != previous.after:
                raise ValueError("Unchanged time requires the next shot to start from the exact previous end state.")
            if shot.transition == "match" and (
                set(shot.first_cast) != set(previous.last_cast)
                or shot.product_in_first_frame != previous.product_in_last_frame
            ):
                raise ValueError("A shared endpoint must keep the same visible actors and product.")
        return self


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare"] = "prepare"
    plan: ContinuityPlan


def repair_for(run, index, *, frames=False, kind=None):
    return next((repair for repair in reversed(run.get("brand_repairs") or [])
                 if index in repair["shots"] and (not frames or repair.get("kind") != "motion")
                 and (kind is None or repair.get("kind", "join") == kind)), {})


def repair_shots(plan, target):
    affected = [target]
    for shot in plan.shots[target:]:
        if shot.transition != "match":
            break
        affected.append(affected[-1] + 1)
    return affected


@lru_cache(maxsize=1)
def case():
    return json.loads((ASSETS / "case.json").read_text(encoding="utf-8"))


def filmed_count(plan: Plan | BrandPlan) -> int:
    return len(plan.shots) - 1 if isinstance(plan, BrandPlan) else FILMED_COUNT


def shots(plan: Plan | BrandPlan):
    if isinstance(plan, BrandPlan):
        locked = isinstance(plan, ContinuityPlan)
        return [
            {"id": index, "title": shot.scene, "frame": shot.frame, "prompt": shot.action,
             "frames": shot.seconds * FPS, "preferred": (0, shot.seconds * FPS),
             "generate_seconds": max(5, shot.seconds if locked else shot.seconds + 1), "speaker": shot.speaker,
             "spoken_line": shot.line or None, "language": plan.language,
             "product_visible": shot.product_in_first_frame, "adaptive": True,
             **({"locked": True, "last_frame": shot.last_frame} if locked else {})}
            for index, shot in enumerate(plan.shots, 1)
        ]
    lines = {1: plan.opening, 2: plan.answer, 14: plan.invitation}
    return [{**shot, "spoken_line": lines.get(index), "language": plan.language}
            for index, shot in enumerate(case()["shots"], 1)]


_PREVIEW = {
    "en": (
        "54 sec · 21 shots", "Product", "End card",
        ("Dad is missing from the album", "Dad says he took the photo", "Pick up the phone",
         "Arrive at the bakery", "Wait for the cake", "Place a strawberry", "Photograph the cake",
         "Overhead of the cake", "Bring the cake home", "Light the candle", "Make a wish",
         "Dad takes the photo", "The daughter notices", "Ask Dad to come closer", "Hand over the phone",
         "Laugh together", "Shutter and freeze frame", "The same photo on the phone", "Rear camera",
         "Phone edges", "Product and end card"),
    ),
    "vi": (
        "54 giây · 21 cảnh", "Sản phẩm", "Cuối phim",
        ("Ba vắng mặt trong album", "Ba nói mình là người chụp", "Cầm điện thoại", "Đến tiệm bánh",
         "Chờ trang trí bánh", "Đặt dâu lên bánh", "Chụp ảnh chiếc bánh", "Góc nhìn từ trên xuống",
         "Mang bánh về nhà", "Thắp nến", "Nhắm mắt ước", "Ba đứng ngoài khung hình",
         "Con gái nhìn sang ba", "Mời ba lại gần", "Chuyền điện thoại", "Hai ba con cùng cười",
         "Bấm máy và giữ ảnh", "Xem đúng tấm ảnh trên điện thoại", "Camera sau",
         "Viền điện thoại", "Sản phẩm và câu kết"),
    ),
}


def display_script(plan: Plan | BrandPlan) -> str:
    if isinstance(plan, BrandPlan):
        units, product = {"zh": ("秒", "产品"), "en": ("sec", "Product"), "vi": ("giây", "Sản phẩm")}[plan.language]
        summary = [f"《{plan.title}》 · 54 {units}", f"{product} · {plan.product}", plan.setting_and_cast]
        cursor = 0
        for index, shot in enumerate(plan.shots, 1):
            end = cursor + shot.seconds
            summary.append(f"\n{index:02d} · {cursor:02d}–{end:02d} · {shot.scene}")
            summary.extend((shot.frame, shot.action))
            if isinstance(shot, ContinuityShot):
                tail_label, edit_label = {"zh": ("尾帧", "接镜"), "en": ("Last frame", "Editing"),
                                          "vi": ("Khung cuối", "Dựng nối")}[plan.language]
                if shot.last_frame:
                    summary.append(f"{tail_label} · {shot.last_frame}")
                if shot.edit_note:
                    summary.append(f"{edit_label} · {shot.edit_note}")
            if shot.line:
                summary.append(f"{shot.speaker} · 「{shot.line}」")
            cursor = end
        return "\n".join([*summary, "", f"{END_CARD[plan.language]} · {plan.product}「{plan.headline}」"])
    duration, product, end_card, labels = _PREVIEW.get(plan.language, ("54 秒 · 21 镜", "产品", "片尾", ()))
    summary = [f"《{plan.title}》 · {duration}", f"{product} · {plan.product}"]
    cursor = 0
    for index, shot in enumerate(shots(plan), 1):
        end = cursor + shot["frames"]
        line = f" 「{shot['spoken_line']}」" if shot["spoken_line"] else ""
        label = labels[index - 1] if labels else shot["title"]
        summary.append(f"{index:02d} · {cursor / FPS:05.2f}–{end / FPS:05.2f} · {label}{line}")
        cursor = end
    summary.append(f"{end_card} · {plan.product}「{plan.headline}」")
    return "\n".join(summary)


def final_request(cuts, language="zh", *, plan=None, end_card=None):
    adaptive = isinstance(plan, BrandPlan)
    return {
        "recipe": plan.format if adaptive else FORMAT,
        "duration_sec": DURATION,
        "frames": FRAME_COUNT,
        **({"language": language} if language != "zh" else {}),
        "timeline": {"shots": [{"asset": cut["source"], "in_sec": cut["start"] / FPS,
                                "duration_sec": cut["frames"] / FPS, "speed": cut["speed"],
                                **({"output_frames": cut["output_frames"], "cues": cut["cues"]} if adaptive else {}),
                                **({"dub": cut["dub"], "cues": cut["cues"]} if "dub" in cut else {})}
                               for cut in cuts]},
        "post_shots": ["end-card"] if adaptive else ["shutter", "same-photo-on-phone", "rear", "rail", "end-card"],
        **({"storyboard": plan.model_dump(), "end_card": end_card} if adaptive else {}),
    }


def is_complete_cut(run, job):
    cuts = run.get("wan_cuts") or []
    key = run.get("wan_final_key")
    result = job.result_data or {}
    saved = run.get("remake_adaptation") or {}
    request = job.request_data or {}
    language = saved.get("language", "zh")
    plan_type = {ADAPTIVE_FORMAT: BrandPlan, CONTINUITY_FORMAT: ContinuityPlan}.get(saved.get("format"))
    plan = plan_type.model_validate(saved) if plan_type else None
    count = filmed_count(plan)
    end_card = request.get("end_card")
    expected = final_request(cuts, language, plan=plan, end_card=end_card) if len(cuts) == count else None
    dialogue = 3
    if plan:
        if not end_card or any(
            cut.get("output_frames") != shot.seconds * FPS
            or [cue["text"] for cue in cut.get("cues", [])] != ([shot.line] if shot.line else [])
            for shot, cut in zip(plan.shots[:-1], cuts)
        ):
            return False
        dialogue = sum(bool(shot.line) for shot in plan.shots)
    if isinstance(plan, ContinuityPlan):
        review = (run.get("director_decisions") or {}).get(f"{VISUAL_REVIEW}:joins") or {}
        boards = [cut.get("review_board") for cut in cuts]
        if (not all(boards) or review.get("cut_assets") != boards
                or review.get("end_card") != end_card or not review.get("approved")):
            return False
    return bool(
        expected and key and job.idempotency_key == key and job.kind == "compose"
        and job.status == "completed" and job.output_asset_id
        and [cut.get("shot") for cut in cuts] == list(range(1, count + 1))
        and len({cut["source"] for cut in cuts}) == count
        and {name: request.get(name) for name in expected} == expected
        and (language == "zh" or result.get("language") == language)
        and {name: result.get(name) for name in ("frames", "fps", "duration_sec", "has_audio", "dialogue")}
        == {"frames": FRAME_COUNT, "fps": FPS, "duration_sec": DURATION, "has_audio": True, "dialogue": dialogue}
    )
