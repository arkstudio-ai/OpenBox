"""The 28 editorial beats measured in the headphone commercial, with product-specific direction."""

import asyncio
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from core.log import create_logger
from light.video_language import Language

log = create_logger("light.commercial_plan")
FORMAT = "product-story-v4"
PLANNING_REVISION = 6
REVIEW = "commercial-reference-v4"
FPS = 24
FRAME_COUNT = 1277
DURATION = FRAME_COUNT / FPS
MATCH_FRAMES = (365, 528, 836)
ASSET_LIMIT = 10
REFERENCE_WIDTH = 1440

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=72)]
DirectionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=350)]
Direction = Annotated[DirectionText, Field(min_length=1)]
View = Annotated[int, Field(ge=1, le=ASSET_LIMIT * 3)]
Role = Literal["product_1", "product_2", "protagonist", "setting"]
Sample = Literal["opening", "middle", "late"]
Scene = Literal["home", "track", "street", "office"]
Transfer = Literal["pass", "fit", "access"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home: Direction
    track: Direction
    street: Direction
    office: Direction


class Treatment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product: Short
    title: Short
    appearance: Direction
    mode: Literal["carry", "wear", "stationary"]
    transfer_mode: Transfer = Field(default_factory=lambda data: "access" if data.get("mode") == "stationary" else "pass")
    protagonist: Direction
    track_wardrobe: str = Field(default="", max_length=260)
    authority: Direction
    settings: Settings
    routine: Direction
    inserts: list[Direction] = Field(min_length=5, max_length=5)
    insert_openings: list[Direction] = Field(default_factory=list, max_length=5)
    insert_product_visible: list[bool] = Field(default_factory=lambda: [False] * 5, min_length=5, max_length=5)
    contact: Direction = Field(description="One repeatable hand contact. Specify hand, visible product surface and direction of approach. For hand-held items, use the supporting hand or the item's geometry as the reference. Leave the body's exact height to the camera unless the merchant specifies it.")
    carry: Direction = Field(description="The product's physical carrying or installed position, with the contact point accessible.")
    first_use: Direction = Field(description="Shot 3 only: a single use by the protagonist at the opening location beside the window.")
    pickup_state: DirectionText = ""
    first_use_state: DirectionText = ""
    handoff: Direction = Field(description="Shot 26 only: one visible workplace transfer of possession or access.")
    handoff_preparation: DirectionText = ""
    handoff_state: DirectionText = ""
    second_use: Direction = Field(description="Shot 27 only: the authority uses the product and reacts, still at work.")
    street_anchor: Direction = Field(description="Only the appearance, base and motion of a wind-powered inflatable street dancer.")
    call_line: str = Field(min_length=1, max_length=44, pattern=r"^[^\r\n]+$")
    rebuke_line: str = Field(min_length=1, max_length=95, pattern=r"^[^\r\n]+$")
    call_voice_gender: Literal["male", "female", "unspecified"] = "unspecified"
    rebuke_voice_gender: Literal["male", "female", "unspecified"] = "unspecified"
    end_copy: str = Field(default="", max_length=72)


class Reading(Treatment):
    transfer_mode: Transfer = Field(description="pass the portable item, fit a wearable onto the authority, or give access to its installed position")
    track_wardrobe: str = Field(max_length=260, description="The visibly distinct running outfit for shots 12–17, preserving the actual product and support. Empty only when the merchant explicitly requires a single outfit or changing it would change the product itself.")
    insert_openings: list[Direction] = Field(min_length=5, max_length=5,
        description="Five camera-ready stills immediately BEFORE each insert action, including object, supporting surface and hand position.")
    pickup_state: Direction = Field(description="Untouched starting state of the actual product and its parts, before the protagonist reaches in.")
    first_use_state: Direction = Field(description="Start of shot 3 at the point of use. Keep the same grip, physical assembly and accessories after the lift in shot 2.")
    handoff_state: Direction = Field(description="Physical condition of the product throughout the transfer, including openings and inactive parts. Describe objects only, without actor names, hands or owners.")
    handoff_preparation: Direction = Field(
        description="Shot 25: at most one continuous physical operation to make the handoff accessible while the authority speaks. If already ready, describe its visible resting state.")
    insert_product_visible: list[bool] = Field(min_length=5, max_length=5,
        description="For each of the five insert actions, is the actual product or any part of it in frame?")
    product_views: list[View] = Field(min_length=1, max_length=2)
    obscured_product_views: list[View] = Field(default_factory=list, max_length=ASSET_LIMIT * 3,
        description="Video frame numbers showing this same product with an external object visibly covering a piece of its silhouette, support or touch surface. Exclude other products and unobstructed frames.")
    protagonist_view: View | None = None
    setting_view: View | None = None
    setting_scene: Scene = "home"
    setting_quote: str = Field(default="", max_length=210,
        description="Exact words asking to preserve the setting in a PRODUCT image or video, empty if none.")

    @model_validator(mode="after")
    def physical_transfer(self):
        if (self.transfer_mode == "access") != (self.mode == "stationary") or (self.transfer_mode == "fit" and self.mode != "wear"):
            raise ValueError("Installed goods transfer access. Only an actual wearable can be fitted onto the other actor.")
        return self


class Plan(Treatment):
    format: Literal["product-story-v4"] = FORMAT
    language: Language
    product_assets: list[str] = Field(min_length=1, max_length=2)
    protagonist_asset: str = ""
    setting_asset: str = ""
    setting_scene: Scene = "home"
    video_samples: dict[Role, Sample] = Field(default_factory=dict)
    obscured_product_videos: list[str] = Field(default_factory=list, max_length=2)
    video_reference_version: Literal[1, 2] = 1
    score_revision: Literal[1, 2] = 1


class ContinuityEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: Literal["routine", "carry", "contact", "first_use", "pickup_state", "first_use_state", "handoff", "handoff_state", "handoff_preparation",
                  "second_use", "call_line", "rebuke_line", "protagonist", "track_wardrobe", "authority",
                  "home", "track", "street", "office", "insert_1", "insert_2", "insert_3", "insert_4", "insert_5"]
    reason: Direction
    replacement: Direction = Field(description="Complete replacement for this ONE slot, in the target language.")
    shows_product: bool | None = Field(default=None, description="For an insert, whether this shot shows any actual part of the product.")
    opening: Direction | None = Field(default=None, description="For an edited insert, its pre-action still if the setup also needs correction.")


class ContinuityNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problems: list[ContinuityEdit] = Field(max_length=8)


# The non-human dance partner and which side of the wide it occupies are part of the original gag.
_STREET_PARTNER = {
    "zh": "街对面广角的左侧，蓝绿色充气舞人固定在风机底座上，橙色锯齿纹随双臂一起摆动。舞者的位置在画面右侧。",
    "vi": "Bên trái khung hình rộng là hình nộm hơi xanh ngọc với họa tiết răng cưa màu cam. Đế quạt đứng yên, hai tay vẫy theo gió. Vị trí nhảy ở bên phải.",
    "en": "Frame left in the wide shot, a turquoise inflatable figure with orange zigzags waves its arms above a fixed blower. The dancer's mark is on the right.",
}


DIRECTING = (
    "复刻用户选中的耳机商业样片。单次生成包含读实际商品素材、适配剧本、拍摄、自动审查与混音。"
    "开场受到环境干扰，使用商品后进入节奏，五个生活插镜接出门，三次相同触点跨场。"
    "运动、街头共舞、短接话、权威人物打断、交接、表情反转。最后同街角换人复演共舞。"
    "模板保留28镜、约53.21秒、标志性机位和音乐起落。"
)

_READ = (
    "你是这支生活方式商业广告的改编导演。按编号核对用户真实图片，把同一参考片的形式移植到此商品。"
    "只返回当前素材的选角、场景、实体动作和两句短对白。面向用户的所有字段都用language指定语言。"
    "product_views选出展示同一件主商品的1至2张图的1起序号。其他图可提供用户指定的演员、场景或使用情境。"
    "另外查看每段视频的开头中间结尾。若手指或遮挡物实际盖住同款商品外轮廓、承重部件或手势要触摸的一部分，"
    "把这些帧的序号记入obscured_product_views。透过真实孔洞看见后方的手、单纯握住但未遮住外轮廓或部件不算。"
    "protagonist_view只选有可辨面孔的角色参考。只有单独的实景选景素材或用户明确要求保留的场景才能填setting_view，"
    "同时指定唯一适用的setting_scene。如果只是商品实拍附带的背景，setting_view填null。商品名称以表单为准，appearance只写可见形状、颜色、材质和可辨结构。"
    "五段按日常准备、动感运动、街头共舞、工作场所发生冲突、同街角换权威人物共舞的顺序。"
    "根据素材和用户意图重新选择人物、可拍的使用方法、生活细节及工作关系。protagonist与authority是两位明显不同的成年演员，"
    "各自以可复现的脸发、衣着、鞋、工作配饰区分；街舞时不换脸或服装，权威人物回街要保留身份标记。"
    "protagonist写日常、街头和进工作场所的同一套通勤衣。track_wardrobe另外写运动场的一套服装，用明显色差或剪裁建立换景。"
    "原片第12镜在同一手势上换运动衫、卸通勤背包，第18镜在同一手势上恢复通勤造型，第24镜进办公室继续街上造型。"
    "如果背包、衣服、鞋子或携具就是被宣传的商品或维持其支撑所必需，务必留着；只改能换的其他衣物。"
    "用户明确要求全片穿同一套时遵守要求。"
    "settings分别写固定布景、出入口、光线和地理关系；street_anchor是街头可辨认且能与人呼应律动的单一地标，结尾原位回用。"
    "routine和五个inserts是一个连贯日常动作序列，分别对应极近景准备、旋合或对接、突然启动、肩后看到节奏、完成一小拍。"
    "为商品选择一种可信的重复手势contact，写清用哪只手、触点和朝向；三处在这一动作的相接相位切场。"
    "first_use是实际用一次商品后心情改变。carry描述运动和街头如何安全携带或佩戴，handoff描述怎么将实物或体验权交给权威人物，"
    "second_use是他体验后从错愕变松弛。不得凭空给商品加按钮、耳机功能、减噪、防水、性能数字或会说话的功能。"
    "transfer_mode根据真实商品决定。若可自然地从头部等处摘下并直接替上司戴上，首选fit，准确重现参考片的反转。"
    "普通便携商品选pass，交到对方控制中；固定商品选access，交出实际使用位置。"
    "固定或过大的商品用stationary，日常开场与工作场所回归设于同一间工作室、门店或共享空间，写清固定安装处。"
    "在运动和街头用空手姿势匹配或经用户素材确认的真实媒介，"
    "权威人物可接手使用位置。只有实际穿戴商品用wear。便携商品用carry。"
    "call_line是街头主人公对来电或画外催促的简短回应，可在1.3秒内说完；rebuke_line是权威人物真实的质问，约2至3秒。"
    "两句的voice_gender按本次角色选定的配音声线填写，未指定声线则填unspecified。"
    "权威人物被感染后的行为就是笑点，不给商品编写独白，不新增广告尾卡。用户明确指定结尾文案时填写end_copy供最后实景叠字，否则留空。"
)

_CONTINUITY = (
    "各字段分别拍摄。第3镜first_use仅在开场窗边原地，商品已拿起或已就位，只做一次使用并产生节奏感。"
    "随后五个inserts各只描写单一动作。早晨使用后仍须衔接准备动作，避免先收到包里又凭空出现在桌上。"
    "商品适合随身使用时，可以像原片一样让五个插镜拍另一件日常事，比如准备晨饮，商品在该活动中的佩戴或收纳状态连续。"
    "insert_product_visible逐一标记五个插镜是否实际露出商品，局部的盖、提手、布料也算露出。"
    "insert_openings分别写五张插镜首帧，具体说明手、器物和支撑的位置，停在相应动作尚未发生的一瞬间。"
    "carry仅描述稳定携带的部位和状态。contact描述哪只手、哪个可见的接触面和从哪个方向接近。手持物用商品和承重手作参照，"
    "例如从上方落到承重手上方露出的外缘。商家未指定触点高度时，把实际的触点高度留给定装，在三次切场保持一致。"
    "短语中不提具体场地。该部位在跑动、跳舞、进门的瞬间始终可及；不能一会在桌面，一会在包内。"
    "运动场换装须保留商品、同一角色和全片规定的接点及承重部位。通勤外套、普通随身包可在换景时省略，回街恢复。"
    "让手势在一眼可见的外表面落下，手与商品间的空隙清楚。优先自然轻触外缘，避免穿过小孔、交叉指头或抛接。"
    "选一个远近两个机位都能辨认、自然够到的部件和外表面，写清手的侧别及来向。"
    "表面平滑且没有标记时，用可见物理边界确定同一侧的接触区域；具体落点在真人定装上确定，"
    "同一切口的两侧按这张实拍定装和上一镜最后一帧接拍。除非商家指定或有可见特征，不凭空要求精确几何中点。"
    "carry交代哪个部位真正承重、真实尺寸下哪个商品部件可触及。对于手持品，优先用商品本身和持物手来描述，"
    "如从上方落到提手露出的外弧。分别描述承重手的位置与可触表面；除非用户明确指定，按真实尺寸让商品的突出部件自然越过支撑点。"
    "实际相位、手与商品的相对位置在合体定装上固定。track_wardrobe只写运动服和与通勤装的区别，勿重复手部动作或身体高度。"
    "定置商品留在原地，跨外景采用人物的空手切点。第26镜只交接一次，第27镜second_use只拍工作场所的权威人物体验和表情。"
    "first_use_state和handoff_state分别写明使用前、交接完成时真实物件的开合、支撑和必要配件位置。"
    "handoff_state只写商品和静态附件的物理状态，不写演员、谁伸手、谁已经接住。人员所有权由26镜首尾分别规定。"
    "pickup_state是第2镜伸手之前的状态。第2镜仅一秒，桌沿低机位拍摄商品刚离开原支撑时切出，人物头脸在画外。"
    "第3镜换中景，途中上移的距离被剪掉，first_use_state是这时的起点。两镜延续同一物件、装配、配件和握持。"
    "pass若需要从携带处取下或开启，handoff_preparation写主人公在25镜受到质问期间怎样备好，26镜再一次交接。"
    "交接优先保持商品在携带时的完整装配状态，按它真实的握持、穿戴、启用或使用方式选择上司的体验。"
    "第25镜共5.17秒，要跟拍进门、上司起身并说完一句话。主人公在这期间最多做一个连续的准备动作。"
    "若道具已在手，只需递到可见位置或进行一次必要的准备；若要从携具取出，取出就是本镜唯一的准备动作，"
    "保持取出时的装配状态交接。在这五秒里从携具取出、放上桌、拆开、另放配件再重新拿起，无法拍成原片的冲突镜头。"
    "遇到这种组合，改成完整交接或从一开始采用可直接交接的携带方式，联动调整handoff_state与second_use。"
    "第27镜由上司亲自体验这个商品，选一眼看懂、适合该商品且能从交接状态直接开始的真实动作。"
    "fit复刻原片的具体动作。第25镜主人公照常佩戴，走近正在质问的上司；第26镜才从自己身上摘下同一件商品，"
    "以一个连续动作直接戴到上司身上，按实际外形确定佩戴部位。尾帧商品由上司身体承重，主人公可以仍轻扶定位。"
    "第27镜近景接同一姿态，主人公轻扶的手松开退出，上司从错愕转为享受；第28镜仍穿工作服、戴同一件商品回原街角跳舞。"
    "这种动作不用上司伸手接到手里、再重复穿戴。难以直接穿上的商品可以按pass交接。"
    "26镜只交接一个正在使用的对象。若体验确实需要拆卸，在25镜把暂时用不到的活动配件放在台面侧边的画外位置，"
    "26–27镜裁掉这部分台面，直到再次使用前配件留在原地。保留主商品的实际轮廓、支撑与接点。"
    "体验动作必须匹配已拍到的状态。涉及流动、取放、穿戴的部位须有实际开口或接触面，不能透过封闭部件。"
    "单镜时长不够拍完整开合和使用时，选择更简单但真实可见的体验；不要叠加倒入另一器皿、喝下又收回等连续任务。"
    "五个场的选景settings只写可见空间、物理位置和光线，不写人物出入的剧情。"
    "原片有且只有两个具名角色。街角搭档为底座固定、双臂可随风起伏的充气舞人，street_anchor仅写它的颜色、形状、位置与动作。"
    "第22镜仅主人公与充气舞人，第28镜仅权威人物与同一个充气舞人，拍摄位置相同。最后一镜主人公不在场。"
    "用户提到社交场合或朋友时保留该使用动机，街角的出镜人数和搭档遵循样片。"
)

_CHECK = (
    "你是参考商业片的场记，对照每个字段实际写下的动作与给定场所、镜长和上一镜的物品位置，逐项核对草稿。"
    + _CONTINUITY +
    "场与场之间本来就有时间省略和动作匹配。请按能实际拍摄的一种自然解释读稿，允许镜外常规动作；"
    "只有两个字段明写相反状态、动作在给定场所无法拍成一镜、或改了换人笑点，才退回。"
    "一次列全这些冲突，提供只替换出错字段的最小可拍改写，不改动无关字段。理由和改写均用成片语言。合格返回空数组。"
    "单个动作可以附带连贯的表情变化。不要对服装、配色、举哪只手或用户产品品类作审美评判。"
    "确认用户可见字段用指定语言，商品与用户图一致。"
)


def _apply_continuity_edits(reading, edits):
    data = reading.model_dump()
    for edit in edits:
        if edit.slot.startswith("insert_"):
            index = int(edit.slot[-1]) - 1
            data["inserts"][index] = edit.replacement
            if edit.opening:
                data["insert_openings"][index] = edit.opening
            if edit.shows_product is not None:
                data["insert_product_visible"][index] = edit.shows_product
        elif edit.slot in Settings.model_fields:
            data["settings"][edit.slot] = edit.replacement
        else:
            data[edit.slot] = edit.replacement
    return Reading.model_validate(data)


async def _views(ctx, asset):
    from light.remake import _material_refs

    first = await _material_refs(ctx, [asset], detail_width=REFERENCE_WIDTH)
    if first == [asset]:
        return [(asset, None, asset)]
    later = await asyncio.gather(*(_material_refs(ctx, [asset], video_sample=point, detail_width=REFERENCE_WIDTH)
                                   for point in ("middle", "late")))
    return [(asset, point, values[0]) for point, values in zip(("opening", "middle", "late"), (first, *later))
            if values]


async def filming_reference_roles(ctx, plan):
    from light.remake import _material_refs

    roles = [(f"product_{i}", asset) for i, asset in enumerate(plan.product_assets, 1)]
    roles.extend([("protagonist", plan.protagonist_asset), ("setting", plan.setting_asset)])
    selections = {role: (asset, plan.video_samples.get(role, "opening")) for role, asset in roles if asset}
    keys = list(dict.fromkeys(selections.values()))
    width = REFERENCE_WIDTH if plan.video_reference_version == 2 else None
    images = await asyncio.gather(*(_material_refs(ctx, [asset], video_sample=point, detail_width=width)
                                    for asset, point in keys))
    by_input = dict(zip(keys, images, strict=True))
    return {role: (asset, point, by_input[asset, point]) for role, (asset, point) in selections.items()}


async def filming_references(ctx, plan):
    refs = await filming_reference_roles(ctx, plan)
    return ([image for role, (_, _, images) in refs.items() if role.startswith("product_") for image in images],
            refs["protagonist"][2] if "protagonist" in refs else [],
            refs["setting"][2] if "setting" in refs else [])


async def adapt(ctx, form, language):
    from light import reference_quality
    from light.remake import RemakeStopped, _parallel
    from light.remake_case import reference_copy

    batches = await _parallel(form["asset_ids"], lambda asset: _views(ctx, asset), concurrency=4)
    inputs = [view for batch in batches for view in batch]
    if not inputs:
        raise RemakeStopped({"zh": "请提供能看清商品的照片或实拍视频。", "vi": "Vui lòng thêm ảnh hoặc video nhìn rõ sản phẩm.",
                             "en": "Add a photo or video that clearly shows the product."}[language])
    brief = {"language": language, "product_name": form.get("product_name"), "shop": form.get("shop"),
             "brief": form.get("brief"), "copy": reference_copy(form), "promotion": form.get("promotion_brief"),
             "notes": form.get("notes"), "available_views": list(range(1, len(inputs) + 1))}
    brief["view_sources"] = [{"view": i, "material": form["asset_ids"].index(asset) + 1, "sample": point}
                             for i, (asset, point, _) in enumerate(inputs, 1)]
    images = [image for _, _, image in inputs]
    brief["street_partner"] = _STREET_PARTNER[language]
    for _ in range(2):
        result = await reference_quality.ask(ctx, "adapt_reference_commercial", Reading, _READ + _CONTINUITY, brief,
                                             images, max_tokens=4000, timeout=85)
        reading = Reading.model_validate(result["report"]).model_copy(update={"street_anchor": _STREET_PARTNER[language]})
        indices = [*reading.product_views, reading.protagonist_view, reading.setting_view]
        if all(index is None or index <= len(inputs) for index in indices):
            break
        brief["correction"] = "Only use image numbers listed in available_views."
    else:
        raise RemakeStopped({"zh": "无法将商品对应到已上传的素材。", "vi": "Chưa đối chiếu được sản phẩm với tư liệu đã chọn.",
                             "en": "The product could not be matched to an uploaded material."}[language])

    feedback = []
    for attempt in range(4):
        check = await reference_quality.ask(ctx, "check_reference_storyboard", ContinuityNotes, _CHECK,
                                            {"language": language, "brief": brief.get("copy") or brief.get("brief"),
                                             "storyboard": reading.model_dump(), "previous_corrections": feedback[-2:]},
                                            images, max_tokens=2000, timeout=75)
        issues = ContinuityNotes.model_validate(check["report"]).problems
        if not issues:
            break
        feedback.append([{"slot": issue.slot, "reason": issue.reason} for issue in issues])
        log.info("Commercial storyboard review session=%s round=%s corrections=%s",
                                         getattr(ctx, "session_id", ""), attempt + 1, feedback[-1])
        try:
            reading = _apply_continuity_edits(reading, issues)
        except ValidationError as exc:
            feedback[-1].append({"slot": "validation", "reason": str(exc.errors(include_input=False))[:350]})
    else:
        message = {"zh": "分镜复核发现动作衔接问题。", "vi": "Kịch bản còn lỗi liên tục về động tác.",
                   "en": "The storyboard still contains a continuity issue."}[language]
        raise RemakeStopped(f"{message} {issues[0].reason}")

    data = reading.model_dump(exclude={"product_views", "obscured_product_views", "protagonist_view", "setting_view", "setting_quote"})
    data["product"] = form.get("product_name") or reading.product
    product_indices = list(dict.fromkeys(reading.product_views))
    roles = [(f"product_{i}", pos) for i, pos in enumerate(product_indices, 1)]
    product_assets = [inputs[i - 1][0] for i in product_indices]
    obscured = set(reading.obscured_product_views)
    obscured_videos = []
    for asset in dict.fromkeys(product_assets):
        samples = {point: i for i, (source, point, _) in enumerate(inputs, 1) if source == asset}
        if set(samples) == {"opening", "middle", "late"} and obscured.issuperset(samples.values()):
            obscured_videos.append(asset)
    setting = reading.setting_view
    if setting and inputs[setting - 1][0] in product_assets:
        # A countertop behind a bottle is part of the user's footage, not a location brief.
        instruction = " ".join(str(brief.get(k) or "") for k in ("brief", "copy", "notes", "promotion"))
        if not reading.setting_quote.strip() or reading.setting_quote not in instruction:
            setting = None
    roles.extend([("protagonist", reading.protagonist_view), ("setting", setting)])
    samples = {role: inputs[pos - 1][1] for role, pos in roles if pos and inputs[pos - 1][1]}
    return Plan(**data, language=language, video_samples=samples, video_reference_version=2, score_revision=2,
                product_assets=product_assets, obscured_product_videos=obscured_videos,
                protagonist_asset=inputs[reading.protagonist_view - 1][0] if reading.protagonist_view else "",
                setting_asset=inputs[setting - 1][0] if setting else "")


class Beat(NamedTuple):
    frames: int
    labels: tuple[str, str, str]  # Chinese, Vietnamese, English
    camera: str
    opening: str
    action: str
    ending: str
    product: bool = False


# Source frame boundaries, measured before the first tutorial frame F1277.
# Keep the three contact cuts at F365, F528 and F836; the last shot is live action.
BEATS = (
    Beat(102, ("窗边干扰", "Tiếng động ngoài cửa", "Noise outside"), "背影中景缓推，沿肩弧移至侧脸",
         "主人公背朝镜头，日常空间的窗边有可见的干扰声源。", "循噪声望向窗外，侧脸显出躁意。", "主人公目光转向附近能带来转机的商品。"),
    Beat(24, ("接触商品", "Chạm vào sản phẩm", "First contact"), "低机位，商品与手的特写",
         "真实商品保持静止，手从画边接近适用部位。", "{approach}", "实际接触成立；商品保持形状。", True),
    Beat(62, ("进入节奏", "Bắt nhịp", "Finding the groove"), "正侧面中近景",
         "从上镜接触状态起，主人公注意力仍在商品。", "{first_use}", "动作完成后，放松肩膀或表情，音乐进入。", True),
    Beat(11, ("准备插镜", "Chuẩn bị", "Prepare"), "物件微距切入", "准备动作正要开始。", "{insert0}", "切在动作的重音。"),
    Beat(12, ("对接插镜", "Gắn khớp", "Fit"), "双手和器物的反角近景", "对准同一生活流程的下一件器物。", "{insert1}", "接合动作完成。"),
    Beat(13, ("启动插镜", "Khởi động", "Start"), "极近景，视觉冲击", "器物启动或亮相之前的一刻。", "{insert2}", "视觉或声音重音落定。"),
    Beat(34, ("肩后律动", "Nhịp qua vai", "Over the shoulder"), "肩后俯角中近景", "回到主人公与同一项日常活动。", "{insert3}", "手和肩的节拍已经建立。"),
    Beat(22, ("日常收拍", "Kết nhịp buổi sáng", "Last morning beat"), "侧面近景，物件接脸", "动作已来到主人公身前。", "{insert4}", "收在一次简洁明确的完成动作上。"),
    Beat(23, ("出发细节", "Sẵn sàng lên đường", "Ready to go"), "装备或背包极近景", "出门前的随身装备仍打开。", "手将装备收好，一次脆利的闭合。", "扣合或拉链动作已经完成。"),
    Beat(48, ("踩拍离场", "Rời nhà theo nhịp", "Dance to the door"), "高位广角跟出门",
         "主人公穿戴整齐，在出门动线上。", "轻跳两拍向门边出发。{carry}", "站在门侧准备做标志手势。"),
    Beat(14, ("手势切出", "Cử chỉ trước khi cắt", "First match out"), "侧面近景，触点居中",
         "主人公已到门边，同侧近景内目标露出。做手势的手尚在画缘外或自然垂在远端；若它同时承重，保持握持，活动指尖收回。", "{contact}",
         "接点一：一次清楚可见的伸手动作结束，指尖刚落定，面向画面右侧。", True),
    Beat(17, ("跑道接拍", "Bắt nhịp ở đường chạy", "Match to the track"), "与上一镜同侧同高度",
         "接点一：同一指尖同相位、同一人，背景已换运动场。", "放开刚完成的手势，身体起动。", "主人公向画面右侧起跑。", True),
    Beat(11, ("脚落白线", "Bước chạm vạch", "Foot on the line"), "贴地鞋底大特写",
         "标线前画面只见地面。", "主人公的一只运动鞋踩过标线。", "脚离开标线。"),
    Beat(16, ("冲刺掠过", "Lướt qua góc thấp", "Low sprint"), "低机位广角，真实速度",
         "主人公从弯道或活动路线一侧进入。", "以有力步态向同方向掠过。", "身体越过机位。"),
    Beat(40, ("随身商品特写", "Cận sản phẩm theo nhịp chạy", "Body-mounted detail"), "随身或物件固定机位，背景横移",
         "{detail_start}", "{detail_action}", "实际商品外形清楚，动感来自背景或使用动作。", True),
    Beat(27, ("高位全景", "Toàn cảnh từ trên cao", "High wide"), "看台或相近高位大全景",
         "先见完整运动空间和路径。", "主人公沿同一方向穿过构图。", "主人公进入通往减速段的位置。"),
    Beat(52, ("减速触点", "Giảm tốc và chạm", "Slow to a touch"), "正面中景，运动到静止",
         "主人公迎面而来，双肩跟随步伐。", "逐渐站稳，做同一个标志动作：{contact}", "接点二：指尖刚落定，人物面向镜头。", True),
    Beat(61, ("街头承接", "Nối sang góc phố", "Match to the street"), "正面中景承接，向后跟拍",
         "接点二：同一手势与相位，在有固定地标的街头。", "放下触点的手，顺势进入音乐中的第一组舞步。", "向同一街角前进。", True),
    Beat(47, ("贴地舞步", "Bước nhảy sát mặt đường", "Feet in rhythm"), "贴地跟拍双脚",
         "街头路面和主人公鞋子占画面。", "脚跟、脚尖交替打出切分舞步。", "脚落在下一重拍。"),
    Beat(32, ("侧跟摆臂", "Theo ngang nhịp tay", "Sideways tracking"), "侧向跟拍中景",
         "主人公在同一街面，保持此前的造型与商品状态。", "肩和手臂配合节拍摆动两拍。", "动作朝街头地标展开。"),
    Beat(59, ("俯拍揭晓", "Góc cao hé lộ bạn nhảy", "Overhead reveal"), "垂直俯拍，揭开空间关系",
         "街面、主角和地标的固定基座同时入构图。", "主人公跳过树影，画面揭示会和他呼应的地标。", "人物与地标各处已确定的一侧。"),
    Beat(77, ("街角共舞", "Cùng nhảy ở góc phố", "Dance partner"), "街对面固定广角，整组动作",
         "沿用上一镜的街角关系。地标为{street_anchor}。", "主人公在地标旁完成易认的双臂波浪舞，一人一物相互呼应。", "定住双方的侧别、光向和基座，为结尾回扣。"),
    Beat(32, ("停步应答", "Dừng bước đáp lời", "The call"), "低角度脸部近景，保留背景地标",
         "舞步停下，主人公注意到来电或画外的催促。", "说出短回应，在句尾做标志动作：{contact}", "接点三：手指在触点，表情转为认真。", True),
    Beat(24, ("工作场所接拍", "Nối sang chỗ làm", "Match into work"), "承接上镜的近景，冷暖光变换",
         "接点三：延续街角同一侧脸和商品。指尖仍在同一部件，或刚顺势松开一线，手还贴近原触点。背景已换工作场所。",
         "顺着刚才的触点自然完成放手，走向工作区。", "前进方向与随后背跟一致。", True),
    Beat(124, ("跟入冲突", "Theo vào cuộc đối chất", "The confrontation"), "越门背跟主人公至双人中景",
         "主人公刚进门，仍在原携带或穿戴状态；权威人物在远端工作位坐着。两人还没到交接位置。",
         "镜头跟进，权威人物起身并出声质问，主人公止步。", "两人面对面，权威人物尚未接手商品。"),
    Beat(63, ("商品交接", "Trao trải nghiệm", "The handoff"), "双人中景，交接全程在画内",
         "主人公控制商品或使用位置，另一位还未接手。", "{handoff}", "接手过程已完成；主人公收回手，只有一件商品。", True),
    Beat(105, ("错愕到微笑", "Từ ngỡ ngàng đến mỉm cười", "From disbelief to a smile"), "权威人物近景，留表情变化的时间",
         "权威人物已接手体验，开始仍不以为然。", "{second_use}。从惊愕、短暂停顿，到闭眼或微笑。", "表情呼应第一场主人公被感染的那一刻。", True),
    Beat(125, ("同街换人共舞", "Đổi người ở cùng góc phố", "The other dancer"), "与第22镜同机位、同街角、同地标",
         "原街角和地标原位，权威人物已在主人公原来的舞蹈位置，双臂已经扬起。", "保留工作造型，完整复演之前的双臂波浪舞。地标同步呼应。", "笑点落在舞步上，实景硬切结束。"),
)

SCENE_IDS = ("home",) * 11 + ("track",) * 6 + ("street",) * 6 + ("office",) * 4 + ("return",)
_SCENE_TITLES = (("日常", "运动场", "街角", "工作场所", "回到街角"),
                 ("Đời thường", "Đường chạy", "Góc phố", "Chỗ làm", "Trở lại góc phố"),
                 ("Morning routine", "Athletics", "Street corner", "Workplace", "Back on the street"))
_OUTDOORS = {"track", "street", "return"}


def shots(plan: Plan):
    stationary = plan.mode == "stationary"
    args = plan.model_dump()
    args.update({f"insert{i}": action for i, action in enumerate(plan.inserts)})
    args["approach"] = ("主人公将手落到固定商品的实际使用部位。" if stationary
                        else "手伸进桌沿低机位，从原支撑上稳稳拿起这同一件商品；在上升途中切走。")
    args["detail_start"] = ("回插已建立的固定商品使用地，以真实使用部位起镜。" if stationary
                            else f"商品正以这种方式随身：{plan.carry}。让外观最可辨认的一面占近景。")
    args["detail_action"] = ("相机跟随使用部位的单一动作，不移动固定商品。" if stationary
                             else "景别锁在商品上，运动造成自然起伏；背景有明显的速度。")
    lang = ("zh", "vi", "en").index(plan.language)
    output = []
    for i, (beat, scene) in enumerate(zip(BEATS, SCENE_IDS, strict=True), 1):
        visible = (plan.insert_product_visible[i - 4] if 4 <= i <= 8 else beat.product)
        visible = visible and (not stationary or scene not in _OUTDOORS or i == 15)
        opening, ending = beat.opening.format_map(args), beat.ending.format_map(args)
        if 4 <= i <= 8 and len(plan.insert_openings) == 5:
            opening = plan.insert_openings[i - 4]
        action = beat.action.format_map(args)
        if i == 2 and plan.pickup_state:
            opening += " 取用前的实物与配件：" + plan.pickup_state
        if i == 2 and not stationary:
            ending = "手握稳商品，商品刚刚离开原支撑向上移动；同一低机位仍只见商品、手和身躯。"
        if i == 3 and plan.first_use_state:
            if not stationary:
                opening += " 换中景，省略拿起后移至使用部位的路程。"
            opening += " 使用前的实际商品状态：" + plan.first_use_state
        if plan.track_wardrobe and i == 12:
            opening += " 跑道服装已在切点换好，演员、商品、实际触点及相位承接。跑道造型：" + plan.track_wardrobe
        if plan.track_wardrobe and i == 18:
            opening += " 同一瞬间恢复之前的通勤造型，仍然是同一演员、同一商品、同一触点。"
        if plan.track_wardrobe and i == 24:
            opening += " 从街头进工作场所沿用同一套通勤衣着和随身物。"
        if i in (22, 28) and plan.mode == "carry":
            wave = ("若手正托住商品，保持真实承重位置，用空手起波浪、肩膀和托物侧肘随拍响应；"
                    "若双手空闲，用双臂完成同样的波浪。")
            if i == 22:
                action = "主人公从脚跟带起肩臂波浪，模仿身旁充气舞人的摆动。" + wave
            else:
                opening = "同一广角，工作装的上司已占据主人公先前的位置，真正拿着或携带上一场接手的同一件商品。"
                action = "复演第22镜可辨认的波浪舞与脚步，充气舞人同侧呼应。" + wave
                visible = True
        if i in (22, 28) and plan.mode == "wear":
            opening += (" 主人公仍照常穿戴同一件商品，与充气舞人起舞。" if i == 22
                        else " 上司沿用体验后的真实穿戴位置，与充气舞人起舞。")
            visible = True
        if i == 25 and plan.handoff_preparation:
            action += " 质问期间，主人公在画内准备交接：" + plan.handoff_preparation
            visible = True
        if i == 25 and plan.transfer_mode == "fit":
            ending = "主人公走近，商品仍佩戴在自己身上；上司指责的动作还未收回。"
        if i == 26 and plan.transfer_mode == "fit":
            opening = "主人公仍戴着唯一这件商品，正面迎向上司，上司尚未戴上。"
            ending = "同一商品已戴在上司正确的部位，由上司身体承重。主人公可仍轻扶定位，接入下一镜。"
        if i == 26 and plan.transfer_mode == "access":
            ending = "上司已占据使用位，主人公让开，固定商品仍在原来的安装处。"
        if i == 26 and plan.handoff_state:
            ending += " 交接完成时的状态：" + plan.handoff_state
            if plan.transfer_mode == "pass":
                opening += " 两人之间清楚看见交接主体；未参与动作的活动配件保存在同一画外处。"
                ending += " 保持原来景框和支撑，未使用的配件留在同一画外处。"
        if i == 27 and plan.handoff_state:
            opening += " 延续刚完成的交接状态：" + plan.handoff_state
        if i == 27 and plan.transfer_mode == "fit":
            action = "承接刚戴到位的状态。若上镜仍有轻扶的手，此时让手退出近景。" + action
        if i == 28 and plan.transfer_mode == "fit":
            opening += " 上司带着第27镜已经戴好的同一件商品，保留工作造型。"
            visible = True
        output.append({"id": i, "frames": beat.frames, "scene": scene, "title": beat.labels[lang],
                       "camera": beat.camera, "frame": opening,
                       "action": action, "end": ending,
                       "product_visible": visible,
                       "spoken_line": plan.call_line if i == 23 else plan.rebuke_line if i == 25 else ""})
    return output


def display_script(plan: Plan) -> str:
    lang = ("zh", "vi", "en").index(plan.language)
    duration = ("53.21 秒 · 28 镜", "53,21 giây · 28 cảnh", "53.21 seconds · 28 shots")[lang]
    labels = dict(zip(("home", "track", "street", "office", "return"), _SCENE_TITLES[lang], strict=True))
    callback = ("穿工作服的上司在同一机位接替主人公，与先前的充气舞人共舞。",
                "Người quản lý vẫn mặc đồ đi làm, thay nhân vật chính nhảy với cùng hình nộm bơm hơi. Máy giữ nguyên góc.",
                "Still in work clothes, the manager takes the lead's place and dances with the same inflatable figure in the same wide shot.")[lang]
    notes = {1: plan.protagonist, 2: plan.pickup_state, 3: plan.first_use, 10: plan.carry, 11: plan.contact,
             22: plan.street_anchor, 23: plan.call_line, 25: plan.rebuke_line, 26: plan.handoff, 27: plan.second_use,
             28: callback}
    if plan.track_wardrobe:
        notes[12] = plan.track_wardrobe
        notes[18] = ("同一手势落在街头，主人公恢复通勤装。",
                     "Qua điểm chạm, nhân vật chính trở lại bộ đồ đi làm trên phố.",
                     "On the same gesture in the street, the lead is back in commuting clothes.")[lang]
    notes.update({index: note for index, note in enumerate(plan.inserts, 4)})
    title = f"《{plan.title}》" if plan.language == "zh" else plan.title
    lines = [f"{title} · {duration}", plan.product, plan.routine]
    frame = 0
    for shot in shots(plan):
        index = shot["id"]
        line = f"{index:02} · {frame / FPS:05.2f}–{(frame + shot['frames']) / FPS:05.2f} · {labels[shot['scene']]} · {shot['title']}"
        lines.append(line + (f" — {notes[index]}" if notes.get(index) else ""))
        frame += shot["frames"]
    if plan.end_copy:
        lines.append(plan.end_copy)
    return "\n".join(lines)


def final_request(cuts, plan):
    return {"recipe": FORMAT, "reference_frames": FRAME_COUNT, "fps": FPS, "duration_sec": DURATION,
            "language": plan.language, "storyboard": plan.model_dump(),
            "timeline": {"shots": [{"asset": c["source"], "start_frame": c["start"],
                                    "source_frames": c["frames"], "output_frames": c["output_frames"],
                                    "speed": c["speed"], "cues": c.get("cues", [])} for c in cuts]}}


def is_complete_cut(run, job):
    from light.commercial_film import _edit_proof, _scene_proofs
    from light.commercial_post import score_for
    from light.wan_edit import _words

    plan = Plan.model_validate(run["remake_adaptation"])
    cuts = run.get("wan_cuts") or []
    if (job.kind != "compose" or job.status != "completed" or not job.output_asset_id
            or not run.get("wan_final_key") or job.idempotency_key != run["wan_final_key"]
            or len(cuts) != len(BEATS)):
        return False
    for index, (beat, cut) in enumerate(zip(BEATS, cuts, strict=True), 1):
        if (cut.get("shot") != index or cut.get("review_version") != REVIEW or not cut.get("source")
                or cut.get("output_frames") != beat.frames or not cut.get("review_board")
                or cut.get("start", -1) < 0 or cut.get("frames", 0) <= 0
                or abs(cut.get("speed", 0) * beat.frames - cut["frames"]) > .001):
            return False
    decisions = run.get("director_decisions") or {}
    proof = _edit_proof(decisions, plan, [cut["review_board"] for cut in cuts], {c["shot"]: c for c in cuts}) or {}
    if not _scene_proofs(decisions, proof, plan):
        return False
    measured = job.result_data or {}
    subtitles = (measured.get("subtitles_srt") or "").replace("\r\n", "\n").strip().split("\n\n")
    words = [" ".join(entry.splitlines()[2:]) for entry in subtitles]
    sources = measured.get("dialogue_sources") or []
    if "scene_checks" in proof or any("asr_text" in source for source in sources):
        expected = (plan.call_line, plan.rebuke_line)
        if len(sources) != len(expected) or any(
            source.get("method") not in {"native_asr", "tts"} or _words(source.get("asr_text") or "") != _words(line)
            for source, line in zip(sources, expected)
        ):
            return False
    submitted = job.request_data or {}
    try:
        submitted_plan = Plan.model_validate(submitted.get("storyboard"))
    except ValidationError:
        return False
    return (proof.get("approved") is True and proof.get("cut_assets") == [c["review_board"] for c in cuts]
            and len({c["source"] for c in cuts}) == len(BEATS)
            and {**submitted, "storyboard": submitted_plan.model_dump()} == final_request(cuts, plan)
            and measured.get("shot_frames") == [b.frames for b in BEATS] and measured.get("score") == score_for(plan)
            and measured.get("frames") == FRAME_COUNT and measured.get("fps") == FPS
            and abs((measured.get("duration_sec") or 0) - DURATION) < .002 and measured.get("has_audio") is True
            and measured.get("language") == plan.language and measured.get("dialogue") == 2
            and [s.get("shot") for s in sources] == [23, 25]
            and words == [plan.call_line, plan.rebuke_line])
