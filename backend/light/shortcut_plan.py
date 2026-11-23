"""A seven-beat ad that lets the product explain the change of approach."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from light.reference_plan import Direction, Short, Shot, ShotPlan

TEMPLATE = "shortcut_story"
DURATIONS = (5, 4, 3, 5, 5, 3, 5)
OPENING = 1
REVEAL = 5
PRODUCT = 6
HANDOFF = REVEAL - 1
SETUP_ROLES = {OPENING: "opening", HANDOFF: "handoff", REVEAL: "reveal"}
ENDPOINT_SHOTS = (*SETUP_ROLES, 7)
DIALOGUE_SHOTS = (OPENING, REVEAL, 7)
REVIEWED_SHOTS = (OPENING, 2, 3, HANDOFF, REVEAL)

DIRECTING = (
    "你是手机「做一条视频」的广告导演。根据实际素材、店名和用户创意，拍一条约30秒的竖屏广告。"
    "两种办法服务同一目标，第一种费劲，第二种由这个真实商品或服务实现。"
    "保留叙事作用，人物、年代、场景、动作、口头语和轻松道具都随用户素材改变。"
    "先给三人各取一个始终不变、包含醒目服装的简短演员标识，写入setting_and_cast。"
    "给两种办法可一眼辨认的取景区，哪怕发生在同一店铺，也用不同前景、景别或光区把两路分开。"
    "按顺序写完整七镜：①在对话正在发生时开镜，一句自然对白向同伴说清共同目标和自己折腾的选择。"
    "指定画内接话人listener，说话人与接话人的身体和视线互相接住。第三人自然看他们或使用自己的道具。"
    "尾帧停在这次交流的自然反应，三人不看镜头、不集体转头，也不看向还没交代的路线。"
    "离开、拿工具和看路线从第2镜开始；②两人踏上费劲的路线；"
    "③拍一个在手机上可一眼认出的动作或结果，用身体、工具和场景交代费劲，避免只能靠台词或微小零件看懂；"
    "④两名辛苦者停下并望向画外同一侧，拍脸和眼神，堂食桌、从容者和成品保持画外；"
    "⑤切到另一机位和不同景别的单人反打，独拍从容者和已经实现的结果，"
    "从容者视线与第4镜互相接住，用开场那个唯一的道具或手势完成回扣；"
    "⑥给商品独立特写，预留上方留白，菜名和店名后期加字；"
    "⑦原先辛苦的人已经加入使用或坐下，给一个清楚的选择或一句简短对白当笑点。"
    "镜头seconds按5、4、3、5、5、3、5填。第6镜kind=product，其余用scene或detail。"
    "前四镜product_in_first_frame=false：可见原料和准备道具，成品解决方案直到第5镜才露脸。"
    "第5至7镜product_in_first_frame=true。每镜填写camera（机位、景别和是否移动）及visible_roles（逐字引用演员标识，"
    "无人物填空数组），有演员主动作时把实际执行人填入performer，不能只写代词。"
    "第1、7镜显出三人；第4镜只有费劲的两人；第5镜只有从容者，他是performer；第6镜无人。"
    "首尾帧分别描述同一机位可见的起止静态画面。只给第1、4、5、7镜填写last_frame，第2、3、6镜省略该项。"
    "第4镜尾帧明确两人望向画外的同一点，第5镜首帧明确从对面方向相接。"
    "相邻镜头接续同一件事时，写清前镜结果如何出现在后镜。每镜只设计一个清楚的主动作。"
    "第1、7镜各可有一句短对白。第5镜优先用无对白的从容动作揭晓，留出看清商品的时间。"
    "其余用可辨的同期动作声，一镜至多一人说话。"
    "以真实产品为准，保留用户明确指定的事实，避免虚构价格和功效。"
    "主体都放在竖屏安全区内，写清视线、站位、人物辨识特征，重复道具要前后一致。"
    "不要让任何解释、教程、文字、字幕、品牌水印或配乐进入模型拍摄画面。"
)


class ShortcutShot(Shot):
    camera: Direction | None = None
    visible_roles: list[Short] | None = Field(default=None, max_length=3)
    performer: Short | None = None
    listener: Short | None = None
    last_frame: Direction | None = None


class ShortcutPlan(ShotPlan):
    shots: list[ShortcutShot] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def whole_story(self):
        """The preview and production must preserve the reveal and comic ending."""
        for index, shot in enumerate(self.shots, 1):
            if (shot.kind == "product") != (index == PRODUCT):
                raise ValueError("第6镜是独立商品特写，其他镜头是剧情或细节。")
            if shot.product_in_first_frame is not (index >= REVEAL):
                raise ValueError("前四镜不露成品，第5、6、7镜首帧要看得到商品。")
            # Previously saved storyboards used only a first frame for shot 4.
            if index != HANDOFF and bool(shot.last_frame) != (index in ENDPOINT_SHOTS):
                raise ValueError("只为第1、4、5、7镜填写 last_frame。")
            if shot.line and index not in DIALOGUE_SHOTS:
                raise ValueError("对白只能写在第1、5、7镜。")
            if shot.seconds != DURATIONS[index - 1]:
                raise ValueError("七镜时长依次为5、4、3、5、5、3、5秒。")
        return self


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare"] = "prepare"
    plan: ShortcutPlan

    @model_validator(mode="after")
    def filmable_setups(self):
        shots = self.plan.shots
        for index, shot in enumerate(shots, 1):
            if not shot.camera or shot.visible_roles is None:
                raise ValueError(f"第{index}镜需要 camera 和 visible_roles，无人物时填空数组。")
            if len(set(shot.visible_roles)) != len(shot.visible_roles):
                raise ValueError(f"第{index}镜的 visible_roles 有重复演员。")
            if shot.performer and shot.performer not in shot.visible_roles:
                raise ValueError(f"第{index}镜的表演者必须在 visible_roles 中。")
        handoff, reveal = shots[HANDOFF - 1], shots[REVEAL - 1]
        if not handoff.last_frame or len(handoff.visible_roles) != 2:
            raise ValueError("第4镜只拍两名辛苦者，尾帧清楚交代共同视线。")
        if not reveal.performer or reveal.visible_roles != [reveal.performer]:
            raise ValueError("第5镜只拍从容者，由这一人完成回扣动作。")
        cast = {*handoff.visible_roles, reveal.performer}
        if len(cast) != 3 or any(set(shots[i].visible_roles) != cast for i in (0, 6)):
            raise ValueError("第1、7镜同框三人；第4镜辛苦的两人和第5镜从容者须用相同演员标识。")
        opening = shots[OPENING - 1]
        if opening.line and (opening.speaker not in opening.visible_roles
                             or opening.listener not in opening.visible_roles
                             or opening.listener == opening.speaker):
            raise ValueError("第1镜有对白时，指定不同的画内 speaker 和 listener。")
        if shots[PRODUCT - 1].visible_roles:
            raise ValueError("第6镜只拍商品。")
        return self
