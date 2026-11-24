"""The 54-second birthday-story template and its delivery contract."""

from functools import lru_cache
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    "若空白文案或确认小米18《这一张，有你》，调工具省略plan，载入已验证的21镜。"
    "其他手机由用户选用自己的产品图。可根据文案填写产品名、可见外观、片名、片尾短句和三句对白，"
    "三个说话人依次为女儿、父亲、女儿。不要添加型号规格、功效和价格。"
    "有非手机或完全不同剧情的需求时，说明该拍法是生日合照模板，再保存已有素材可支持的改编。"
)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    format: Literal["product-story-v1"] = FORMAT
    title: str = Field(default="这一张，有你", min_length=1, max_length=20)
    product: str = Field(default="小米 18", min_length=1, max_length=20)
    appearance: str = Field(default="", max_length=320)
    headline: str = Field(default="这一张，有你。", min_length=1, max_length=20)
    opening: str = Field(default="爸，怎么又没你？", min_length=1, max_length=16)
    answer: str = Field(default="我在拍啊。", min_length=1, max_length=16)
    invitation: str = Field(default="爸，往我这边一点。", min_length=1, max_length=16)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare"] = "prepare"
    plan: Plan | None = None


@lru_cache(maxsize=1)
def case():
    return json.loads((ASSETS / "case.json").read_text(encoding="utf-8"))


def shots(plan: Plan):
    lines = {1: plan.opening, 2: plan.answer, 14: plan.invitation}
    return [{**shot, "spoken_line": lines.get(index)}
            for index, shot in enumerate(case()["shots"], 1)]


def display_script(plan: Plan) -> str:
    summary = [f"《{plan.title}》 · 54 秒 · 21 镜", f"产品：{plan.product}"]
    cursor = 0
    for index, shot in enumerate(shots(plan), 1):
        end = cursor + shot["frames"]
        line = f" 「{shot['spoken_line']}」" if shot["spoken_line"] else ""
        summary.append(f"{index:02d} · {cursor / FPS:05.2f}–{end / FPS:05.2f} 秒 · {shot['title']}{line}")
        cursor = end
    summary.append(f"片尾 · {plan.product}「{plan.headline}」")
    return "\n".join(summary)


def final_request(cuts):
    return {
        "recipe": FORMAT,
        "duration_sec": DURATION,
        "frames": FRAME_COUNT,
        "timeline": {"shots": [{"asset": cut["source"], "in_sec": cut["start"] / FPS,
                                "duration_sec": cut["frames"] / FPS, "speed": cut["speed"]}
                               for cut in cuts]},
        "post_shots": ["shutter", "same-photo-on-phone", "rear", "rail", "end-card"],
    }


def is_complete_cut(run, job):
    cuts = run.get("wan_cuts") or []
    key = run.get("wan_final_key")
    result = job.result_data or {}
    expected = final_request(cuts) if len(cuts) == FILMED_COUNT else None
    request = job.request_data or {}
    return bool(
        expected and key and job.idempotency_key == key and job.kind == "compose"
        and job.status == "completed" and job.output_asset_id
        and [cut.get("shot") for cut in cuts] == list(range(1, FILMED_COUNT + 1))
        and len({cut["source"] for cut in cuts}) == FILMED_COUNT
        and {name: request.get(name) for name in expected} == expected
        and {name: result.get(name) for name in ("frames", "fps", "duration_sec", "has_audio", "dialogue")}
        == {"frames": FRAME_COUNT, "fps": FPS, "duration_sec": DURATION, "has_audio": True, "dialogue": 3}
    )
