"""The director's shot list; execution and editing are deliberately separate."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Short = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)
]
Direction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)
]


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scene: Short
    kind: Literal["scene", "detail", "product"] = "scene"
    optional: bool | None = None
    product_in_first_frame: bool | None = None
    seconds: int = Field(ge=3, le=8)
    frame: Direction
    action: Direction
    speaker: str = Field(default="", max_length=60)
    line: str = Field(default="", max_length=24)

    @property
    def can_omit(self):
        return not self.line and self.kind != "product" and (
            self.optional if self.optional is not None else self.kind == "detail"
        )

    @model_validator(mode="after")
    def consistent_speech(self):
        if bool(self.speaker) != bool(self.line):
            raise ValueError(
                "speaker and line must either both be present or both be empty"
            )
        if self.kind != "scene" and self.line:
            raise ValueError("product/detail shots contain no dialogue")
        return self


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    format: Literal["shot-list-v2"] = "shot-list-v2"
    title: Short
    product: Short
    appearance: Direction
    setting_and_cast: Direction
    identity_frame: Direction
    headline: Annotated[str, StringConstraints(min_length=2, max_length=10)]
    shots: list[Shot] = Field(min_length=5, max_length=12)

    @model_validator(mode="after")
    def whole_story(self):
        if self.shots[-1].kind != "product":
            raise ValueError("finish with a separate product shot")
        if not 20 <= sum(s.seconds for s in self.shots) <= 75:
            raise ValueError("the complete source footage must be 20-75 seconds")
        return self


def display_script(plan: ShotPlan) -> str:
    lines = [f"《{plan.title}》", plan.setting_and_cast]
    for shot in plan.shots:
        lines.extend(["", shot.action])
        if shot.line:
            lines.append(f"{shot.speaker}：「{shot.line}」")
    return "\n".join([*lines, "", f"片尾 · {plan.product}「{plan.headline}」"])


DIRECTING = (
    "你是这条竖屏商业短片的导演。根据所选真实商品、店名和用户参考文案，创作一条约25到30秒、可以独立观看的广告。"
    "模板样片提供广告节奏与宣传字的风格参考。自行决定题材、人物、地点、叙事结构、对白、是否跨场以及如何展示商品。"
    "用户文案是创意参考，可以改编或采用纯视觉叙事。保留明确指定的事实、商品与品牌文字，不虚构优惠或功效。"
    "使用shot-list-v2给出完整可拍分镜。5至12个短镜头，每镜3至8秒原素材，可留出动作、对白和剪辑余量。"
    "每镜填写scene、kind、frame、action、seconds，product_in_first_frame明确标记商品在动作开始前是否可见；有对白的scene同时给speaker和line，一镜至多一句24字。"
    "同一空间使用相同scene。setting_and_cast与identity_frame记录本片实际选定的场景、演员身份和商品视觉特征，供不同机位保持一致。"
    "frame只正向描述开拍前可见的物品、构图和人物位置，不写之后的动作和画外物件。action描述主要动作和视线。可以将删掉仍能看懂的无台词镜头标optional。"
    "kind=detail为商品、环境或道具插镜。末镜kind=product是独立商品画面，预留可读题字的位置，headline由后期排版。"
    "剪点与修正方式由剪辑导演看实拍后决定。首帧用配置的图片模型，Wan 3.0拍摄，IMS合成。"
)
