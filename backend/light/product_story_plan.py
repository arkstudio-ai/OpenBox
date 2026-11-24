"""The 54-second birthday-story template and its delivery contract."""

from functools import lru_cache
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from light.video_language import Language

TEMPLATE = "product_story"
FORMAT = "product-story-v1"
ASSETS = Path(__file__).with_name("remake_assets") / "product-story"
FPS = 24
FRAME_COUNT = 1296
DURATION = FRAME_COUNT / FPS
FILMED_COUNT = 16
CREDITS = "Dream Culture — Kevin MacLeod (incompetech.com), CC BY 4.0. Excerpted and mixed."

DIRECTING = (
    "「品牌剧情广告」是横屏54秒的生日故事模板，参考耳机广告的场景衔接、道具交接与商品亮相。"
    "从旧相册里缺席的爸爸开场，经过买蛋糕和生日许愿，让女儿邀请爸爸一起自拍，"
    "用真实自拍帧做回看，最后给手机后摄、边框和正反面展示。"
    "没有自定义内容时可省略plan，载入已验证的21镜。用户给出片名、片尾或逐字台词时，"
    "逐字填入相应字段，按本次成片语言填写其他可见文字。三句台词应分别短到能在3.5、2.2和4秒内自然说完。"
    "其他手机由用户选用自己的产品图。可根据文案填写产品名、可见外观、片名、片尾短句和三句对白，"
    "三个说话人依次为女儿、父亲、女儿。不要添加型号规格、功效和价格。"
    "有非手机或完全不同剧情的需求时，说明该拍法是生日合照模板，再保存已有素材可支持的改编。"
)


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


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare"] = "prepare"
    plan: Plan | None = None


@lru_cache(maxsize=1)
def case():
    return json.loads((ASSETS / "case.json").read_text(encoding="utf-8"))


def shots(plan: Plan):
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


def display_script(plan: Plan) -> str:
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


def final_request(cuts, language="zh"):
    return {
        "recipe": FORMAT,
        "duration_sec": DURATION,
        "frames": FRAME_COUNT,
        **({"language": language} if language != "zh" else {}),
        "timeline": {"shots": [{"asset": cut["source"], "in_sec": cut["start"] / FPS,
                                "duration_sec": cut["frames"] / FPS, "speed": cut["speed"],
                                **({"dub": cut["dub"], "cues": cut["cues"]} if "dub" in cut else {})}
                               for cut in cuts]},
        "post_shots": ["shutter", "same-photo-on-phone", "rear", "rail", "end-card"],
    }


def is_complete_cut(run, job):
    cuts = run.get("wan_cuts") or []
    key = run.get("wan_final_key")
    result = job.result_data or {}
    language = (run.get("remake_adaptation") or {}).get("language", "zh")
    expected = final_request(cuts, language) if len(cuts) == FILMED_COUNT else None
    request = job.request_data or {}
    return bool(
        expected and key and job.idempotency_key == key and job.kind == "compose"
        and job.status == "completed" and job.output_asset_id
        and [cut.get("shot") for cut in cuts] == list(range(1, FILMED_COUNT + 1))
        and len({cut["source"] for cut in cuts}) == FILMED_COUNT
        and {name: request.get(name) for name in expected} == expected
        and (language == "zh" or result.get("language") == language)
        and {name: result.get(name) for name in ("frames", "fps", "duration_sec", "has_audio", "dialogue")}
        == {"frames": FRAME_COUNT, "fps": FPS, "duration_sec": DURATION, "has_audio": True, "dialogue": 3}
    )
