# MiniMax H3 提示词手册

H3 把一段视频的画面、动作和声音放进同一条提示词。先决定每份素材在镜头里做什么，再选下表的模式。英文写画面和声音说明，保留用户提供的台词、歌词和画面文字的语言。以下试写用纸风车、修理台等示例说明语法。实际生成读取当前任务的商品、角色、文案和素材。

## 1. 选模式与写第一行

固定使用 Hybrid B25-49 的任务也按素材角色选提示词。仅有关键帧时写基础三字段，带独立图像、视频或声音参考时写 R2V 六字段，两种写法使用同一份主模型权重。

| 创作任务 | 模式 | 提示词起点与实际输入 |
|---|---|---|
| 整段从零创作 | T2VA，文生音视频 | 直接写三字段。直连快速档传空的 `input_assets` |
| 让一幅图接着演 | I2VA，首帧 | 首行绑定第一张图到第 1 镜 0.00 秒。OpenBox 传 `first_frame` |
| 一段动作连接两幅画面 | FL2VA，首尾帧 | 首行将第一张图绑定到开场、第二张绑定到最终镜头末尾。OpenBox 传 `first_frame` 与 `last_frame` |
| 从合理的前一状态落到指定画面 | L2VA，尾帧 | 首行将唯一一张图绑定到最终镜头末尾。直连快速档单传 `last_frame` |
| 借用动作、人物、产品外观、声线，或改编、续写原视频 | Ref2VA，常称 R2V | 直接写六字段。OpenBox 传实际的 `reference_image`、`reference_video`、`reference_audio`，音频要搭配视觉参考 |

基础模式使用图时，**第一行是对齐指令，空一行再写正文**。最终镜头编号取正文里最后一个 `[Shot N]`，结束时刻按实际帧数除以 24，保留两位小数。例如本工作流请求 7 秒得到 175 帧，尾时是 7.29 秒。OpenBox 根据素材角色补齐首行，给工具提交三字段即可。镜头同时传动作参考和首尾帧时，改用 R2V 六字段。用 `<Video 1>` 描述动作节奏，在分镜正文描述从开场画面到收尾画面的变化，每个独立商品图再按顺序写 `<Picture N>`。[MiniMax 基础模式](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md) · [ComfyUI 原生工作流](https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-native)

## 2. 基础三字段，各写什么

| 字段 | 内容 |
|---|---|
| `integrated_multimodal_description:` | 从 `[Shot 1]` 写实拍或动画风格、起始景别与人物站位，再把动作、反应、运镜、谁在何时开口、现场可以听见的声响写进同一条时间线 |
| `overall_soundscape:` | 用一到四句写贯穿本段的环境声、物件声和非语言声，例如桌面轻响与衣袖擦过工具台。完全静音按需求写 `N/A` |
| `non_diegetic_music:` | 用一到三句写观众听到的配乐，例如乐器、节拍、强弱起落。没有配乐写 `N/A`。角色听得到的收音机或现场演奏放进镜头正文 |

### T2VA 试写与 L2VA 变体

下面是完整的文生音视频提示词。把场景换成自己的即可。需要只指定尾图时，交给 L2VA 的正文要写到指定尾图的姿态，CLI 会把唯一一张图绑定到最后一镜。例如尾图是两只手放开已经合拢的工具盒，就把第二镜改成两手按实盒盖、松开、停在画面两侧。

```text
integrated_multimodal_description: [Shot 1] Live-action medium view in a repair shop. A mechanic on the left lays an open red tool case on the bench. Her assistant passes the missing socket from the right. As it touches her palm she says (S1), in a clipped, cheerful voice, <d>[Chinese] 齐了!</d> [Shot 2] At 00:03.250, the camera cuts straight down to the case. She slides the socket into the empty slot, presses the lid shut and lifts both hands to either side. They come to rest there.

overall_soundscape: A loose socket rolls briefly. The lid closes with a dry snap.

non_diegetic_music: N/A
```

在**手写原生工作流**里需要完整对齐行时，可用本地 Skill 附带的脚本离线打印。以二镜、175 帧的结尾为例，在该 Skill 目录运行下面代码。输出就是三种模式各自需要的逐字首行。正文与首行间空一行。

```python
from runpy import run_path

format_prompt = run_path("scripts/h3.py")["plain_prompt"]
body = "integrated_multimodal_description: [Shot 1] Opening. [Shot 2] At 00:03.250, the camera cuts to the case."
for mode in ("i2v", "fl2v", "l2v"):
    print(mode, format_prompt(body, mode, 175).splitlines()[0])
```

### 可直接提交的 I2VA 正文，首图是一只手扶住静止风车

```text
integrated_multimodal_description: [Shot 1] Live-action close-up at a worktable. A paper pinwheel fills the left half of the frame. A thumb holds the lower end of its wooden stick. The maker tilts the stick into the breeze. The nearest blade lifts and the wheel makes one full turn. An off-screen woman (S1) with a bright Mandarin voice says in an off-screen voiceover: <d>[Chinese] 看, 风来了!</d> The maker's lips stay closed. The camera gently pushes in as the wheel picks up speed.

overall_soundscape: Paper rustles close to the microphone. A faint draft continues by the window.

non_diegetic_music: N/A
```

**换基础模式时改变叙事起点。** 文生模式自己建立整个空间。首帧模式从图中的姿势往后演。首尾帧模式交代手怎样接触物件、怎样转动、怎样落到尾图。尾帧模式从合理的前一状态反推。对于两张构图相差大的图，写出真正发生的交接、位移或切镜。硬让两张图互相变形，容易在中间产生多余的手或物件。[MiniMax 基础模式](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)

### 可直接提交的 FL2VA 正文，首尾用同一张已核准原画

```text
integrated_multimodal_description: [Shot 1] A locked frontal product photograph on pale paper. The printed line "风起了" stays sharp at the top. The brass pin at the centre holds its position. The free tip of a ribbon lifts slightly, settles back into the opening shape, and comes to rest. The light is constant. A clear off-screen woman (S1) says in an off-screen voiceover, in one brisk breath: <d>[Chinese] 风一吹, 它就转!</d>

overall_soundscape: One close rustle of ribbon over quiet room tone.

non_diegetic_music: N/A
```

这例需要原画上本来就有对应文字。当前用户的商品名、单位和宣传语应在生图时排入原画。OpenBox 的 `h3_profile="poster"` 适合锁版海报。写清固定机位、完整文字的位置，以及唯一需要运动的画面区域。将同一原画用作首尾帧时，直连服务可同时传 `poster_motion_region`，把该区域落实到采样器。只写提示词和首尾帧时，中间仍可能重绘文字。按时间检查整段，文字闪动时查看前后相邻帧，核对原画。

## 3. 镜头、对白与声音的写法

- 第一镜从零开始，直接写 `[Shot 1]`。真正换视角时另起 `[Shot 2] At 00:03.250, the camera cuts to a top view of the worktable.` 后续切点递增且落在有效片长内。一个连续动作里逐渐拉近可沿用同一镜。
- 运镜写明类型、方向、幅度和速度。镜身向前用 `push in`，原地变焦用 `zoom in`，水平转头用 `pan`，整机侧移用 `truck`，跟随人物用 `tracking shot`。给一个可观察目标，例如从手上的扣件移到已经扣紧的接头。
- 每个实际发声的人按发声顺序分配 `(S1)`、`(S2)`，同一段内始终复用该编号。初次开口写音色、口音、语速和动作。合说可以用 `(S1,S2)`，画外旁白和演唱同样有身份。只听声音的配乐按声源写。
- 台词逐句写成 `<d>[Chinese] 好, 就现在!</d>`。语言标记和要说的字在标签内。说话人的编号、表情和演法在外。保留核准原话。整理参考转写时用能表达语气的简洁标点，听不清的原音标 `[unclear]`。唱词用同样边界，在标签外写谁唱、怎样唱。可见字另用英文双引号逐字列出，例如 `the label reads "向上"`。
- 同一人画外说话，写 `says in an off-screen voiceover`，随后交代画面里对应人物闭口或正在做什么。现场对话写可见的开口和对方的反应。同一句跨**同一段视频内**的切镜时，用 `<scenetrans>` 标出前半末尾、后半起点，并说明同一声音延续。有意在视频结尾截断才用 `<cutoff>`。独立提交的下一段需重新提供声线条件。
- 轻快对话看三个落点：谁在第一个动作上开口，第二人看到什么立刻接话，最后一个字落在哪里。例如递上物件时问，接住时答，按下时只留一声“咔哒”。保留为悬念或笑点设计的停顿。原生口播的长度可调整台词密度、表演与镜头时长。工作流的采样步数影响采样过程。
- 当前 Workbench 使用 CFG 1。把要求改写成镜头内应该看到的内容，例如连续素墙、固定机位、纸面印字清晰。先检查喂进模型的原图和动作参考，画框铭牌或源片自带的字幕可能在新镜头继续出现。[ComfyUI 中文指南](https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-prompt-guide) · [MiniMax 基础模式](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)

## 4. 全参考 R2V 六字段

1. `subject_definitions:` 逐行定义会在后面引用的内容。`<Subject N>` 是要复用的人、物、场景或特征。`<Picture N>`、`<Video N>`、`<Audio N>` 指真实输入，三类各从 1 开始按各自顺序编号。一图可供多人，一人可从一张图取脸、从一段视频取动作。仅供人物外观的图片可写在人物定义内。图片本身承担分镜、构图或关键帧时单列定义。多格参考板标明每格方位和供哪镜使用。
2. `summary:` 用方括号标明任务，并用一句话写成片做什么。`reference generation` 借动作或身份。`keyframe completion` 绑定具体画面。`video editing` 直接修改输入视频。`video continuation` 承接原结尾。`audio reference` 借声线或节奏。`audio reuse` 使用原信号。实际同时使用的类型用 ` + ` 连接。直接编辑源视频时指明目标是该视频的编辑版本。
3. `retention_analysis:` 每个需跟踪的标签写清出现在哪一镜、保留哪些特征以及怎样变化。视觉用 `fully_preserved`（完整保留所定义的特征）、`partially_preserved`（部分沿用）、`attribute_transfer`（将某特征转给另一主体）、`weak_reference`（只借大致氛围）。音频用 `fully_copy`（整条原信号作最终声音）、`partially_copy`（保留一段或某些声部）、`reference`（借音色、韵律等特征）、`weak_reference`（仅借大类或氛围）。这些是生成提示。交付时仍要核验音轨。
4. `detailed_description:` 先用一两句定整体风格，再写 `[Shot 1]`。让每个标签在对应动作、画面或发声的地方出现。谁从哪里进入、手拿什么、对手何时反应，要能从画面看出来。后续切镜用单点时间。
5. `overall_soundscape:` 交代环境声、动作声。音频参考对哪种声响生效也写清。
6. `non_diegetic_music:` 写听众能听见的配乐，或 `N/A`。[MiniMax 全参考模式](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)

### 可直接提交的 R2V 正文，换商品、借动作、换词保留声线

例子实际输入一段手递物件的视频、一张当前机械铅笔商品照、一段获准的单人说话录音。三份资产分别接 `reference_video`、`reference_image`、`reference_audio`。镜头里的新台词和产品按本次任务替换。

```text
subject_definitions:
<Subject 1> is the seated demonstrator in <Video 1>, identifiable by her green cuff. She is speaker (S1).
<Subject 2> is the silver mechanical pencil in <Picture 1>, with a hexagonal barrel and a brass clip.
<Video 1> supplies the handoff and its change from a wide view to a close view.
<Audio 1> supplies the clear, lively Mandarin timbre of <Subject 1> (S1).

summary:
[reference generation + audio reference] <Subject 1> accepts <Subject 2>, clicks it once and shows its tip, following the hand movement in <Video 1>.

retention_analysis:
<Subject 1> ([Shot 1], [Shot 2]): fully_preserved - keep her green cuff and visible identity.
<Subject 2> ([Shot 1], [Shot 2]): fully_preserved - keep the barrel shape and brass clip from the product photo.
<Video 1> (movement and cut): partially_preserved - retain the handoff, wrist turn and cut to the hand. The item in her hand is now <Subject 2>.
<Audio 1>: reference - carry its speaking timbre into the newly written Mandarin lines.

detailed_description:
Natural daylight, live-action tabletop demonstration. [Shot 1] <Subject 1> reaches in from the right as a hand gives her <Subject 2>, matching the handoff in <Video 1>. Her fingers close around the silver barrel. In the lively voice from <Audio 1>, (S1) looks up and says <d>[Chinese] 接好了!</d>
[Shot 2] At 00:02.750, the camera cuts to her thumb pressing the brass cap. There is one clear click. She turns the pencil toward the viewer and immediately adds, in the same voice, <d>[Chinese] 看, 笔尖出来了!</d>

overall_soundscape: Low workshop room tone. A single dry click lands as the thumb reaches the cap.

non_diegetic_music: N/A
```

**跨片复用声音。** 为每位需要连续声线的人准备获准的清晰单人音频。各段复用同一个音频资产并重新绑定角色、音色和本段新词。多人场面用转头、递物、视线和轮流开口指定谁在说。OpenBox 的动作视频只接画面流，声音参考需单传实际音频。在本地 CLI 中，需要参考视频自带声音时使用其专门的带声选项，并按实际接线核对音频编号。单独的首尾帧模式依靠文字描述声线。交付前连听相邻片段，检查音色、语速和响度。[MiniMax 全参考模式](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md) · [MiniMax 发布说明](https://www.minimax.io/news/minimax-h3-open-source)

## 5. 工作流扩展与交付检查

- **中间姿势有指定时刻。** 在多帧 ComfyUI 图中给 Add Guide 接图，用 `frame_idx = round(秒数 × 24)` 选中帧，例如 2.5 秒对应第 60 帧。希望提示词能指名那张图时，也把它接入 `ref_images` 的对应槽，再在六字段里写哪一镜经过哪幅构图。只接 Add Guide 的图能引导那一帧，文本编码器没有对应的 `<Picture N>` 槽。OpenBox 普通首尾帧接口适合只约束起点和终点的任务。[ComfyUI 多帧参考](https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-multiframe)
- **用控制视频修动作。** 姿态管骨架、深度管空间、Canny 管轮廓。选有对应输入的 Fun ControlNet 工作流，把控制视频接入，再用文字指定主体外观、光线和对白。局部重绘的遮罩中，1 是要重生成的区域。用指定原画锁商品字时，文字区应保留。[ComfyUI Fun ControlNet](https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-fun-controlnet)
- **风格嵌入。** 已安装兼容嵌入的 ComfyUI 可在正向提示中写 `embedding:文件名`（省略扩展名）。例如 `embedding:minimaxh3_blooming_flowers` 指向社区的花开风格嵌入。写真实商品广告时，按用户需要选择。海报配置是采样图的参数，两者独立。[ComfyUI 提示词嵌入](https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-prompt-guide)
- **提交前用半分钟对稿。** 对照输入素材逐个核对编号，逐句核对说话人和批准的字，核对切点早于实际片尾，把最重要的动作放进最终会剪入的时间。复刻模板要记录原片的动作与接话关系，把原商品和旧促销替换成当前用户输入。
- **成片验收。** 用实际画面核对手和商品、海报文字与角色人数，用实际音轨核对白与接话，并复用已有检查。ASR 出现同音异字时对照听感和确定的原稿。发现真正的缺陷后，只修正对应素材、镜头或剪点。OpenBox 在阿里云 IMS 剪入已经做好原生对白与海报字的片段。
