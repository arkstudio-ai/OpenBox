"""Reproducible storyboard for the approved 30-second OpenBox example."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from light import commercial_plan, product_story_plan, shortcut_plan, silk_road, video_language, wan_story
from light.production_promotion import GUIDANCE as PROMOTION_GUIDANCE
from tool.video_runninghub import MODEL

TEMPLATE = "twist"
ASSETS = Path(__file__).with_name("remake_assets") / "this-order"

RESOLUTION = "768p"
PREVIOUS_VERSION = "this-order-20261118-v1"


@lru_cache(maxsize=1)
def manifest() -> dict:
    return json.loads((ASSETS / "case.json").read_text(encoding="utf-8"))


class Adaptation(BaseModel):
    """Only the story's product and copy slots are supplied by the Agent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    language: video_language.Language = "zh"
    product: Annotated[str, StringConstraints(min_length=1, max_length=36)] = "麻辣小龙虾"
    appearance: Annotated[str, StringConstraints(max_length=260)] = ""
    reveal_copy: Annotated[str, StringConstraints(max_length=55)] = ""
    taste_copy: Annotated[str, StringConstraints(max_length=50)] = ""
    end_copy: Annotated[str, StringConstraints(max_length=44)] = ""

    def localized(self, language: video_language.Language):
        product = self.product
        if product == "麻辣小龙虾":
            product = {"zh": product, "en": "Spicy crawfish", "vi": "Tôm hùm đất cay"}[language]
        return self.model_copy(update={"language": language, "product": product})


_COPY = {
    "vi": {
        "title": "Ai trả tiền bữa này?", "seconds": "giây", "scenes": "cảnh", "product": "Sản phẩm",
        "beats": ("Đồng nghiệp đẩy hóa đơn cho người mới", "Quản lý lên tiếng bênh người mới",
                  "Quản lý mời cả bàn, món ăn được dọn ra", "Vũ nếm miếng đầu tiên",
                  "Hôm sau Vũ hỏi lại về hóa đơn", "Quản lý báo tin và nhắc đôi đũa",
                  "Cận cảnh món ăn, hai đôi đũa và tên quán"),
        "speakers": ("Đồng nghiệp", "Quản lý", "Quản lý", "Vũ", "Vũ", "Quản lý", ""),
        "dialogue": ("Vũ, người mới trả tiền nhé.", "Ai bảo người mới phải trả tiền?",
                     "Bữa này tôi mời. Ăn lúc còn nóng nhé.", "Để tôi nếm thử.",
                     "Chị ơi, bữa tối qua hết bao nhiêu?", "Em được nhận chính thức rồi! Lần sau mang hai đôi đũa nhé.", ""),
        "treat": "Bữa này tôi mời.",
    },
    "en": {
        "title": "Who’s paying?", "seconds": "sec", "scenes": "scenes", "product": "Product",
        "beats": ("A coworker slides the bill to the new hire", "The manager steps in",
                  "The manager treats the table and the dish arrives", "Wu takes the first bite",
                  "Wu asks about the bill the next day", "The manager shares the news and mentions the chopsticks",
                  "Two pairs of chopsticks, the dish and the shop name"),
        "speakers": ("Coworker", "Manager", "Manager", "Wu", "Wu", "Manager", ""),
        "dialogue": ("Wu, the new hire pays, right?", "Who says the new hire has to pay?",
                     "This one’s my treat. Eat it while it’s hot.", "Let me try it.",
                     "How much was dinner last night?", "You got the job! Bring two pairs of chopsticks next time.", ""),
        "treat": "This one’s my treat.",
    },
}


def title(language):
    return _COPY[language]["title"] if language in _COPY else manifest()["title"]


def applies(entry: str | None, form: dict) -> bool:
    return entry == "video" and form.get("template") in (TEMPLATE, *wan_story.TEMPLATES)


def version_for(form: dict) -> str:
    return wan_story.VERSION if form.get("template") in wan_story.TEMPLATES else manifest()["version"]


def tool_for(form: dict) -> str:
    return "light_wan_story" if form.get("template") in wan_story.TEMPLATES else "light_remake"


def job_versions():
    return (manifest()["version"], PREVIOUS_VERSION, *wan_story.VERSIONS)


def owns_job(job) -> bool:
    key = getattr(job, "idempotency_key", None) or ""
    return key.startswith("light:") and any(f":{version}:" in key for version in job_versions())


def reference_copy(form: dict) -> str:
    if "reference_copy" in form:
        return form["reference_copy"] or ""
    # Existing typed drafts can be used as reference. The old generated
    # four-shot screenplay must not come back as the user's instructions.
    text = str(form.get("script") or "")
    return "" if any(marker in text for marker in ("镜头1", "spoken_line", "【方案】")) else text


def input_fingerprint(form: dict) -> str:
    snapshot = {key: form[key] for key in ("shop", "brief", "asset_ids")}
    snapshot.update(reference_copy=reference_copy(form), template_version=version_for(form))
    if form.get("template") in {wan_story.SODA_TEMPLATE, silk_road.TEMPLATE, shortcut_plan.TEMPLATE, product_story_plan.TEMPLATE}:
        snapshot["template"] = form["template"]
    if form.get("template") == silk_road.TEMPLATE:
        snapshot.update(product_name=form.get("product_name", ""), frame_plan=1)
    elif form.get("template") == product_story_plan.TEMPLATE:
        snapshot.update(product_name=form.get("product_name", ""), storyboard=commercial_plan.FORMAT,
                        planning_revision=commercial_plan.PLANNING_REVISION)
    if promotion := form.get("promotion_brief"):
        snapshot["promotion_brief"] = promotion
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def storyboard(adaptation: Adaptation) -> list[dict]:
    reveal = adaptation.reveal_copy.strip("。！？!?… ")
    taste = adaptation.taste_copy.strip("。！？!?… ")
    lines = {3: f"这顿我请。{reveal}。" if reveal else "这顿我请。趁热吃。",
             4: f"{taste}。" if taste else "那我尝尝。"}
    localized = _COPY.get(adaptation.language)
    if localized:
        lines = dict(enumerate(localized["dialogue"], 1))
        if reveal:
            lines[3] = f"{localized['treat']} {adaptation.reveal_copy}"
        if taste:
            lines[4] = adaptation.taste_copy
    shots = []
    for shot in manifest()["shots"]:
        index = shot["shot"]
        item = {**shot, "spoken_line": lines.get(index, shot["spoken_line"])}
        if localized:
            item.update(beat=localized["beats"][index - 1], speaker=localized["speakers"][index - 1],
                        language=adaptation.language)
        for field in ("first_frame", "last_frame", "motion"):
            if field in item:
                item[field] = item[field].replace("{product}", adaptation.product)
        shots.append(item)
    return shots


def display_script(adaptation: Adaptation) -> str:
    language = adaptation.language
    labels = _COPY.get(language, {"seconds": "秒", "scenes": "段", "product": "商品"})
    seconds = labels["seconds"]
    lines = [f'《{title(language)}》 · 30 {seconds} · 7 {labels["scenes"]}',
             f'{labels["product"]}：{adaptation.product}', ""]
    for shot in storyboard(adaptation):
        lines.append(f'{shot["from_sec"]}–{shot["to_sec"]} {seconds} · {shot["beat"]}')
        if shot["speaker"]:
            lines.append(f'{shot["speaker"]}：「{shot["spoken_line"]}」')
        else:
            lines.append(f'{video_language.END_CARD[language]}：「{adaptation.end_copy or adaptation.product}」')
        lines.append("")
    return "\n".join(lines).strip()


def planning_instruction(form: dict, action: str) -> str:
    if form.get("template") in wan_story.TEMPLATES:
        return wan_story.planning_instruction(form, action)
    data = {"shop": form["shop"], "selected_materials": form["asset_ids"], "brief": form["brief"],
            "user_reference_copy": reference_copy(form)}
    guidance = ""
    if promotion := form.get("promotion_brief"):
        data["promotion_brief"] = promotion
        guidance = PROMOTION_GUIDANCE
    action_line = (
        "只保存七段分镜供用户查看，不生成媒体。" if action == "prepare" else
        "用户已确认制作。以保存的七段分镜生成；如果素材或参考文案刚变更，基于最新输入填写适配字段。"
    )
    language = (form.get("run") or {}).get("video_language", "zh")
    return (
        video_language.direction(language) + "\n"
        "选定样片《这单谁买》，调用 light_remake（需要时用 capability_search 查找）。"
        "同事推账单 → 主管当面解围 → 空桌上只端进一份商品 → 当场试吃 → 次日追问账单 → 转正与两双筷子 → 菜品和店名。"
        "七段依次为4、4、5、4、4、6、3秒。人物与谁交谈、何时揭晓商品、结尾店名由服务端落实。"
        "根据选中素材识别真实商品与外观。用户输入是创作参考，将能从素材支持的内容自然适配到第3段上菜的reveal_copy、"
        "第4段品尝的taste_copy或片尾的end_copy。对白应短，能在镜头内说完。片尾用简短词组写菜品或吃法，店名单独排版。"
        "不杜撰折扣、价格、功效或店名。"
        f"{guidance}{action_line} 如果工具报告制作暂停，告知用户返回制作页重试。用户输入：{json.dumps(data, ensure_ascii=False)}"
    )
