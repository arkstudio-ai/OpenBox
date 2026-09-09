# 视频合成引擎选型：调研、实测与切换手册

> 2026-09-09。结论：**先用阿里云 IMS 云剪辑**做合成/渲染，**Remotion 作为已验证的自托管备选**。
> 两条路都跑通了同一条口播样片，spike 代码与成片在 `work/ims-spike/`、`work/remotion-spike/`。
> 本文是给未来「要不要切、切到哪、怎么切」的人看的：读完不用重做调研。

---

## 0. 一页结论

| 问题 | 答案 |
|---|---|
| 现在用什么 | IMS 云剪辑（`SubmitMediaProducingJob` + Timeline JSON），agent 不直接写 Timeline，中间加一层**编译器**把已知坑固化掉（§5） |
| 什么时候切 | IMS 效果做不出产品要的东西（字幕背景框、物理动效、自定义字体动效）、25fps 不可接受、或按分钟计费超过一台渲染机成本时 |
| 切到哪 | Remotion（§6）。已实测：视频重合成并行有效，10s 成片 4s，54s 成片 11s；license 走 Automators 档 |
| 不切到哪 | CapCut/剪映（无 API、剪映 6.0+ 加密、导出要 UI 自动化）、火山智能创作云（模板驱动，自由度不够）、HyperFrames（视频重合成不能并行，已被 Remotion 的 OffthreadVideo 解掉） |
| 架构不变量 | **自己持有时间线 JSON**，IMS 和 Remotion 都只是导出器。切引擎不改技能，只换编译器目标 |

---

## 1. 背景与痛点

### 1.1 bossip 时代（已弃用，教训保留）
- 每台 ECD 桌面（6c12g Linux，无 GPU）本地跑 ffmpeg + HyperFrames（Chrome 逐帧截图）。
- Chrome 渲染器和 codex、泄漏的 stdio MCP 进程、dev-browser 的 Chrome 挤在 7G cgroup 里，`OOMPolicy=continue` 先杀 chrome/ffmpeg，用户看到「渲染莫名失败」。
- 无 GPU：≥30s 的片前台跑超 120s 回合上限。
- 定时任务和交互任务抢同一台机器；桌面可能停机。
- 已解决的坑（别再踩）：hyperframes 钉版本 0.7.94（上游一天多版、CLI 参数会变）；`HYPERFRAMES_BROWSER_PATH=/usr/bin/google-chrome` 避免现场下载浏览器（大陆 28KB/s）；`@font-face` 走本地 Noto CJK。

### 1.2 openbox 现状（2026-09-01 视频原子化后）
- HyperFrames、media worker、SkillJob 耐久运行时已删除（`docs/VIDEO_ATOMIZATION_PLAN.md` §6）。合成 = agent 在用户桌面跑 `compose.sh`（ffmpeg concat + libass 烧 ASS）。
- 退役理由：合成不花供应商的钱；每用户独占桌面无排队；字幕样式是技能资产。
- 剩余内存压力主要来自 dev-browser 的 Chrome（6GiB action service、TasksMax 512→2048，见 `docs/DEPLOY.md` 2026-09-07）。
- **本次调研的触发**：想要 CapCut 级的花字/动效/转场，并同时交付 MP4 和可编辑草稿。

### 1.3 需求
1. MP4 成片 + 可编辑草稿都要。
2. 素材不出境（技能硬规则：禁图床/隧道/对外监听）。
3. 不想再养渲染机；定时任务要异步可靠。
4. agent 能低错误率地驱动。

---

## 2. 候选全景与判定

| 方案 | MP4 | 草稿 | 判定 | 一句话理由 |
|---|---|---|---|---|
| **阿里云 IMS 云剪辑** | 云端异步 | Timeline JSON + 官方 WebSDK 编辑器 | **采用** | 同账号 OSS 不出境、零运维、按分钟计费；两个已知 bug 有确定性规避（§4.2） |
| **Remotion** | 自托管无头 Chrome | React 组件 + Player 预览 + 开源时间线编辑器 | **备选，已验证** | OffthreadVideo 解决视频解码瓶颈；agent skill 生态最大；需 license |
| HyperFrames | 自托管无头 Chrome | HTML composition，浏览器即预览 | 不选 | 视频重合成只能单 worker；上游发版快；与 Remotion 同类但生态小 |
| Revideo（Motion Canvas fork，MIT） | 自托管 | 代码 | 备选的备选 | 无 license；但已并入 Midrender 商业路线，无 OffthreadVideo 等价物 |
| 火山引擎 智能创作云 | 云端 | 模板 | 不选 | 剪映同源效果最像，但模板驱动，自由时间线弱（用户已验） |
| capcut-cli + Windows 导出机 | UI 自动化点导出 | CapCut 草稿 | 不选 | 见 §3.1 |
| MLT / Shotcut | `melt` 无头 | .mlt 在 Shotcut 打开 | 不选 | 一份文件既渲又编，但效果上限低 |
| Diffusion Studio | WebCodecs | 代码即草稿，画布代码双向 | 观察 | 唯一为 agent 设计的编辑器；尚年轻 |
| OpenCut（MIT，84k star） | 自带 headless | 自家多端编辑器 + MCP | 观察 | 重写版未 production-ready，classic 可评估 |
| DaVinci Resolve 脚本 | `-nogui` 官方 API | Resolve | 不选 | 外部脚本需 Studio（$295/机）；目标用户不是剪辑师 |
| Shotstack / Creatomate 等海外云 API | 云端 | Studio SDK | 不选 | 素材出境，踩红线 |
| VectCut 云渲染 | 第三方渲 CapCut 草稿 | CapCut | 不选 | 素材出境、还原度未知 |

---

## 3. 关键事实（调研定论，带来源）

### 3.1 CapCut / 剪映
- **没有官方 CLI 或公开编辑 API**。所有「AI 剪 CapCut」方案都是直接读写本地草稿 `draft_content.json`，成片必须人在桌面端点导出。
- **剪映 6.0+ 草稿 AES 加密**，工具写的草稿在剪映 10.x 被报「内容已损坏」；CapCut 国际版仍明文，但 CapCut 10.x 也开始拒绝工具写入。国内用户群的 CapCut 草稿交付物本身不稳。
- CapCut 与剪映草稿格式约 95% 相同（`platform.app_source` cc/lv），但转场/蒙版/动画/贴纸/曲库的资源 ID 两边完全不同，写错渲染成空操作不报错。
- capcut-cli（renezander030，MIT，0.22.0，429 star）：纯 Node 零依赖，86 条命令，`describe` 出机器可读契约，`serve` 是 JSONL 队列跑批器，`compile spec.json` 可编译整份草稿。设计模式值得借鉴；但它只产草稿不出片，唯一 fixture-tested 版本是 CapCut 6.2.8。
  - 仓库：https://github.com/renezander030/capcut-cli ；版本矩阵 docs/version-support.md；加密说明 docs/jianying-encryption.md
- 生态：sun-guannan/CapCutAPI(VectCutAPI, 2.2k star) 是各 MCP server 的底座；pyJianYingDraft(4.3k) 自动导出只支持剪映 ≤6。

### 3.2 HyperFrames（HeyGen 开源）
- 机制：Playwright 开 HTML，每帧 seek GSAP 时间线截图，ffmpeg 编码；按 worker 切帧段并行。
- **固有瓶颈**：Chrome 同时 seek 多个 video 元素耗尽解码器，视频重合成官方建议退回单 worker → 口播（全是视频片段）并行失效。shader 转场并行无效（issue #677）。无 GPU 机器上空闲 worker 用 swiftshader 空转一个核。
- 若坚持 HyperFrames，可行的攻克路线：ffmpeg 出底片，Chrome 只渲透明图形层（无 video 元素→全速并行），最后 overlay；或预抽帧成 JPEG 序列。
- https://github.com/heygen-com/hyperframes

### 3.3 Remotion
- `OffthreadVideo`：用 ffmpeg 在浏览器外抽精确帧再喂页面，绕开浏览器解码与 seek 不精确问题 → 视频重合成可并行。**实测**：54.5s 成片，8 并发 11.4s，1 并发 52.9s（M 系列 15 核）。
- 慢路径：源视频关键帧稀疏时抽一帧要读整段。sd2 片段关键帧 4–10s，入库时 `ffmpeg -g 30 -keyint_min 30` 重编码可规避。
- Agent skill：28 个规则文件，skills.sh 装机量全球第 4。
- 草稿编辑器现成：designcombo/react-video-editor（"CapCut and Canva clone"，1.7k star，license 待查）、reactvideoeditor/free-react-video-editor。
- **License**：盈利组织 >3 人必须买；评估阶段免费；Studio/Player 预览不算 Render。
  - Creators：$25/座席/月；**Automators（适用 openbox）：$0.01/render，最低 $100/月，开发者不需座席**；Enterprise $500/月起。
  - Stripe 收美元，无国内通道；需公司外币卡或港币主体；invoice 走境外服务费入账。
  - 不买的替代：Revideo，或 HyperFrames + 预抽帧。
  - https://www.remotion.dev/docs/license/pricing ；https://github.com/remotion-dev/remotion/blob/main/LICENSE.md
- 本机坑：系统 Chrome 148 报 `Visited http://localhost:3000/index.html but got no response`，改用 Remotion 自带 chrome-headless-shell（93MB）即好 → 渲染机镜像里 `npx remotion browser ensure` 预烧。`getCanExtractFramesFast` 在 4.0.522 已不导出，用 `getVideoMetadata`。

### 3.4 阿里云 IMS 云剪辑
- API：`SubmitMediaProducingJob`（Timeline / ProjectId / TemplateId 三选一）+ `GetMediaProducingJob` 轮询；输出 `oss-object`。CLI：`aliyun plugin install --names aliyun-cli-ice` 后 `aliyun ice submit-media-producing-job`。
- 开通：控制台开通 IMS + 服务授权（读写 OSS 的服务关联角色）。未开通时 API 返回 403 `User not authorized to operate on the specified resource`，CLI 无开通命令。
- **实测**：9.5s 成片排队+渲染 22s；5s 单素材 10s；输出固定 **25fps**、h264/aac、yuv420p。花字入出场、字幕动效、下三分之一、fade 转场全部生效。
- Timeline 要点（`docs/timeline-configuration-description`）：
  - VideoTrackClip：`MediaURL`（OSS https 地址）或 `MediaId`；`In/Out`、`TimelineIn/Out` 秒；`X/Y/Width/Height` 支持比例 [0,1) 或像素；`AdaptMode` Contain/Cover/Fill。转场写在**前一个**片段的 `Effects: [{Type:"Transition", SubType, Duration}]`，SubType 有 fade/wiperight/perlin/random 等。
  - SubtitleTrackClip：`Type: "Text"`；`Content`；`Alignment`（TopLeft…BottomRight/Left/Center/Right）；`Font`（AlibabaPuHuiTi、KaiTi 等，或 `FontURL` 自定义）；`FontSize/FontColor/Outline/OutlineColour/BackColour`；`TextWidth` + `AdaptMode: AutoWrap` 换行；`AaiMotionInEffect/AaiMotionOutEffect`（36 种：fade_in、slide_down_in、zoomin_in、typewriter1_in…）+ `AaiMotionIn/Out` 秒；`EffectColorStyle` 花字（CS0001-000001~16、CS0002-000001~16、golden、skyline、neon_green…）。
  - **没有**字幕背景框参数（只有描边/阴影）。
- 文档：https://help.aliyun.com/zh/ims/developer-reference/timeline-configuration-description ；字幕特效 example-of-subtitle-effects-1 ；花字 flower-effect-example ；转场 use-cases/transition-effect-filter

---

## 4. 实测记录

### 4.1 同一条口播样片，两边各渲一次
内容：2 段 5s 竖屏视频 + 0.5s fade 转场 + 顶部花字（滑入 3s 后淡出）+ 4 条底部字幕 + 3s 起下三分之一滑入。八格对照图：`work/remotion-spike/out/compare.png`。

| | Remotion | IMS |
|---|---|---|
| 耗时 | 4.1s（本机 8 并发） | 22s（含排队） |
| 字幕背景框 | 有（圆角半透明） | 无此参数 |
| 动效 | spring 物理 | 预设枚举 |
| 帧率 | 24（跟源） | 25（固定） |
| 帧精度核对 | 第 118 帧 = 第二段第 10 帧叠第一段第 118 帧，数学正确 | 转场在 4.4–4.9s 间真实混合 |

### 4.2 IMS 两个历史 bug 的定位（用户之前遇到过）
**黑边**
| 配置 | 结果 |
|---|---|
| 16:9 素材进 9:16 画布，只写 `AdaptMode:"Cover"` | 上下黑边（等同 Contain；文档说默认 Fill 也未生效） |
| 同上 + 显式 `X:0,Y:0,Width:720,Height:1280` | 铺满 |
→ **规则：每个 VideoTrackClip 必须带显式几何，AdaptMode 才生效。** 编译层强制补。

**文字位置**
- X/Y 是文字框相对画布左上角的偏移，**锚点随 Alignment 变**：TopCenter 时 X=0 即水平居中、Y 为文字框顶边。
- Text 必须显式 `TextWidth`（比例）+ `AdaptMode:"AutoWrap"`，否则单行溢出。
→ 按此规则写，四处文字全部落在预期位置。之前的「位置不对」是 agent 不知道锚点语义，不是渲染 bug。

---

## 5. IMS 落地设计（当前路线）

> **编译层已落地（2026-09-09）**：`backend/video/timeline.py`（自有 schema）、`backend/video/ims_catalog.py`
> （枚举表，分 VERIFIED / KNOWN 两档）、`backend/video/ims_compiler.py`（编译器 + CLI），
> 测试 `backend/tests/unit/test_video_timeline_compiler.py`（32 例）。编译产物已在真实 IMS 跑通
> （JobId 82d3c5bb…，16s 出片，关键帧与手写版一致）。命令行：
> `cd backend && uv run python -m video.ims_compiler <timeline.json> --out-url oss://bucket/key --oss-region cn-shanghai`
> 示例输入 `work/ims-spike/openbox-timeline.json`。
>
> **平台原子工具已落地（同日）**：`backend/tool/video_compose.py`（actions: schema / validate / submit /
> status / wait / cancel）、`backend/video/ims_client.py`（tea-openapi 泛型 RPC，不引入 ICE SDK，因为它要求
> tea-openapi ≥0.4.5 与无影集成的 <0.4.0 钉定冲突）、`backend/video/compose_recovery.py`（挂在
> job_recovery 的定时补扫上）。复用 `video_jobs` 表（kind=compose）与 `file_assets`，**无迁移**。
> 配置 `video_compose`（region/endpoint/bitrate_kbps/poll_interval_seconds/daily_job_limit，见 `core/config.py`）。
> 已注册进 build agent、`video` 意图包、doom-loop 的 wait/status 白名单，技能 allowed-tools 已声明。
> 真机验证：工具直连 IMS，JobId bbb35a4c…，submit → 一次 wait → completed，OSS head 1,151,373 字节。
> 测试 `tests/unit/test_video_compose.py`（18 例）。

### 5.1 分层
```
技能层（video-production）   写「时间线 JSON」（我们自己的 schema，不是 IMS Timeline）
        │
编译层（backend/video/…）    时间线 JSON → IMS Timeline，固化 §4.2 规则、字体/花字/动效枚举校验
        │
平台原子工具 video_compose   照 video_generate 模式：幂等键、耐久 job 行、submit/status/wait/fetch、
                              25s 有界等待 + polling_paused、job_recovery 补扫
        │
IMS SubmitMediaProducingJob  素材 = OSS 上已有资产（不出账号）；成片回 OSS → share_file 交付
```

### 5.2 时间线 JSON（`backend/video/timeline.py`，以代码为准）
```jsonc
{
  "canvas":   { "width": 720, "height": 1280, "fps": 24 },
  "shots":    [{ "asset": "oss://bucket/key.mp4", "in_sec": 0, "duration_sec": 5, "fit": "cover",
                 "transition_out": { "type": "fade", "seconds": 0.5 } }],
  "captions": [{ "from_sec": 0.2, "to_sec": 2.4, "text": "…" }],
  "caption_style": { "bottom_ratio": 0.095, "max_width": 0.88, "style": {…}, "motion": {…} },
  "texts":    [{ "text": "…", "from_sec": 0, "to_sec": 3, "align": "center|left|right", "x": 0.0, "y": 0.125,
                 "max_width": 0.9, "style": { "font", "font_url", "size", "color", "outline", "outline_color", "color_style" },
                 "motion": { "in_effect", "in_sec", "out_effect", "out_sec" } }]
}
```
单位只有秒和画布比例，像素只在编译器边界出现。`extra="forbid"`：agent 塞不进 IMS 原生字段。
`spoken_preset(shots, cues, hook=, handle=)` 生成口播默认布局（花字 + 下三分之一）。
同一份 JSON 之后也编译成 Remotion props（`work/remotion-spike/src/Spoken.tsx`，待把 hook/handle 泛化成 texts）。

### 5.3 编译层固化的规则（`ims_compiler.py` 文件头逐条对应）
1. 每个视频片段补 `X:0, Y:0, Width:<canvas.w>, Height:<canvas.h>, AdaptMode:"Cover"`（口播默认裁满；需要留黑边时显式 Contain）。
2. 文字统一 `Alignment:"TopCenter"` + `X:0` 水平居中，Y 由「离底比例」换算成像素；`TextWidth:0.88`、`AdaptMode:"AutoWrap"`。
3. 字体白名单（AlibabaPuHuiTi 等）、`AaiMotionInEffect` / `EffectColorStyle` 枚举校验，非法值在编译期报错而不是渲完才发现。
4. 字幕背景框不可用 → 用 `Outline:3` + `OutlineColour:"#000000"` 兜底；产品若坚持底框，这就是切 Remotion 的第一个触发点。
5. 输出 25fps 固定；素材入库统一 24 或 25 皆可，对照测试按时间换算。
6. `ClientToken` = 幂等键；`OutputMediaConfig.MediaURL` 落 `oss://<bucket>/videos/<user>/<slug>/final.mp4`。

### 5.4 配置（三处，缺一不可）
| 项 | 在哪 | 说明 |
|---|---|---|
| `OSS_BUCKET` / `OSS_REGION` / `OSS_ENDPOINT` | env（本机 `.env` / 内测 gw2 的 `config/backend.env`） | 既有的素材中转桶。video_compose 的输入输出都在这个桶，没有单独的 IMS 桶 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` / `_SECRET` | env，或 `~/.aliyun/config.json` profile | `core/aliyun.py` 的既有加载链，OSS 和 IMS 共用 |
| `video_compose.{region,endpoint,bitrate_kbps,poll_interval_seconds,daily_job_limit}` | `openbox.json` | region 留空 = 跟 OSS_REGION；示例见 `openbox.jsonc.example` |
| IMS 开通 + OSS 服务授权 | 阿里云控制台，每账号一次 | 未开通时所有调用 403，工具会报「IMS 未开通」 |

实测现值（2026-09-09，云助手只读 grep gw2 `config/backend.env`）：`OSS_BUCKET=bossip`、`OSS_REGION=cn-shanghai`、
`WUYING_REGION_ID=cn-shanghai`、`WUYING_ENV_TAG=gw2`、`BILLING_MODE=shadow`、`APP_ENV=prod`（仅角标）；`openbox.json`
里尚无 `video_compose` 段。**注意 spike 用的是 `bossip-media-sh` 桶，内测后端用的是 `bossip` 桶**，同账号同区域；
IMS 的 OSS 服务授权是账号级的，两个桶都能读写，工具跟着 `OSS_BUCKET` 走，不需要为 IMS 单独配桶。

本机开发环境 `.env` 没有设 OSS_BUCKET，所以工具在本机会直接以「需要 OSS_BUCKET/OSS_REGION」拒绝，这是预期行为；e2e 脚本是进程内临时设的。

### 5.5 计费与用户确认（2026-09-09 落地）
- **价目**：`backend/billing/rates.json` 新增 `media` 段，IMS 普通模板中国内地官方价（来源 help.aliyun.com/zh/ims/video-clip）：
  480p 0.015 / 720p 0.03 / 1080p 0.06 / 2K 0.12 / 4K 0.24 积分/分钟，**不足 1 分钟按 1 分钟计，合成失败不计费**。
  档位按输出短边判定（720x1280 竖屏 = 720p）。要加利润直接改 `per_minute`，1 积分 = 1 元不变。
- **三个时点**（`backend/billing/media.py`）：`validate` 免费报价（`estimated_credits` / `minutes_billed` / `tier`）；
  `submit` 前在 `BILLING_MODE=enforce` 下检查会话所属 workspace 余额（`INSUFFICIENT_CREDITS` 拒绝），shadow/off 不拦；
  IMS Success 后按**成片实际时长**结算一条 `usage_events`（kind=`video_compose`，幂等键 `compose:<job_id>`），
  enforce 记账扣积分，shadow 只记不扣，时长缺失记 `unreported` 而不是白送。补扫路径同样结算。
- **用户确认**：技能新增第四张卡「合成确认」（`references/compose-timeline.md`）：prose 里贴分镜顺序与秒数、
  每条字幕与时间、横幅、转场、输出尺寸、`duration_sec`、精确 `estimated_credits`，选项
  `可以 / 改字幕或时间 / 换转场或动效 / 改用无特效拼接（ffmpeg）`；没点「可以」不许 submit。
  这是第一条真正落账的媒体计费路径；视频生成与 STT 的落账（BILLING_REVIEW_2026-09-07 的 B2'）仍未做。
- 账单页现有列表按 token 展示，媒体事件 `total_tokens=0`、`tokens` 里是 `duration_sec / minutes_billed / tier`；前端要单独渲染这类事件。

### 5.6 成本与限制
- 按输出时长计费，本次 3 条测试忽略不计；上量后与 §6 渲染机成本对比。
- WebSDK 编辑器 5.0 起需申请 license，做「用户可拖拽改草稿」时再申请。
- 排队时间不可控（实测 5–15s），不要放在同步回合里。

---

## 6. 切到 Remotion 的手册（备选路线）

> 2026-09-09 拍板：**现阶段只做 IMS**。Remotion 编译目标、`color_style` 到 Remotion 的映射、
> 字幕背景框等 IR 扩展、双编译器的同步维护，全部等到 §6.1 触发条件成立时再做，现在不预投。

### 6.1 触发条件
任一成立即可启动：产品要字幕底框/自定义动效；25fps 不可接受；IMS 参数语义再出不可规避的 bug；月账单 > 一台渲染机 + license。

### 6.2 步骤
1. **财务确认能付美元 Stripe**（公司外币卡或港币主体）。付不了 → 走 Revideo 或 HyperFrames+预抽帧。
2. 一台 **Linux** 渲染机（不是 Windows），CPU 为主 16c/32G 起，GPU 可选；镜像里 `npm ci` 钉 Remotion 版本 + `npx remotion browser ensure` + fonts-noto-cjk。
3. 编译层加 Remotion 目标：时间线 JSON → `SpokenProps`，复用 `work/remotion-spike/src/Spoken.tsx`。
4. `video_compose` 工具的后端换成渲染机队列（Postgres `SKIP LOCKED` 即可，compose 里已有 PG/Redis），保持 submit/status/wait/fetch 接口不变。
5. 素材入库时 `-g 30 -keyint_min 30` 重编码，避免 OffthreadVideo 慢路径。
6. 上线前订 Automators；评估期免费。
7. 草稿编辑：先用 Player 做只看预览；要拖拽再评估 designcombo/react-video-editor（查 license）。

### 6.3 注意
- 不要在用户桌面上跑 Remotion（回到 bossip 的 OOM 老路）。
- SKILL.md 里「HyperFrames 已退役，永不引回」是针对桌面本地渲染架构的决定，与在专用渲染机上跑 Remotion/HyperFrames 不是一回事；切换时把这条改写清楚，否则下一个执行者会拒绝。

---

## 7. Spike 复现

```bash
# Remotion（本机，需 Node ≥18；首次渲染会下 93MB headless shell，走代理）
cd work/remotion-spike && npm ci
npx remotion studio src/index.ts                          # 实时预览
npx remotion render src/index.ts Spoken out/final.mp4     # 10s 样片
npx remotion render src/index.ts Spoken60 out/final60.mp4 # 54s 压力测试
npx remotion still  src/index.ts Compare out/compare.png  # 八格对照图

# IMS（需 aliyun CLI + ice 插件 + IMS 已开通授权；素材在 oss://bossip-media-sh/ims-spike/）
cd work/ims-spike
./submit.sh timeline.json final.mp4          # 主对照
./submit.sh timeline-wide.json wide.mp4      # 黑边复现
./submit.sh timeline-wide2.json wide2.mp4    # 黑边规避
```
- `work/remotion-spike/RESULTS.md`、`work/ims-spike/RESULTS.md` 各有当次详细记录。
- `work/remotion-spike/src/Inspect.tsx`：本机没 ffmpeg 时抽任意 mp4 某帧成 png 的办法。

---

## 8. 未决事项
- [x] IMS 编译层第一版（§5.3 六条规则 + 枚举表）—— 2026-09-09，真实任务验证通过
- [ ] Remotion 编译目标：`Spoken.tsx` 的 hook/handle 泛化为 `texts`，与 IMS 共用同一份时间线 JSON
- [x] `video_compose` 平台原子工具 —— 2026-09-09，真机验证通过
- [ ] 内测环境（阿里云 gw2，`ai.bossipai.com.cn`）接入：本次 IMS 开通与授权就是在 gw2 所在的阿里云账号上做的，该账号即内测账号；gw2 的 `openbox.json` 加 `video_compose` 段即可。**目前没有独立的生产账号**（DEPLOY.md 的「生产环境标识」只是 2026-09-07 切的角标，价格仍是 0.10 元测试价、计费 shadow）；将来若另起生产账号，IMS 开通 + OSS 授权要在新账号重做一次
- [x] 技能层：第 8 步双路径 + 第四张确认卡 + `references/compose-timeline.md`（2026-09-09）
- [x] 计费：报价 / enforce 余额门 / 成功后按实际时长落账（2026-09-09，shadow 与 enforce 均有测试）
- [x] 内测验收（2026-09-09 18:47–18:58，gw2，账号 bbdwxh_admin，会话 `session_7YBXX3HEDBEP975WEM0D09KHAN`）：
  720p 两段生成 → STT 卡 → `validate` 报 `estimated_credits=0.03` → 正文展示方案 → 第四张卡四个选项齐全 →
  点「可以」后才 `submit` → 32 秒完成 → 成片卡出现、输出 `credits=0.03` → 账单新增
  `video_compose / ims-compose-720p / 0.03 / shadow`（前端显示「已统计」）。成片 14.4s = 8s + 7s − 0.6s 转场重叠，
  `planned == actual`，属预期；卡上应明说「含转场重叠后总长 14.4s」，已加进 references/compose-timeline.md。
- [ ] 视频**生成**的估价与落账（`video_generate estimate` 目前不返回金额 → 技能按「预计费用暂不可得」处理）：
  照 `billing/media.py` 的模式给 `rates.json` 加视频模型按秒×分辨率价目，这是 BILLING_REVIEW 的 B2'，下一项
- [ ] 观察：首稿确认卡在一次会话里出现两次（10:48 与 10:49，第二次是精简稿）。若用户第一次已点「可以」仍重问，
  属技能纪律偏差；若第一次选了缩短，重问是规则内行为。待复现确认
- [ ] 前端账单页渲染 `kind=video_compose` 的用量事件（按分钟而非 token）
- [ ] 真实 sd2 片段跑一遍 IMS（本次用的是合成测试素材）
- [ ] 财务：能否付美元 Stripe（决定 Remotion 备选是否成立）
- [ ] 观察：Diffusion Studio、OpenCut 重写版成熟度（半年后复评）
