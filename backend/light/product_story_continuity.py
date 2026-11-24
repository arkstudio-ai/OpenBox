"""Validate approved stills, actual takes and joins for the brand story."""

from io import BytesIO
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw, ImageStat
from pydantic import BaseModel, Field, model_validator

from light import product_story_post as post, reference_quality
from light.product_story_plan import FPS, VISUAL_REVIEW, repair_for
from light.remake import RemakeStopped, _digest, _material_refs, _parallel, _reference_asset
from light.shortcut_review import FrameCheck
from light.wan_remake import _on_set, _still

FRAME_SIZE = "1536x864"
SAMPLE_WIDTH, SAMPLE_HEIGHT = 320, 180
SINGLE_FRAME = (
    "只输出一张铺满16:9画幅的单一机位剧照，只表现这一刻。"
    "不要拼图、分屏、分格、前后对比或多视图。参考图若含多个角度，它们用于确认同一商品，"
    "画面中不得照抄参考图的分格或把同一商品复制成多件。"
)


class JoinCheck(FrameCheck):
    shot: int | None = Field(default=None, ge=1, le=14)

    @model_validator(mode="after")
    def require_target(self):
        if not self.ok and self.shot is None:
            raise ValueError("A rejected join must identify the shot to repair.")
        return self


class EndpointCheck(BaseModel):
    ok: bool
    reason: str = Field(min_length=1, max_length=400)
    first_correction: str = Field(default="", max_length=480)
    last_correction: str = Field(default="", max_length=480)

    @model_validator(mode="after")
    def name_bad_frame(self):
        if not self.ok and not (self.first_correction or self.last_correction):
            raise ValueError("Identify which endpoint needs correction.")
        return self


class HoldCheck(BaseModel):
    eligible: bool
    reason: str = Field(min_length=1, max_length=400)


class ReviewRejected(RemakeStopped):
    def __init__(self, shot, reason, correction="", *, kind, **evidence):
        self.review_feedback = {"shot": shot, "reason": reason, "correction": correction, "kind": kind, **evidence}
        super().__init__(reason)


class JoinRejected(ReviewRejected):
    def __init__(self, check):
        self.check = check
        super().__init__(check.shot, check.reason, check.correction, kind="join")


def _frame_scope(shot, state, *, ending):
    people = shot.last_cast if ending else shot.first_cast
    visible = shot.product_in_last_frame if ending else shot.product_in_first_frame
    picture = shot.last_frame if ending else shot.frame
    visibility = ("必须可见" if visible else "在画外。镜头必须裁掉商品及其支架所在的那块场景，不能把这块区域拍成空的")
    return (
        f"取景中实际可见的人物：{'、'.join(people) or '无人'}。"
        f"主商品{visibility}。"
        f"这一刻的场内事实：{_on_set(state.facts)}。静止画面：{_on_set(picture)}。"
        "场内事实用于保证连续性，取景以静止画面所描述的人物与物件为准。若只描述手部，画面内不露出脸。"
    )


def _reference_notes(plan, people, product_count):
    cast = f"按顺序使用下列演员的单人肖像：{'、'.join(people)}。" if people else ""
    product = (f"最后 {product_count} 张是「{plan.product}」的真实素材。"
               f"商品的形状、材质、比例和标志保持一致：{_on_set(plan.appearance)}。"
               if product_count else "")
    return cast + product


def _endpoint_name(role, suffix, correction, anchor=None):
    # A later review may ask for a different fix at the same attempt number.
    signature = (anchor or "") + "\n" + correction
    version = f"-{_digest(signature)[:12]}" if correction or anchor else ""
    return f"brand-v3-{role}-{suffix}{version}"


def _prior_product_tail(plan, tails, index):
    shot = plan.shots[index - 1]
    place = plan.states[shot.before].location
    if index <= 2 or not shot.product_in_first_frame or plan.shots[index - 2].product_in_last_frame:
        return None
    # Reach across an insert that intentionally leaves the product out of frame.
    for previous in range(index - 2, 0, -1):
        candidate = plan.shots[previous - 1]
        if plan.shots[previous].transition == "ellipsis" or plan.states[candidate.after].location != place:
            break
        if candidate.product_in_last_frame and previous in tails:
            return tails[previous]
    return None


async def prepare(ctx, prefix, run, plan):
    from light.production import save_director_decision

    materials = await _material_refs(ctx, plan.product_assets)
    if not materials:
        raise RemakeStopped("请先选择清晰的商品照片，或一段开头能看清商品的实拍。")
    saved = run.get("director_decisions") or {}

    async def portrait(item):
        who, look = item
        return who, await _still(
            ctx, prefix, f"brand-v3-cast-{_digest(who)}",
            f"只拍一位演员「{who}」的中性背景半身肖像。{_on_set(look)}。清楚保留脸、发型和全部可见服装。"
            "平光，空背景，无其他人、商品、道具、文字或水印。",
            [], size="1024x1024", quality="medium",
        )

    async def location(item):
        name, description = item
        return name, await _still(
            ctx, prefix, f"brand-v3-place-{_digest(name)}",
            f"用于电影接镜的同一场地空镜，「{name}」。{_on_set(description)}。"
            "远景只拍固定结构、灯光和家具位置，禁止放入人物、商品及任何在剧情中会移动的道具。",
            [], size=FRAME_SIZE, quality="medium",
        )

    actor_names = {name for shot in plan.shots for name in (*shot.first_cast, *shot.last_cast)}
    place_names = {plan.states[shot.before].location for shot in plan.shots[:-1]}
    cast = dict(await _parallel(((n, plan.cast[n]) for n in sorted(actor_names)), portrait, concurrency=4))
    places = dict(await _parallel(((n, plan.locations[n]) for n in sorted(place_names)), location, concurrency=4))
    setups = {}
    tails = {c["shot"]: c["review_tail"] for c in run.get("wan_cuts", []) if c.get("review_tail")}

    async def make(index):
        repair = repair_for(run, index, frames=True)
        revision = repair.get("revision", 0)
        decision_key = f"{VISUAL_REVIEW}:setup:{index}" + (f":repair:{revision}" if revision else "")
        if decision_key in saved:
            return index, tuple(saved[decision_key]["frames"])

        shot = plan.shots[index - 1]
        hero = index == len(plan.shots)
        before, after = plan.states[shot.before], plan.states[shot.after]
        inherited = setups[index - 1][1] if shot.transition == "match" else None
        note = repair.get("report", {}).get("correction", "")
        incoming = tails.get(index - 1) if repair and shot.transition == "cut" else None
        join_repair = repair_for(run, index, kind="join")
        product_tail = _prior_product_tail(plan, tails, index) if incoming and join_repair else None
        if join_repair and join_repair != repair:
            join_note = join_repair.get("report", {}).get("correction", "")
            note = f"{join_note}。同时完成当前首尾帧修正：{note}" if join_note else note
        previous_setup = None
        if index > 1 and shot.transition == "cut":
            prior = repair_for(run, index - 1, frames=True).get("revision")
            prior_key = f"{VISUAL_REVIEW}:setup:{index - 1}" + (f":repair:{prior}" if prior else "")
            previous_setup = (saved.get(prior_key) or {}).get("frames")
        saved_first = repair.get("report", {}).get("first_asset") if repair.get("target") == index else None
        first = inherited or saved_first
        first_fix = ""
        last_fix = ""
        for attempt in range(3):
            suffix = f"{index:02d}-{attempt}" + ("-endpoint" if attempt else "")
            suffix += f"-repair-{revision}" if revision else ""
            repair_note = f"整体接镜要求：{note}。严格区分当前首帧和动作结束后的状态。" if note else ""
            opening_sources = materials if shot.product_in_first_frame else []
            continuity_frame = incoming or (previous_setup[1] if first_fix and previous_setup else None)
            anchor = ":".join(filter(None, (continuity_frame, product_tail)))
            first = first or await _still(
                ctx, prefix, _endpoint_name("first", suffix, first_fix, anchor),
                "16:9写实电影静帧。商品图片只定义实际商品，演员参考只定义指定演员的长相与服装。"
                + ("" if hero else "图1是同一场地的空镜，只参照固定空间结构与照明。")
                + ("图2是上一镜核准的结束画面。参照它的已发生动作、演员和道具状态，"
                   "本镜仍采用指定的新构图和角度。" if continuity_frame else "")
                + ("图3是特写切走之前商品最后一次可见的实拍画面。保持商品所在位置以及承托它的支架或底座，"
                   "除非当前分镜明确交代了移动。图3中的演员动作、取景和时间不使用。" if product_tail else "")
                + _reference_notes(plan, shot.first_cast, len(opening_sources))
                + f"{repair_note}{_frame_scope(shot, before, ending=False)}"
                + (f"只修这张首帧：{first_fix}。" if first_fix else "")
                + ("商品完整位于左半，右半为均匀暗背景。" if hero else "保留场地参考里的固定结构和照明。")
                + SINGLE_FRAME
                + "无标题、字幕、海报字或新增水印。",
                [*([places[before.location]] if not hero else []),
                 *([continuity_frame] if continuity_frame else []),
                 *([product_tail] if product_tail else []),
                 *(cast[name] for name in shot.first_cast), *opening_sources],
                size=FRAME_SIZE, quality="high",
            )
            last = None
            if not hero:
                entering = [name for name in shot.last_cast if name not in shot.first_cast]
                tail_sources = materials if shot.product_in_last_frame else []
                last = await _still(
                    ctx, prefix, _endpoint_name("last", suffix, last_fix, first if attempt else None),
                    "编辑第一张图为同一个连续镜头的结束帧。锁住同一机位、取景、人物身份和固定布景。"
                    "只做达到新状态必须的姿态或物件位移，禁止新增动作、人物或改换角度。"
                    "没被指定移动的物体保留原位置、朝向和比例。拿走商品时保留原位置的支架和旁边的道具。"
                    + _reference_notes(plan, entering, len(tail_sources))
                    + f"{repair_note}{_frame_scope(shot, after, ending=True)}"
                    + (f"只修这张尾帧：{last_fix}。" if last_fix else "")
                    + SINGLE_FRAME
                    + "无叠字或水印。",
                    [first, *(cast[name] for name in entering), *tail_sources],
                    size=FRAME_SIZE, quality="high",
                )
            references = [*materials, *(cast[n] for n in dict.fromkeys(shot.first_cast + shot.last_cast))]
            anchors = [asset for asset in (incoming, product_tail) if asset]
            proof = await reference_quality.ask(
                ctx, "check_brand_endpoints", EndpointCheck,
                "独立核对即将拍摄的实际广告静帧。输入图先为用户原图和指定演员肖像，最后为待确认的首、尾帧，独立商品片尾只有首帧。"
                "检查演员在应出现的位置且没有提前出现或消失，商品和关键道具符合场内状态并且外观稳定。"
                "相同镜头中的首尾帧固定机位和照明，只发生指定动作。商品、布景和脸必须从图片中看见才算通过。"
                "一个输入图内部出现多视图拼贴时，该端点必须退回重做为一张铺满画幅的单场景。"
                "若构图写了只见手或某区域在画外，应对照画面检查。区分可拍的画面与偏好的构图。"
                "reason和两条correction用成片语言，引用实际观测。轻微光线、大小或姿态差异若能完成指定动作可通过。"
                "只有改变观众能读到的故事、商品身份、持物转移或妨碍执行主动作的差异才退回。"
                "两手均未持有或交接道具时，手是搭在桌沿还是放在膝上允许自然变化。"
                "中远景中一个手指或笔尖轻微偏移，若主要动作和道具状态清楚，也可通过。"
                "有人提前完成动作、正持有的道具换手、商品消失、同一画面拼贴仍必须退回。"
                "若有接镜参考，核对首帧是否承接上一镜真实尾帧的位置、动作和物件状态。"
                "商品跨特写参考只用于核对商品相对桌面、支架或其他固定参照物的场内位置。"
                "允许换机位和小幅构图差异，但视角差异解释不了的换位必须退回首帧。"
                "首帧合格时留空first_correction，只修尾帧；尾帧合格时留空last_correction。"
                "分别写各自需要的静止状态，不能把动作结束姿势作为首帧改法。独立片尾只允许first_correction。",
                {"shot": index, "product": plan.product, "language": plan.language, "source_photos": len(materials),
                 "actor_refs": list(dict.fromkeys(shot.first_cast + shot.last_cast)),
                 "join_refs_after_actor_refs": [label for label, asset in (
                     ("previous actual tail", incoming), ("last visible product tail", product_tail)) if asset],
                 "repair_instructions": note,
                 "shot_description": shot.model_dump(), "before": before.model_dump(), "after": after.model_dump()},
                [*references, *anchors, first, *([last] if last else [])], max_tokens=1500, timeout=55,
            )
            verdict = EndpointCheck.model_validate(proof["report"])
            if verdict.ok:
                decision = await save_director_decision(
                    ctx, prefix, min(index, len(plan.shots) - 1), decision_key,
                    {**proof, "frames": [first, last]},
                )
                return index, tuple(decision["frames"])
            first_fix, last_fix = verdict.first_correction, verdict.last_correction
            if first_fix:
                first = inherited
        raise ReviewRejected(index, verdict.reason, " ".join(filter(None, (first_fix, last_fix))), kind="endpoint",
                             **({"first_asset": first} if first and not first_fix else {}))

    pending = list(range(1, len(plan.shots) + 1))
    while pending:
        ready = [i for i in pending if plan.shots[i - 1].transition != "match" or i - 1 in setups]
        setups.update(await _parallel(ready, make, concurrency=4, stop_on_error=True))
        pending = [i for i in pending if i not in setups]
    return setups, materials


def internal_jump(frames):
    """Return the first large, isolated one-frame discontinuity inside a take."""
    small = [frame.resize((128, 72)).convert("RGB") for frame in frames]
    deltas = [sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 for a, b in zip(small, small[1:])]
    for index, delta in enumerate(deltas):
        neighbors = deltas[max(0, index - 3):index] + deltas[index + 1:index + 4]
        if delta >= max(27, median(neighbors or [0]) * 3.5):
            return index + 1
    return None


async def approved_hold(ctx, index, shot, pair, frames, download):
    if (shot.before != shot.after or shot.line or shot.first_cast or shot.last_cast or not shot.product_in_first_frame
            or not shot.product_in_last_frame):
        return None
    result = await reference_quality.ask(
        ctx, "check_brand_hold", HoldCheck,
        "判断这段无对白商品镜头能否整段复用第一张已核准剧照。严格对照分镜指定动作及两张帧图。"
        "只有分镜明确要求固定机位静态展示，商品、布景、光照、道具全程无叙事变化，"
        "而且第一张图已完整呈现要求用于后续接镜的状态，才填写eligible=true。"
        "光线照亮或在构图上连接两个物体不代表光线在移动。先核对action是否真的要求这种变化。"
        "没有指定变化时，画面中微小的明暗生成差异无需复现。"
        "只有分镜要求改变视角、光影、对焦、商品或道具位置时，才因此填写eligible=false。",
        {"shot": shot.model_dump()}, list(pair), max_tokens=1400, timeout=50, reasoning_effort="high",
    )
    if not HoldCheck.model_validate(result["report"]).eligible:
        return None
    with TemporaryDirectory(prefix="brand-static-take-") as directory:
        folder = Path(directory)
        picture = folder / "approved-frame.png"
        await download(ctx, pair[0], picture)
        movie = await post._motion(folder, index, picture, frames, "1")
        return await _reference_asset(ctx, f"brand-still-hold-{index}-{_digest(pair[0])}-{frames}",
                                      movie.read_bytes(), mime="video/mp4")


async def review_take(ctx, cut, shot, pair, materials, download, *, language="zh"):
    with TemporaryDirectory(prefix="brand-take-review-") as directory:
        source = Path(directory) / "take.mp4"
        await download(ctx, cut["source"], source)
        start, count = cut["start"], cut["frames"]
        data = await post.ff(
            "-threads", "1", "-i", source, "-vf",
            f"fps={FPS},trim=start_frame={start}:end_frame={start + count},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}",
            "-an", "-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        )
    stride = SAMPLE_WIDTH * SAMPLE_HEIGHT * 3
    if len(data) != stride * count:
        raise RemakeStopped(f"第 {cut['shot']} 镜未能逐帧核对实际剪辑区间。")
    frames = [Image.frombytes("RGB", (SAMPLE_WIDTH, SAMPLE_HEIGHT), data[offset:offset + stride])
              for offset in range(0, len(data), stride)]
    if (jump := internal_jump(frames)) is not None:
        reason, correction = {
            "zh": (f"镜内第 {jump} 帧突然切换画面。", "保持同一机位，只拍一个动作。"),
            "en": (f"The picture abruptly cuts at frame {jump}.", "Keep one camera setup and film a single action."),
            "vi": (f"Hình bị cắt đột ngột ở khung {jump}.", "Giữ một góc máy và chỉ thực hiện một động tác."),
        }[language]
        return None, FrameCheck(ok=False, reason=reason, correction=correction)

    picks = (0, count // 4, count // 2, count * 3 // 4, count - 1)
    board = Image.new("RGB", (SAMPLE_WIDTH * len(picks), SAMPLE_HEIGHT + 22), "#191d25")
    draw = ImageDraw.Draw(board)
    for col, index in enumerate(picks):
        left = col * SAMPLE_WIDTH
        board.paste(frames[index], (left, 22))
        draw.text((left + 8, 4), f"SHOT {cut['shot']} FRAME {start + index}", fill="white")
    buffer = BytesIO()
    board.save(buffer, format="JPEG", quality=88)
    evidence = await _reference_asset(ctx, f"brand-motion-{cut['source']}-{start}-{count}", buffer.getvalue())
    result = await reference_quality.ask(
        ctx, "check_brand_take", FrameCheck,
        "独立审阅实际保留的镜头。前两张是核准首尾帧，最后一张板从左到右是实拍保留区间五帧。"
        "商品原图若存在位于中间。检查动作是否只发生一次、自然结束并达到指定状态。"
        "检出道具悬空、筷子和食物变形、餐盘消失、人物换位、衣着变化、商品外形漂移、过度假动作及意外换机位。"
        "reason和correction使用成片语言并指出具体画面。只有影响动作、商品、状态或下个镜头的差异才退回。"
        "失败时给单个动作与保持构图的可执行correction。",
        {"shot": shot.model_dump(), "language": language, "actual_frames": [start + index for index in picks]},
        [*pair, *(materials if shot.product_in_first_frame or shot.product_in_last_frame else []), evidence],
        max_tokens=1500, timeout=55,
    )
    check = FrameCheck.model_validate(result["report"])
    if not check.ok:
        return None, check
    tail_buffer = BytesIO()
    frames[-1].save(tail_buffer, format="JPEG", quality=90)
    tail = await _reference_asset(ctx, f"brand-tail-{cut['source']}-{start + count - 1}", tail_buffer.getvalue())
    return {"review_board": evidence, "review_tail": tail}, None


async def approve_joins(ctx, prefix, run, plan, cuts, hero, materials):
    from light.production import save_director_decision

    key = f"{VISUAL_REVIEW}:joins"
    boards = [cut["review_board"] for cut in cuts]
    saved = (run.get("director_decisions") or {}).get(key, {})
    if saved.get("cut_assets") == boards and saved.get("end_card") == hero and saved.get("approved"):
        return

    result = await reference_quality.ask(
        ctx, "check_brand_joins", JoinCheck,
        "按成片顺序比较实际镜头，每张实拍板从左到右为同镜起、过程、结束，最后一张为片尾。"
        "逐个比较上一镜最右帧和下一镜最左帧，特别检查同一时间地点内关键道具有无凭空变化、角色是否到场两次或凭空换位。"
        "以实际看见的证据指出首个错误的镜号。允许视角更换、道具出画及分镜明确交代过的时间跨度。"
        "片尾独立静帧允许换背景与还原完整商品摆放，商品本身必须能与原图对上。"
        "最后一张是片尾无字底图。商品名、广告语及音乐署名会在通过接镜后由后期加上。"
        "片尾右侧现在应有干净留白，不得因底图尚未叠字而退回。"
        "退回道具消失时，若原分镜明确要求其在画外，修正应收紧取景以排除该道具原来的区域。"
        "逐个尊重product_in_first_frame和product_in_last_frame。若为false，correction不得要求把商品拍进这个端点。"
        "通过时只填写ok和reason。失败时shot填写需要补拍的镜号（1起，片尾也有编号），"
        "reason和correction使用成片语言，用简短句子明确画面哪里不一致、怎样修图修动作。"
        "保持原分镜人物、台词和商品，修正必须可在原镜头时长内完成。",
        {"product_photos": len(materials), "language": plan.language, "end_card_stage": "unlettered_plate",
         "shots": [{"shot": i, "before": s.before, "after": s.after, "transition": s.transition,
                    "product_in_first_frame": s.product_in_first_frame,
                    "product_in_last_frame": s.product_in_last_frame,
                    "purpose": s.scene, "editing": s.edit_note} for i, s in enumerate(plan.shots, 1)]},
        [*materials, *boards, hero], max_tokens=2600, timeout=80,
    )
    check = JoinCheck.model_validate(result["report"])
    if not check.ok:
        if check.shot > len(plan.shots):
            raise RemakeStopped("接镜审查返回了分镜之外的镜号，已保留各镜素材。")
        raise JoinRejected(check)
    await save_director_decision(
        ctx, prefix, len(cuts), key, {**result, "approved": True, "cut_assets": boards, "end_card": hero},
    )
