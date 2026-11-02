# 你拍我剪（M3）：spike 结论、实现与验收

> 2026-11-02。方案 `LIGHT_MODE_PLAN.md` §5，实施拆解 `LIGHT_MODE_IMPL_PLAN.md` §5（F1–F9）。全部在 light-test（后端 `20261102-light-6c2d97f0` 起）实测。对外文案写 bossip。

## 1. 三个 spike 的结论

测试素材：本机用真实样片 + macOS 语音合成拼出的 5 段「手机实拍」（竖拍 H.264 带人声、横拍 16:9 带人声、竖拍 HEVC .mov、iPhone 式「16:9 像素 + 90° 旋转标记」HEVC .mov、竖拍无声），放 `oss://bossip/light/spike/20261102/`。脚本在后端容器里直接调 IMS / DashScope。

| 问题 | 结论 | 实测 |
|---|---|---|
| IMS `RegisterMediaInfo` + `GetMediaInfo` 能否做媒体探测 | **能**。传 bucket 的 https URL（无需签名），返回时长、宽高、`Rotate`、编码、帧率、音轨列表 | 每段约 2 秒（首次 8.5 秒）；HEVC/mov、旋转标记、横拍都正确；无声素材 `AudioStreamInfoList` 为空 |
| IMS `SubmitSnapshotJob` 能否采帧 | **不能直接用**：报 `MissingTemplateId`，账号下没有截图模板（`ListSystemTemplates` 的截图类型为空） | 改用 **OSS 自带 `x-oss-process=video/snapshot,t_<ms>,f_jpg,w_480,m_fast,ar_auto`**：签名 GET 直接回一张 JPEG，0.1–0.26 秒，HEVC/mov 与旋转素材都正确（`ar_auto` 按旋转出图） |
| DashScope fun-asr 能否直接吃视频 URL | **能**：把视频的签名 URL 当音频 URL 传，引擎自己取音轨；结果带句子时间戳 | 2 段带人声 1.5–2.8 秒，中文准确；无声视频报 `have no audio`（实现里按 `has_audio` 跳过） |
| HEVC / mov 能否作 IMS 合成输入 | **能**：H.264 竖拍 + HEVC 竖拍 + HEVC 旋转 + H.264 横拍混剪一条，`AdaptMode=Cover` | 12 秒成片 12.8 秒合成完成；旋转被正确处理，横拍居中裁 9:16，原声保留，中文花字正确 |
| 音轨（F4） | IMS `AudioTracks` + `Effects:[{Type:Volume,Gain}]` + `AFade In/Out` 都被接受；视频片段上的 `Volume` 生效 | 两次合成 6–17 秒完成；`volume: 0` 的片段那一段只剩背景声 |

## 2. 实现（F1–F9）

| 包 | 落点 | 说明 |
|---|---|---|
| F1 媒体探测 | 迁移 `pj06c7d8e9f0`（`file_assets.meta JSON`）、`video/media_probe.py`、`api/assets.py` `/complete` | 视频上传完成后台探测写 `meta`（`duration_sec / width / height`（已按旋转换算显示方向）`/ rotation / orientation / codec / fps / has_audio / media_id`）；`ensure_meta` 按需补探 |
| F2 采帧 | `tool/footage_analyze.py` | OSS `video/snapshot` 签名链接直接喂视觉模型，每段 4–10 帧（约 2 秒一帧），不经桌面、不落盘 |
| F3 转写 | 同上；`video_transcribe` 放开 `video/*` | fun-asr 读视频 URL；`_dashscope_transcribe` 现在返回 `sentences[{from_sec,to_sec,text}]` |
| F4 音轨 | `video/timeline.py` `Timeline.audio[]`、`Shot.volume`；`video/ims_compiler.py` | 音床（BGM / 配音）各占一条 `AudioTrack`，超出成片时长自动截；BGM 暂不提供（IMS 公共媒资授权未确认），方案卡写「原声」 |
| F5 合成完成链 | `video/compose_recovery.py`（阶段 A 已上） | 合成在本轮等待预算外完成时，补扫唤醒对话取回成片 |
| F6 费率 | `billing/footage.py`、`rates.json` `media.footage` | 素材分析：整批素材按分钟向上取整，1 积分/分钟，每批只收一次（`footage_analyze:{session}:{素材集合}`）；成片：你拍我剪会话里的合成按阶梯结算（≤60 秒 5、≤180 秒 9、之后每分钟 +2），账单标题「素材分析」「成片」 |
| F7 技能 | `.openbox/skills/footage-edit/`（SKILL.md + `references/structure-templates.md`） | 不调 `video_generate` / `image_gen`；`footage_analyze` 一次读全部素材 → 一张方案卡（镜头、字幕、花字、原声、两行价格）→ `video_compose` |
| F8 入口与 App | `entries.json` `footage_edit` 转主入口（免费档可用）；`LightInputBar(allowVideo, maxPhotos: 10)` | 相册多选照片和视频（`pickMultipleMedia`，最多 10 段），单段 >200 MB 跳过并提示；视频按流上传，逐段显示「正在上传第 n/N 段」 |
| F9 通知 | `notifications/*` 新 kind `footage_edit_ready / footage_edit_failed`；`tool/video_compose.py` `poll_compose_job` | 你拍我剪会话的合成落定即推送，链接回对话 |

新工具 `footage_analyze`（`sandbox_required=False`）注册在 build agent 与 video 意图包里。

## 3. 验收（方案 §6，2026-11-02 18:02–18:09，iPhone 17 Pro 模拟器连 light-test）

免费档新账号 `light-full-1102b`（注册即 14 积分，无套餐、无云电脑）：

1. 首页「你拍我剪」→ 开始 → 点「探店」。
2. 相册选 5 段 15 秒素材（门头竖拍、招牌面竖拍带口播、店内**横拍**带人声、多菜 **HEVC .mov**、炸鸡竖拍）→ 发送，逐段上传约 5 秒。
3. agent 调 `footage_analyze`（5 段并行，约 70 秒）→ 方案卡（第一版只用了 22 秒素材；回复「做成 60 秒左右，五段都用上，先门头开场」后 14 秒出新卡：8 个镜头、3 句字幕、店名与「招牌牛肉面 ¥28」花字、原声、「成片 5 积分（素材分析 2 积分已收）」）。
4. 点「就这么剪」→ 合成 IMS 2 分 48 秒 → F5 唤醒贴片 → 结果卡；推送 `footage_edit_ready`。
5. 账单两行：`素材分析 2`、`成片 5`，与方案卡一致；对话轮的 LLM 计费另计（约 0.3 积分）。
6. 成片：60.0 秒、720×1280、带原声；门头开场 → 招牌面（带价格花字）→ 横拍店内居中裁 → HEVC 多菜 → 收尾，店名全程。

**耗时**：上传完成 → 成片出现在对话约 6 分钟（分析与出卡约 2 分 30 秒、一次改方案 14 秒、合成与贴片 3 分 34 秒，其中 IMS 合成 2 分 48 秒），**超过 5 分钟的验收线约 1 分钟**。主要时间在 IMS 合成排队与渲染（同样 60 秒成片在 spike 里是 12 秒素材 12.8 秒）与分析的视觉调用。可做的提速见 §4。

发现并已修：方案卡里有一句字幕超出画面宽度（>14 字）→ 技能改为每条 ≤12 字、长句按逗号拆成多条。

## 4. 未做 / 下一步

- **提速到 5 分钟内**：上传完成即在后台跑探测 + 采帧 + 转写（现在只跑探测），agent 调 `footage_analyze` 时直接取缓存；视觉调用帧数从 4–10 降到 4–6；合成用 IMS 的加急或 480p 预览先出。
- BGM：等 IMS 公共媒资授权或自备曲库后，在方案卡上加「配乐」一行，`Timeline.audio` 已就绪（口播段把音床 `volume` 压到 0.15）。
- 口播 TTS（方案 §5.4 第 5 条）未做。
- 测试机无云电脑：「发到抖音」未在你拍我剪成片上实测。
