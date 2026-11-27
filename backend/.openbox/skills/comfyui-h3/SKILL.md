---
name: comfyui-h3
description: 在 OpenBox 用 ComfyUI MiniMax H3 写分镜和原生对白。按需选择首帧、首尾帧和 R2V 图像、视频或声线参考，涵盖提示词、素材角色和任务恢复。
---

# ComfyUI MiniMax H3

通过 `video_generate(action="models")` 核对选择的模型和能力。模型 ID 为 `ComfyUI-MiniMax-H3`。素材用当前工作区的 asset ID。OpenBox 在后端上传素材、认证和取回作业。

配置双机工作台时，所有镜头沿用原版「视频生成 - MiniMax H3」Hybrid B25-49 工作流。人物镜头 10 步，锁版海报 12 步。按镜头提交，后端读取两台机器的实时队列和待上传任务，分配并记录执行机。用回执里的 job_id 等待或恢复。

旧版直连 Turbo 服务按所选模板确定主模型。使用画廊素材包的制作全程使用 Hybrid B25-49，普通镜头 10 步，已排好文字的动态海报传 `h3_profile="poster"` 使用 12 步。通用快速档按输入选择 FL2VA 8 步或 Ref2VA 4 步。Hybrid 和 Ref2VA 都能在同一请求中接收首尾帧和独立参考。有原片动作要迁移时传 `reference_video`，需要连续声线时传 `reference_audio`。素材齐备即可提交。

## 并行出图、逐镜生成与验收

按输入素材安排调用。同一商品图或已有角色参考可以同时供多个镜头使用。在一轮中分别调用最多 10 个独立的 `image_gen`，包括首帧、尾帧和海报。尾帧需要引用新首帧时，拿到其资产 ID 后再做。相邻镜头共用同一边界画面时复用该资产。

App 直接成片模式按确认分镜一次生成，镜头就绪就按剧本合成。仅开启完美成片时执行本节的内容检查与针对性补拍。每个镜头的素材、提示词和参数齐备就提交，可与其他独立原画放在同一轮。回执有 `background_review=true` 时，后台从首个镜头就绪起留 5 秒合并近同时完成的镜头，再读取画面、对白和时间，即使 Agent 正忙也会启动。同一次制作按实际验收的镜头累计时长计费。继续准备其余镜头，需要结果时，把全部待验收 ID 一次交给 `video_generate(action="await_ready", job_ids=[...])`。根据各段 `review` 对照动作、商品、画内文字和台词，只有缺失或不清楚的证据才单独复核。普通会话及旧作业收到原始片段后可合并调用素材分析。阿里云剪辑提交后用 `video_compose(action="await_ready", job_id=...)` 取成片。

## 依据素材选择模式

| 需求 | 素材角色 |
|---|---|
| 从零生成，或只指定动作落点 | 双机工作台或直连快速档可留空素材，或单传 `last_frame` |
| 从具体图像开始并延续动作 | I2V，`first_frame` |
| 在两张具体画面之间完成动作 | FL2V，`first_frame` + `last_frame` |
| 指定复刻原片的连续动作、运镜或切点 | R2V，用 `reference_video`。有关键帧未呈现的商品或人物细节时，可追加 `reference_image` |
| 从已有身份、材质或商品参考直接生成镜头 | R2V，可单独使用 `reference_image` |
| 用同一声线说不同镜头的原生台词 | R2V，`reference_audio` + `reference_image` 或 `reference_video` |
| 复刻中间动作，并锁定开场与收尾构图 | 同次传 `first_frame`、`reference_video` 和 `last_frame`。有独立商品图、声线样本时可追加 |

先区分制作原画的素材和拍这一镜需要的素材。用 `image_gen` 把角色、站位和当次商品做进原画。普通镜头从已确认的首帧起拍，指定了结束画面就同时传尾帧。原画已经给出了演员和商品时，`video_generate.input_assets` 只传相应关键帧，按基础三段写动作和台词。直连画廊模板增加独立参考时，Hybrid 会走参考时间线。用户要求原样复跑或对照指定参数时，从原作业复用素材和角色、提示词、画幅、时长及种子，仅调整用户指定的变量。

要保留原片特有的中途动作或切点时，传能看清这段表演的 `reference_video`。指定的声线、关键帧看不到的身份或商品细节也可各自加入参考。按自然段落拆分，连续动作可在 15 秒内写多个时间切点。混合模式沿用 R2V 六段写法，首尾帧分别约束第 0 帧和末帧。`<Picture N>` 仅给独立 `reference_image` 编号，`<Video N>` 描述实际参考片的动作。

## I2V 与 FL2V 的提示词

- 首行写首帧或首尾帧对齐，空一行进入正文。OpenBox 根据实际素材角色和对齐后的帧数补齐这行。I2V 从首图的角色、站位与构图自然往后写。FL2V 描述从首图出发、发生可见过渡、落到尾图的动作链。
- 正文依次写三个字段。动作、机位、对白和画内声写在同一镜头描述内。
  - `integrated_multimodal_description:` 从 `[Shot 1]` 开始，先交代风格和构图，再按顺序写谁做什么、哪一步触发哪一个反应。
  - `overall_soundscape:` 写整段环境声、动作声和非语言声。
  - `non_diegetic_music:` 写画外配乐。纯原声用 `N/A`。
- 第一个镜头默认从 0 秒开始。切换机位写递增的单点，例如 `[Shot 2] At 00:03.500, the camera cuts to a low view of the same door.` 同一镜内用有方向和幅度的运镜，例如缓慢小幅推近门把手。

## R2V 的提示词

使用六段结构写清楚素材实际发挥的作用。

1. `subject_definitions:` 定义人物、重要道具和环境为 `<Subject N>`。用 `<Video N>`、`<Picture N>`、`<Audio N>` 对应实际输入的参考视频、图像和独立声音。每种类型单独按素材顺序从 1 编号。
2. `summary:` 根据实际任务选方括号前缀，例如 `[reference generation + audio reference]`、`[video editing]` 或 `[video continuation]`。一句话概括本段结果和参考关系。
3. `retention_analysis:` 对每个参考写清保留范围。用 `fully_preserved` 保留确定内容，`partially_preserved` 保留指定动作或切点，`attribute_transfer` 转移特征，`weak_reference` 只借鉴氛围。替换物件时说明旧动作、新物件各从哪里来。
4. `detailed_description:` 按播放顺序写 `[Shot 1]` 和实际切换时间。把可见动作、相机、对白和画内声放在对应镜头里。
5. `overall_soundscape:` 写生成的环境声和同期音效。
6. `non_diegetic_music:` 写配乐或 `N/A`。

例如，`<Video 1>` 提供演员抬起手臂的连续动作，`<Picture 1>` 提供新道具。把接触、运动方向和落点写进相应动作。OpenBox 参考视频送入画面。需要参考声线时，独立提供音频。

### 跨镜头声线

同一人物或旁白的镜头复用同一个已有、获确认的单人音频资产。把 `reference_audio` 与画面参考一起提交，在定义中写 `<Audio 1> supplies the speaking timbre of the off-screen narrator (S1).`，在 `retention_analysis` 写 `<Audio 1>: reference`，并在实际开口的位置再绑定同一个 `(S1)`。本段的新台词放在 `<d>` 内。多人场景用各自样本，并用转头、动作和轮流开口来区分。验收对比相邻片段的音色、口音和响度，同时核对台词与口型。 [MiniMax 官方音色参考示例](https://www.minimax.io/news/minimax-h3-open-source)

### 把原生对白写在动作上

先从所选样片听出旁白音色、每段说几句、哪句紧跟动作、哪些停顿留给观众看，再用当次产品和核准文案排句。轻快对答可写成 `As the prop lands, off-screen (S1) asks brightly in one quick breath <d>[Chinese] …</d>. The painter (S2) answers on the very next beat <d>[Chinese] …</d>.` 用具体开口时机、重音和句尾落点指导表演。商品特写如有两项叙事任务，可让触碰时的短句接上看见效果时的反应。保留作者指定的无声镜头和用户指定的原话。需要精确节奏时，区分原生镜头长度和最终剪入时长，给最后一个字留足时间。

## 对白、画面与验收

- 用英文写视觉和声音说明。说话人用稳定的 `(S1)`、`(S2)` 标记，人物、口型、语气和动作写在标签外。把指定普通话原文放在 `<d>[Chinese] 我们出发吧!</d>` 内。分清谁先开口、谁在听。画外音写明声音在画外，画面里的其他人保持自然闭口。无对白用 `no_speech=true`。
- 商品宣传段先用 `image_gen` 把已确认的商品名、店名、价格或口号排进原画，查对字形、数字、单位和安全区。把原画作为 `first_frame`。需要锁住版式且动作可回到初始构图时，可把同一张图或经过核验的同版式图再作为 `last_frame`。
- 锁版动态海报用同一张核准原画作首尾帧，传 `h3_profile="poster"`。在双机工作台或直连服务上，若只有局部需要动，第一次提交时就传 `poster_motion_region=[左,上,宽,高]`。四项均按画面比例填写，看这张原画选取实际运动范围，避开所有固定文案。提示词写固定机位和框内的一处动作。检查整段广告字，可用 `video_analyze(action="screen_text")` 按时间抽帧，发现跳字时查看相邻帧。IMS 剪接使用已带广告字的镜头。
- 提交人物镜头前查参考里的底边字幕、铭牌和道具。源视频有字时用能保留动作的干净片段，或从本次人物与机位制作干净关键帧。人物说话时用正向描述交代背景，例如平整的裸墙和连续木纹。
- 常用 `duration=2–15` 秒，显式 `9:16` 或 `16:9`，分辨率 `480p`、`720p` 或 `768p`，`generate_audio=true`、`watermark=false`。生成 24fps，帧数会对齐。参考视频每段 2–15 秒。以当前模型菜单为准。
- 每次拍摄使用稳定的幂等键。将本次就绪的片段交给 `footage_analyze`，需要字幕时间时传 `transcribe=true, word_times=true`。对读不清的位置再用 `video_analyze(action="screen_text", source="<资产 ID>")` 或定点抽帧。听觉上的同音转写应结合上下文与原稿核对，有实际漏句、说错商品或可见错字才针对该段修正。

完整字段、音视频保留标记、跨切对白和原创示例随本 Skill 一起返回。手册包含文生、首帧、尾帧、首尾帧、动作加声线参考和动态海报，可直接按当前用户的素材改写。[H3 提示词手册](references/prompt-writing.md) · [OpenBox API 和参数](references/api.md)
