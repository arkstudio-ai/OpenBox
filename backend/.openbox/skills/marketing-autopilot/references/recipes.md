# 形态 → 配方（B4）

`video_analyze` 对热点视频给出 `form`（`video/analysis.py`：口播 / 画面+旁白 / 产品展示 / 剧情 / 混剪 / 字幕型 / 其他）
以及 `structure[]`（分段秒数与内容）、`script_text`、`on_screen_text[]`、`recreate_elements{presenter, scene, pace, caption_style, music}`。
技能按 `form` 查下表决定怎么复刻。口径按拍板：**复刻到「能看、像那个类型」即可**，预算与条数优先。

模型档位由模版 `model_tier` 固定（`autopilot/tiers.py`：高 `video-sd-720p-proⅠ`@720p / 中 `wan3.0-video`@720p / 低 `MiniMax-H3`@768p），
画幅统一 9:16，不许换模型。每段先 `video_generate(action="estimate")` 累加预算，超模版 `credits_cap_per_run` 即停。

| form | 段数 × 时长 | 生成方式 | 台词 / 声音 | 字幕 | 合成 | 质检（tolerances） |
|---|---|---|---|---|---|---|
| **口播** | 2–4 段 × 5–8 s（跟 `structure`，总长 ≤ 热点时长，≤ 30 s） | 现有口播工艺：`prompt-recipes.md` 骨架 + 人物一致性（同一 `presenter` 描述与 seed / 参考图） | 台词按 `script_text` 重写成自己的版本（不抄原文），每段一句完整话；模型直接开口念 | `spoken_preset` 底部字幕，逐段 cue = 转写文本 | `video_compose` `spoken_preset`，转场 `dissolve` 0.3 s | **STT 相似度 ≥ `tolerances.stt_similarity`（默认 0.85，比对话里的 0.90 放宽）**；时长偏差 ≤ `duration_deviation_sec`；超出重生 ≤ `max_regenerations` 次，再超出弃用该条 |
| **画面+旁白** | 3–5 段 × 4–6 s | 每段一个画面 prompt（来自 `structure[i].what` + `visual_style`），无人物开口 | 旁白：优先选**带音频生成**的模型档位念旁白；没有则**只出画面 + 字幕**（TTS 工具缺口，见计划 §2.6） | 顶部标题（`on_screen_text[0]` 改写）+ 底部旁白字幕 | 顺序拼接，转场 `dissolve`/`slide`，背景音乐留空 | 不做 STT（无人物台词）；只查时长偏差与每段是否成功 |
| **产品展示** | 3–4 段 × 4–5 s（开场 → 细节 → 使用 → 收尾） | 画面 prompt 强调 `recreate_elements.scene` 与产品特写；可用客户素材图做首帧（`creator_context`） | 无口播；卖点做字幕 | 花字卖点 1 条/段（≤ 12 字） | 顺序拼接，转场 `zoom`/`dissolve` | 只查时长与成功数；**声明必选 `ai`**，带货再在简介写清 |
| **剧情** | 3–4 段 × 5–7 s（起 → 转 → 结） | 每段一个场景 prompt，人物描述固定；对白改成字幕不让模型开口（口型/一致性差） | 无配音 | 对白字幕 + 场景说明花字 | 顺序拼接，转场 `cut`/`dissolve` | 只查时长与成功数；剧情不追求连贯度 |
| **混剪** | 4–6 段 × 3–4 s | 同一主题不同画面（`topics` 词做变体） | 无 | 一句顶部标题贯穿 | 快切 `cut`，可 `zoom` 入场 | 只查成功数；单段失败直接丢，够 4 段即成片 |
| **字幕型** | 1–2 段 × 8–12 s | 背景画面 1–2 个（静态感、低运动） | 无 | **正文全部走字幕**：`on_screen_text` 改写成 3–6 条 cue，等分时长 | `spoken_preset` 或自定义 cue | 只查时长；文字合规自审（`prompt-recipes.md` 合规节） |
| **其他** | — | 不复刻 | — | — | — | 记入报告「形态未识别，跳过」，换下一条热点 |

## 通用规则

- **选题过滤**：热点标题/话题命中模版 `topics_blocklist` 直接跳过；`content_forms` 不含该 `form` 的也跳过；`risk_notes` 提到版权/人物肖像/广告法的，改写时避开对应元素。
- **文案**：标题 ≤ 30 字（创作者中心上限，`douyin-desktop-publish`），简介 1–3 句 + 3–5 个话题词；跟热点的把热点词填 `hot_word`。
- **预算**：每条视频 = 分析（≈0.15）+ 各段生成估价 + 合成（按时长档）+ 采集（命中缓存为 0）。运行前把 `videos_per_run × 单条估价` 与 `credits_cap_per_run` 取小决定条数；每段提交前再累加校验，超限**立即停止、已成片的照常发布并在报告写明**。
- **重生策略**：只重生不合格的那一段，最多 `max_regenerations` 次；弃用一条视频不影响同次运行的其他条。
- **发布**：按模版 `publish_mode`（`auto` → `desktop_publish`，`package` → `douyin_publish`）；`degrade=true` 时本条改投稿包并写进报告。
- **报告字段**（D4）：热点来源与标题、`form`、分段摘要、每条花费、成片卡、标题/简介/话题、发布结果或降级原因、被弃用条目的原因。
