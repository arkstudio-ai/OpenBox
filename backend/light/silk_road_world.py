"""Plan a new story around the reference film's camera and edit."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Take(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    setting: str = Field(min_length=1, max_length=145)
    title: str = Field(default="", max_length=38, description="Short action label for the App's shot card.")
    action: str = Field(min_length=1, max_length=420)
    first_frame: str = Field(default="", max_length=320, description="Opening still. Specify composition, actor and wardrobe, gaze, and the exact state of each prop.")
    last_frame: str = Field(default="", max_length=320, description="Closing still. Specify what changed in the action and preserve scene, clothes and prop continuity.")
    location_material: int | None = Field(default=None, ge=1, le=10, description="Optional one-based environmental photo for this scene.")


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=2, max_length=36)
    subtitle: str = Field(min_length=1, max_length=52)


class World(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    premise: str = Field(min_length=1, max_length=185, description="The new story and relationship, only for the app preview.")
    look: str = Field(min_length=1, max_length=140, description="Period and overall photography for cast preparation. Describe each location and its light in the relevant take.")
    cast: str = Field(min_length=1, max_length=280, description="The fixed appearance and wardrobe, with short names for the principals.")
    product_material: int = Field(default=1, ge=1, le=10, description="One-based position of the dish image or video.")
    cast_material: int | None = Field(default=None, ge=1, le=10, description="One-based position of an explicitly supplied cast reference.")
    takes: list[Take] = Field(min_length=9, max_length=9)
    chapters: list[Chapter] = Field(min_length=7, max_length=7)


CAMERA = (
    "暖色开场，低机位从商品局部推到人脸和第一口，利用主体遮挡衔接。",
    "暖色场景，侧向跟拍，路人擦过前景，停在柜边或同类位置。",
    "短正面跟拍接侧面宽景，主体从低位起动，朝右继续。",
    "明亮户外，越肩切人物近脸，中间保持物件与人物视线方向。",
    "由人物和手里道具向下摇拍本段指定的反应，再回到人物的短停和表情。",
    "冷青柔光，同场双人横向构图、短正反打、揭开或递出商品时的细部特写。",
    "近拍同一份商品和手，硬切品尝反应，再硬切送别挥手。",
    "冷色宽景，交通工具向纵深离开，保留广角和动势。",
    "第六段的紧反打插镜，主角先向下注视礼物，再轻抬眼。",
)

DIRECTING = (
    "选定《丝路留香》样片，沿用九段运镜、构图、转场和舒缓到反转的剪辑节拍。"
    "先辨识用户所选素材的真实商品、店内材质和时代。始终填写 world，仅有菜品图时也规划清楚本片人物和空间。"
    "根据素材和用户的参考文案改写具体人物、空间、道具与剧情动机。不要用古装替换真实的现代店面。"
    "没有明确的历史题材需求时，背景群演及服装都按日常现代场景处理。"
    "world 内的 premise 只给用户预览。look 写时代和摄影质感，供定妆使用。每段 setting 写该段时代、地点、光线。"
    "cast 只写固定演员的名字、外观、发型和穿着。场景、路人、动物以及道具写进实际出现的 take。"
    "每个 take 都填写 first_frame 和 last_frame 的独立静帧描述。写出景别、人物站位、视线、服装和可见道具，"
    "并交代同一场所、光线和道具在动作前后的变化。action 只写两张图之间的连续动作。"
    "跨时代改编时，逐段写明新场景的服装、交通工具、道具和背景。近景和反打也沿用这一段的设定。"
    "每段 title 用简短动宾短语说清主要动作，给用户在片段卡里辨认。"
    "素材按表单顺序从1编号。product_material 选真实菜品。每段 take 的 location_material 只选属于本段地点的真实店内或外景参考。cast_material 只在明确提供演员定妆图时填。"
    "段号与拍法：" + " ".join(f"{index}. {text}" for index, text in enumerate(CAMERA, 1)) +
    "第6和9段是同场的递礼与反打。第9段发生在第6段掀盖后，首尾都只拍收礼人的脸和肩膀。第7段保留试吃到挥手的硬切。"
    "第5段保留由人物摇向反应再返回的节奏，按素材选择合适的人、环境或动物作反应，动物只出现在需要的段。"
    "第6段首帧是交接前仍合盖的餐盒，尾帧展示打开后的同一份商品。第7段取食的是第6或9段的收礼人。"
    "chapters 按开场、起因、误会、首试、反应、揭晓、回扣列七组简短宣传用字，避免把动作标签写成题字。"
    "章题、副句和片尾短句只写剧情或情绪，完整店名和菜名由成片排版加入。"
    "不得把整片情节写进单镜拍摄提示。开场与揭晓使用真实商品，误会段保持道具一致。"
    "静音拍摄，题字在剪辑时加，码头/站台等冷色段保持与前面暖色段的反差。"
    "九段各拍一次，出片后的首尾帧修改、补拍和重新合成由用户在 App 中发起。"
    "填写独立菜名、真实外观、合适的一口和片尾短句。不要杜撰价格或促销。"
)


# Product exposure is fixed by the beat, while people, props and the dish come
# from the approved plan. The gift stays covered through its first frame.
PRODUCT_FRAMES = {1: {"first_frame", "last_frame"}, 6: {"last_frame"}, 7: {"first_frame"}}


def endpoint_prompt(plan, index, role, *, has_cast, has_location, has_product):
    take = plan.world.takes[index - 1]
    first = role == "first_frame"
    moment = take.first_frame if first else take.last_frame
    if not moment:
        moment = ("取动作开始前的静止画面：" if first else "取动作完成后落定的静止画面：") + take.action
    references = []
    if has_cast:
        references.append("演员参考用于本帧指定人物的脸、发型及穿着。演员只在画面需要的站位出现。")
    if has_location:
        references.append("场地参考用于本段的建筑、道具材质和光线。")
    if has_product:
        references.append(f"商品参考用于同一份「{plan.product}」的真实外观和摆盘。{plan.appearance}。")
    if index == 6 and first:
        moment += "餐盒盖已经合严，纸袋保持原位置，本帧商品未露出。"
    return (
        f"写实餐饮广告电影静帧，超宽画幅，完整单镜构图。地点和光线：{take.setting}。"
        f"{''.join(references)}画面：{moment}。保持与同场画面一致的服装、道具、方向和时代。"
        "画面内不加题字、字幕、水印或品牌贴纸。"
    )


def animate_endpoints(world, index):
    take = world.takes[index - 1]
    reveal = ("镜头前半保持同一餐盒合盖，约在全段 45% 处掀盖。"
              "揭盖后停留在同一份商品及自然的接礼反应。") if index == 6 else ""
    return (
        f"以首尾两帧为画面边界。{take.setting}。镜头运动：{CAMERA[index - 1]}"
        f"动作：{take.action}。{reveal}"
        "只让画面已有的人物、道具和背景按本段动作过渡，每次近景也沿用相同服装和场景。"
        "画面不带题字、字幕或水印。"
    )


def take_prompt(world, index, *, has_cast, has_location, product):
    take = world.takes[index - 1]
    prompt = (
        "写实商业电影。视频1是模糊灰度运镜板，只参考取景、相对位置、运动轨迹和原有切换节拍。"
        "运镜板中的轮廓属于旧画面。前景与背景中的人物、服装、建筑、交通工具、道具和动物都按本段新设定呈现。"
        f"本段的时代、地点与灯光：{take.setting}。"
    )
    if has_cast:
        prompt += (
            f"图1只用来认演员的脸、发型和穿着，画面背景按本段设定重拍。人物是{world.cast}。保持脸、衣着发型稳定，"
            "只出现当前动作要求的角色，画面是整张电影画面，禁止拼贴和分栏。"
        )
    if has_location:
        prompt += f"图{2 if has_cast else 1}仅参考真实地点的材质、色彩、建筑和灯位。"
    prompt += product
    return (
        f"{prompt}运镜：{CAMERA[index - 1]}具体动作：{take.action}。"
        "从首帧到尾帧，人物衣着、可见道具和背景都延续本段的时代与地点，近景和反打也保持一致。"
        "画面不带题字、字幕或水印。"
    )
