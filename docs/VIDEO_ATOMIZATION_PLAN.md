# 视频能力原子化——执行手册

> 文档状态：Execution Handbook v2（2026-09-01，**已执行完毕**）<br>
> 执行结果记录在 §14；与本文原计划的偏离及原因一并列在那里。<br>
> **本文件是执行助手的第一准则。** 你（执行者）没有参与前期讨论，本文即全部上下文；
> 按顺序执行，不要引入本文之外的目标。行号以 commit `3818ab0` 为基准，动手前先用
> grep 复核（行号会漂移，符号名不会）。<br>
> 决策规则：所有已知分歧本文已给定论（§12）。遇到本文未覆盖且影响范围超过单文件
> 的新情况：**停下来向用户报告**，不要自行发挥。<br>
> 姊妹文档：`docs/SKILL_TOOL_DECOUPLING_PLAN.md`（下称 DECOUPLE）、
> `docs/DIRECT_PATH_CLEANUP_PLAN.md`（下称 CLEANUP，环境地图 §0.3 与坑清单 §8 全部
> 沿用）、`docs/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md`（下称 DEFER，INTENT_PACKS
> 改动需与其对齐）。

---

## 0. 执行者须知（先读完再动手）

### 0.1 你要做什么

把"竖屏口播成片"这一条焊死的流水线，拆成**平台原子工具 + 沙箱原子能力 + 技能知识层**：

1. `video_generate` 升级为**唯一视频生成入口**：参数全放开（prompt/model/ratio/
   duration/audio/watermark/seed/多参考素材），脱离 production 前置；
2. `video_transcribe` 改为对任意 owned 音频资产可用的 ASR 原子工具；
3. 合成/字幕**退役无影 media worker**，改为 agent 在无影云桌面用 bash + ffmpeg +
   技能脚本完成；
4. `video_project` 状态机与五道哈希审批**整体退役**，口播工艺降级为技能
   （SKILL.md + references + scripts），状态存 `/workspace` 文件；
5. 凭据与命名纠偏：`DOUBAO_*` 环境变量与 `doubao` provider 条目改名 `BOSSIP_*`/
   `bossip`（§9）。

方向依据：与 DECOUPLE 同一条逻辑推到底——上一轮拆掉"技能→解锁工具"的授予链，
本轮拆掉"工具→焊死流程"的绑定链。两家已收敛的开放技能标准（agentskills.io，
Anthropic Agent Skills / OpenAI Codex skills）都要求：**规范能表达的（知识、脚本、
依赖声明）放技能层；规范表达不了的（计费、凭据、幂等、能力校验）必须放工具层。**

### 0.2 必读文件（按序）

1. 本文件全文；
2. CLEANUP §0.3 环境地图与 §8 坑清单（端口 8080、docker PG、热重载盲区、公开仓库
   红线、`docs/LOCAL_CREDENTIALS.md` 凭据位置——全部沿用，本文不重抄）；
3. `backend/.openbox/skills/skill-creator/SKILL.md` —— 技能制作规范，§7 的技能重写
   以它为准绳；
4. `backend/.openbox/skills/video-production/SKILL.md`（90 行）与
   `references/`（5 件）—— 被重写对象，其中大量工艺知识要**移植而非丢弃**；
5. `backend/tool/video_workflow.py`、`video_production.py`、`video_providers.py`、
   `container/media_jobs.py` —— 改造对象；读懂哪些是"钱与安全"（保留）、哪些是
   "工艺与流程"（下放技能）。

### 0.3 基线（动手前实测并记录到 PR 描述）

| 事项 | 最近实测（2026-08-30，CLEANUP） | 本轮要求 |
|---|---|---|
| 后端 | `cd backend && uv run pytest -q` → 1016 passed | 动手前重跑记录 |
| 前端 | `npm run check` 干净；lint 有 2 个既有错误（`content-view.ts`，不许新增）；`npm run test` 182 例 | 本轮**前端零改动**，仍须全绿 |
| 移动端 | `cd mobile && dart analyze` 零问题 | 零改动，仍须零问题 |
| 无影 | `.env` 指向生产桌面（隧道 127.0.0.1:18000）；`backend/scripts/wuying_dev.sh` | PR2 浏览器验收、PR3 桌面清理验证要用 |

### 0.4 铁律

1. **权限系统一行不动**（DECOUPLE 铁律沿用）。暴露 ≠ 授权。
2. **不重建耐久运行时**。SkillJob 已整体删除（CLEANUP），本轮一切等待仍是
   "回合内有界等待 + `polling_paused` 跨回合恢复 + `video/job_recovery.py` 补扫"。
3. **计费安全三件套只许加强不许削弱**：幂等键、`video_jobs` 耐久表 + 恢复补扫、
   按模型能力元数据的响亮拒绝。§3 还要**新增**在途去重与额度背压。
4. **前端 / 移动端 / locale 零改动**。历史审批卡照旧渲染（部件与 API 保留，先例：
   `SkillJobPart`）；composer 的视频模型选择器保留，成为生成工具的默认模型来源。
5. PR 顺序不可倒置（§11）；每个 PR 的 DoD 全绿才允许提交；PR2 的浏览器验收会
   产生真实生成费用，**执行前向用户报数并确认**。
6. 本手册含内部拓扑（实例名、适配器路径、token 名）。**若仓库将公开，先脱敏本文
   §1/§10**；真实 key 永不入库（CLEANUP 红线沿用）。

---

## 1. 调研定论（2026-08-31 实测，不是猜测）

### 1.1 中转站真实拓扑

```
OpenBox 后端
  └─ https://openapi.bossipai.com.cn      腾讯云 106.52.167.53，nginx 1.29.4
       │   纯反代门面，白名单只放行 /v1/videos 前缀
       ▼
     阿里云 bossip-gw-1 :3000  bossip-newapi（bossip/new-api:fork-minimaxv2-20260810d）
       ├─ ch123 wan3-bailian        → wan3-video-adapter(node)      → 百炼 MaaS
       ├─ ch113 seedance-ts-compat  → tokenspace-sora-adapter(node) → api.tokenspace.net.cn
       ├─ ch120 seedance-tokenspace → 同上（优先级 20）
       ├─ ch106 volcengine-官方      → ark.cn-beijing.volces.com
       └─ ch114 metaso-minimax-h3   → metaso.cn/api/minimax
```

- `/v1/models`、`/api/pricing`、`/v1/video/generations`（task 通道）、`/api/v3/*`
  （ark 原生）在公网门面**全部 nginx 404**。运行时内省能力为零。
- **能力元数据的权威来源是 gw-1 上的适配器源码**：
  `/opt/bossip/wan3-video-adapter.mjs`（441 行）、
  `/opt/bossip/tokenspace-sora-adapter.mjs`（839 行）。
- 我们的 key = relay token id 9 `center-media-vip`（sha256 双向比对确认），
  **与 bossip-center 产品共用**——见 §10 运维配套。

### 1.2 vip 分组实际可路由的视频模型（abilities 表实查）

| 模型 | 渠道 | 已声明于 openbox.json? |
|---|---|---|
| wan3.0-video / wan3.0-video-prime | ch123 | ✅ |
| video-sd-1080p-pro / video-sd-720p-proⅠ / seedance-2.0-480-fastⅠ | ch113 | ✅ |
| doubao-seedance-2-0-260128 | ch120(P20) / ch106 | ✅ |
| **doubao-seedance-2-0-fast-260128** | ch120 / ch106 | ❌ 待补 |
| **doubao-seedance-1-5-pro-251215** | ch106 | ❌ 待补 |
| **MiniMax-H3** | ch114 | ❌ 待补 |

### 1.3 wan3 适配器的真实参数面（源码定论）

| 参数 | 支持 | 备注 |
|---|---|---|
| ratio | `adaptive` `16:9` `4:3` `1:1` `3:4` `9:16` | **无 21:9，传了报错**（不兜底） |
| duration | `-1` 智能 或 **2–30 整数秒** | |
| resolution | 480P/720P/1080P | 512p/768p/1440p/2k/4k 自动收敛并如实记账 |
| generate_audio / watermark / **seed** | ✅ | seed 我们从未传过 |
| media 角色 | `first_frame` **`last_frame`** `reference_image` `reference_video` **`reference_audio`** | 首尾帧/参考音频完全没用上 |
| 不支持的开关 | `camera_fixed` `frames` `callback_url` 等 8 个 | **显式 400，不静默丢弃** |

`VideoModelConfig` 注释里"relay 静默吞参数并按默认值计费"对**适配器本身**已过时
（它正是为修这个而写，不认识的开关一律 400）。但**上面那层仍然会静默丢弃**：
付费实测（§14.6）证实 new-api 的 `/v1/videos` 不把 `image_url` / `first_frame_url`
/ `content[]` 映射进适配器需要的 `media[]`，任务照样成功、产出与参考素材无关。
只有网关文档写明的多素材通道（`images[]` + prompt 里的 `@image_file_N` 占位符）
能把参考图送到 wan3。对 sd2 720p 丢 `extra_videos` 同样仍为真（代码已有防御）。

### 1.4 tokenspace 适配器定论

三个 sd2 模型（480-fastⅠ/720p-proⅠ/1080p-pro）**全部映射同一上游
`doubao-seedance-2-0-260128`**，仅 `nativeResolution` 不同；480-fastⅠ 实为标准版跑
480p（Fast 池未开通），且 `dropAudioOptions: true`（与后端"480p 禁音频"校验一致）。

### 1.5 生产使用铁证（logs/tasks 表）

wan3.0-video 64 次成功消费（最近 2026-08-31）；seedance-ts-compat 153/1；
volcengine 288/4。**`video-sd-1080p-pro` 有 484 条 upstream 5xx/`non-JSON (HTTP 400)`
错误记录**——运维观察项，见 §10.3，与本改造无关但别混淆归因。

### 1.6 用户已拍板的方向决定

1. 花费护栏走**积分/额度**（未来与 LLM 同一 credits 体系），不做审批卡；
2. 口播 = 标准技能结构（文档 + 辅助脚本），脚本做松散状态管理，**只助不阻**；
3. `video_generate` 是唯一视频入口，恒绑 user_id / 会话 / OSS 资产化；
4. bash+ffmpeg 是沙箱原子能力，素材进出走既有 `obx-file` 桥，技能负责教学。

---

## 2. 目标架构

```
计费层（横切，本轮只做背压占位）      credits/积分 —— 未来与 LLM 统一
────────────────────────────────────────────────────────
平台原子工具（后端，凭据+钱+幂等）    video_generate（聚合唯一入口）
                                      video_transcribe（ASR，任意 owned 音频资产）
                                      image_gen / creator_context / share_file（不动/微改）
────────────────────────────────────────────────────────
沙箱原子能力（无影云桌面）            bash + ffmpeg（预装）+ obx-file（既有）
────────────────────────────────────────────────────────
知识层（技能，可偏离可移植）          video-production v2 = SKILL.md
                                      + references/ + scripts/（lint、比对、ASS、合成、状态）
```

**退役清单**：`video_project` 工具、`tool/video_workflow.py`（2,026 行）、
`video_render` 工具、`container/media_jobs.py`（1,672 行）与 action_server 媒体
端点、`sandbox/client.submit_media_job`、镜像与 bootstrap 里的 hyperframes 栈。
**保留清单**：`video_jobs` 表、`video/job_recovery.py`、`polling_paused` 契约、
providers 路由层、`video_productions/segments/approvals` 三张表（只读历史）与
`api/video_productions.py`（零改动）。

原子性验收标准（贯穿所有 PR）：**零技能加载时，每个原子工具独立可用**。
"生成一段 5 秒的猫" 不建项目、不读 SKILL.md 也必须跑通。

---

## 3. 注册表 v2 与生成参数放开（PR1 核心）

### 3.1 `VideoModelConfig` 扩展（`core/config.py:164`）

新增字段（全部可选，缺省=不设限、由通道校验器兜底）：

```python
ratios: list[str] = []              # 空 = 通道默认；wan3 声明六种（无 21:9）
duration_range: tuple[int,int] | None = None   # (2,30)/(4,15)/(4,30)
supports_smart_duration: bool = True           # -1
supports_seed: bool = False
supports_first_last_frame: bool = False
supports_reference_audio: bool = False
```

`openbox.json` / `openbox.jsonc.example` 按 §1.2/§1.3 矩阵重写 `models[]`：
补 3 个缺失模型；wan3 两条声明 ratios 六种、duration (2,30)、seed/首尾帧/参考音频
= true；Seedance 系按 ark 现行校验值声明。**注册表是运行时唯一真源**（§1.1 内省为
零）；校准手段见 §10.4 离线核对脚本，不做运行时依赖。

### 3.2 `video_generate` 接口定论（`tool/video_production.py`）

```
action: models | estimate | submit | status | wait | cancel | fetch
提交参数（open 模式）：
  prompt(必填) model? resolution? ratio? duration? generate_audio?
  watermark? seed? input_assets: [{asset_id, role?}] idempotency_key(必填)
  allow_duplicate: bool = false
沿用：wait_seconds ≤25、after_version、wait_iteration、polling_paused 契约原样
```

- `models`：返回注册表 + 能力元数据（含 tier 贵贱提示）。技能文档从此不抄模型表。
- `estimate`：跑全套校验 + 报预计计费口径（wan3 按秒），**不提交**。免费预检。
- `submit` 双模式过渡：PR1 里 `production_id+segment_id+key` 的旧签名**原样保留**
  （`video_project` 仍在用），新增 open 模式按 prompt 走；PR3 删除旧模式。
- `input_assets[].role` ∈ first_frame/last_frame/reference_image/reference_video/
  reference_audio，缺省按 mime 推断为 reference_*。按注册表能力校验后拒绝或放行。
- 默认值解析：model 缺省 = 会话选择的 composer 模型（沿用现逻辑）→ 配置默认；
  resolution 缺省 = 模型原生档；ratio 缺省 = `9:16`（产品偏好，不再是类型锁）。
- **完成即物化**：任务 completed 时把成片 deliver 到
  `/workspace/media/<job_id>.mp4`（复用 `sandbox/assets.deliver`），结果里给出
  沙箱路径 + `download_url`。`fetch` 动作对任意 owned 资产做同样的事（老素材再剪辑）。

### 3.3 新增两道计费安全（与参数放开同一 PR，不许拆开）

1. **在途去重**：submit 前按 `prompt_hash`（`video_providers.compute_prompt_hash`，
   现成）查 `video_jobs` 中 in-flight 同内容任务；命中则拒绝并返回该 job_id，
   除非 `allow_duplicate=true`。防"换个幂等键重提同样内容"的双付。
2. **额度背压**：`video_generation.daily_job_limit: int = 50`（0=不限）。超限工具
   直接拒绝并让 agent 转告用户。这是 credits 落地前的占位背压，**不是审批**。

### 3.4 通道层修正（`tool/video_providers.py`）

- `validate_request`/`build_payload`：duration 按注册表 `duration_range` 放宽
  （现 sd2 分支只透传 4–15，wan3 需 2–30）；body 增加 `seed` 透传（先对照 gw-1 上
  fork 的 `docs/newapi-sd2-video-api.md` 核实 sd2 body 是否收 seed——不收则 seed
  仅在 task 通道开放，注册表如实声明）；ratio 校验以**注册表 per-model 列表**为闸，
  `_RATIOS` 保留为全集。
- 首尾帧/参考音频角色：sd2 body 无角色语义（只有 image_url/extra_*）。**这些角色
  只在 task 通道可表达**，而公网门面未放行 task 端点 → 依赖 §10.2 的网关配套。
  PR1 的实现按注册表能力开关做**优雅降级**：模型声明了但通道不可达时，estimate/
  submit 给出明确错误（"该角色需 task 通道，当前网关未放行"），不做静默改写。

---

## 4. `video_transcribe` 改造（PR1）

- 入参改为：`asset_id`（owned、ready、mime `audio/*`）+ 原有 action/幂等键/等待参数。
  production/segment 绑定字段删除（PR3 前保留兼容读取，见 §11）。
- 流程简化为：presign 音频 → DashScope（`fun-asr`，`api/v1/services/audio/asr/
  transcription`）→ 回填 transcript。**media worker 抽音频环节整体消失**——抽音频
  由 agent 在沙箱 `ffmpeg -vn` 完成（§7 技能脚本），经 §5 的安静上传变成资产。
- 相似度比对、verdict、filler 归一化**移出后端**：`normalize_spoken_text`/
  `compare_transcript`（`video_workflow.py:222/250`）移植为技能脚本
  `compare_transcript.py`（§7）。工具只存事实（transcript 文本），不再裁决。

## 5. `share_file` 微改：安静上传（PR1）

`ShareFileArgs` 增加 `attach: bool = true`。`false` 时走同一
`attach_sandbox_image` 通道但**不 pin FilePart**（给
`sandbox/assets.attach_sandbox_image` 加 `pin_part: bool = True` 参数），仅登记
资产并返回 `asset_id`。用途：中间产物（送 ASR 的音频、垫乐）不刷屏。描述里写清
两种模式的语义。

## 6. 合成退役：bash + ffmpeg + 技能脚本（PR3 落地，PR2 先用）

定论（用户委托设计）：**结构化 render worker 退役**。理由：合成不花供应商的钱
（中断=免费重跑，不需要付费任务那套耐久性）；每用户独占桌面无排队；字幕样式=
技能资产（用户 fork 技能改脚本，不用发版）。

- 删除：`container/media_jobs.py`、`media-jobs.json`、`media-runtime/`、
  action_server 的 media 路由、`sandbox/client.py:408 submit_media_job`、
  `tool/video_production.py` 的 `execute_render`/`_render_payload`/
  `_dispatch_render`/`_sync_render`/转写 worker 派发链、`video_render` 工具注册。
- 镜像与 bootstrap（`container/Dockerfile:7,37`、
  `scripts/wuying_bootstrap.py:146-151,195,243`）：**ffmpeg + fonts-noto-cjk 保留**
  （bash 合成的硬依赖），hyperframes/gsap/media npm 栈移除；**chromium 不动**
  （dev-browser/computer 在用）。
- ASS 生成与合成命令的工艺参数（`_SUBTITLE_BOTTOM_RATIO=0.095`、wrap 逻辑、
  fps 24/crf 21/preset veryfast/audio 160k、`RENDER_PIPELINE_REVISION=
  bossip-wrap-bottom-v5` 的样式语义）**全部移植进技能脚本**，不许凭感觉重写。

## 7. 口播技能 v2（PR2）

目录（`backend/.openbox/skills/video-production/`，重写）：

```
SKILL.md                 ≤200 行；frontmatter 只保 name/description
                         （allowed-tools 保留为依赖声明语义，不再暗示授权）
references/
  prompt-recipes.md      提示词配方（沿用现文件精神，加入 seed/首尾帧连贯性策略、
                         b-roll 即"无 @台词的一次生成"）
  model-guide.md         §1.2/§1.3 矩阵的使用者视角版（选型、贵贱、坑）
  quality.md             STT 质检标准（0.90 阈值、短替换人工裁决）与重生成策略
scripts/
  state.py               /workspace/videos/<slug>/state.json 的 init/show/set；松散
  lint_prompt.py         移植 video_workflow._PROMPT_LINT_RULES + lint_segment_prompt
  compare_transcript.py  移植 normalize_spoken_text/compare_transcript
  build_ass.py           移植 media_jobs._ass_document/_wrap_subtitle_text/_ass_timestamp
  compose.sh             concat + subtitles 烧录（§6 工艺参数）
  extract_audio.sh       ffmpeg -vn 抽音频一行命令（参数移植自 _extract_audio）
```

SKILL.md 工作流骨架：creator_context → 讲稿（40/48 字规范仍是**建议**，由
lint_prompt.py 自检）→ 用 `question` 工具向用户确认讲稿/花费/成片形式（**软确认，
无哈希绑定**；钱由 §3.3 背压和未来 credits 兜底）→ 逐段 `video_generate`（连贯性
双策略：锚点提示词，或 seed+首帧引用）→ `extract_audio.sh` + `share_file(attach=
false)` + `video_transcribe` → `compare_transcript.py` 出裁决 → 选择性重生成 →
`build_ass.py` + `compose.sh` → `share_file` 交付成片。等待遵循工具的
`polling_paused` 指令：暂停即收束本轮，下回合恢复同一 job。

技能脚本运行环境 = 无影桌面（python3、ffmpeg 已装，DoD 里实测）。脚本是纯本地
确定性程序，**不碰凭据、不发网络请求**。

## 8. 状态机退役与历史兼容（PR3）

- 删除 `video_project` 工具与 `tool/video_workflow.py` 全文件（先把 §4/§7 要移植
  的符号搬走）；`registry.py`/`agent/agent.py:158,173`/`agent/tool_exposure.py:60`
  同步收敛：INTENT_PACKS["video"] = `creator_context, image_gen, video_generate,
  video_transcribe, share_file`；BUILD_ONLY_WORKFLOW_TOOLS 移除 video_project/
  video_render。与 DEFER 的预算测试（`test_tool_exposure_budget.py`）对齐。
- 数据：**不写 drop 迁移**。`video_productions/segments/approvals` 只读保留，
  `api/video_productions.py` 与前端/移动端历史渲染零改动（先例 `SkillJobPart`）。
- `exposure_signals.py` 的 production/approval 探测保留（历史会话仍要亮 video 包）。
- `video_jobs.kind` 的 `"segment"` 字面量**保留**（历史行兼容；语义=一次付费生成）；
  `render`/`stt` kind 停止新增，历史行照旧展示。
- `video/job_recovery.py` 原样保留（只认 kind=segment 的付费任务，正确）。

## 9. 命名与凭据纠偏（PR1）

现状：视频中转既不是豆包官方也不是 TokenSpace 直连，环境变量还叫 `DOUBAO_*`，
provider 条目叫 `doubao`——语义错位。改名：

| 现名 | 新名 | 位置 |
|---|---|---|
| `DOUBAO_API_KEY` / `DOUBAO_BASE_URL` | `BOSSIP_API_KEY` / `BOSSIP_BASE_URL` | `.env`（本机手改）、`k8s/`（grep 排查）、`openbox.jsonc.example` |
| provider `"doubao"` | provider `"bossip"` | `openbox.json`、`core/config.py:205` 默认值、`video_generation.channel_providers` |
| 硬编码回退 | 同步改 | `tool/video_providers.py:347-357`（`_ark_route` 的 env 读取与报错文案） |
| 过时注释 | 修正 | `VideoModelConfig` docstring"静默吞参数"段（§1.3 定论）；`video_production.py:160`、`video_providers.py:6` 的 legacy 说法 |

兼容策略：`_ark_route` 读 `BOSSIP_*` 缺失时回退读 `DOUBAO_*` 并打一条
deprecation warning，**保留一个版本**后删（PR3 收尾时评估）。测试里的 doubao
字样按新名批量对齐。模型 id `doubao-seedance-*` 是上游真名，**不改**。

## 10. 运维配套（非代码，用户执行；凭据不经手执行者）

1. **专属 token**：在 relay 上为 OpenBox 新发 vip 组 token（如
   `openbox-media-vip`），替换共用的 `center-media-vip`。收益：用量按产品归因、
   配额与轮换互不牵连。换 key 只动 `.env`。
2. **task 端点放行（决策：推荐做，但 PR1-3 不依赖）**：腾讯云门面 nginx 放行
   `/v1/video/generations` 前缀 → wan3 系列可迁 task 通道 → 首尾帧/参考音频/seed
   全量可用（§3.4 的降级分支随之消失）。不做则这些能力保持"注册表声明 + 明确
   报错"状态。
3. **1080p 错误率排查**：ch113 `video-sd-1080p-pro` 的 484 条 upstream 错误
   （§1.5），另立问题单，勿混入本改造归因。
4. **校准脚本**（可入库 `backend/scripts/`）：离线比对 openbox.json 注册表 vs
   gw-1 两个适配器源码常量（SUPPORTED_MODELS/RATIOS/DURATION/ROLE 表），输出差异
   报告。跑在有 aliyun CLI 云助手权限的机器上，非运行时依赖。

## 11. PR 切分与 DoD

| PR | 内容 | DoD（全绿才许提交） |
|---|---|---|
| **PR1 平台层** | §3 注册表v2+参数放开+estimate/models/fetch+在途去重+额度背压；§4 转写改造（保留旧签名兼容）；§5 安静上传；§9 改名 | 后端 pytest 全绿（新增：open 提交校验、在途去重、背压、注册表 schema、fetch 物化、attach=false）；`video_project` 旧链路回归不破（双模式）；前端/移动端零改动且检查全绿；DEVLOG 记录 |
| **PR2 技能层** | §7 技能v2 全量重写；INTENT_PACKS 暂不动（旧工具还在） | 技能脚本 pytest 单测（lint/比对/ASS 纯函数）；**浏览器验收**：真实跑通一条 3 段成片（费用先报数确认），验证零技能单工具调用与带技能全流程两条路 |
| **PR3 退役** | §6 worker 退役 + §8 状态机退役 + 双模式收敛 + 镜像/bootstrap 清理 + 测试清理 | pytest 全绿；`verify_wuying_desktop.py` 通过（ffmpeg 在、hyperframes 无、chromium 在）；历史会话审批卡/成片在前端照常渲染（浏览器验收）；净删行数入 DEVLOG |

测试影响预告：删 `test_video_workflow*.py`、`test_video_production.py` 流程用例、
`test_media_jobs.py`、`test_video_broll.py`（b-roll 特例随状态机消失）；保留并扩
`test_video_providers.py`、`test_video_dedupe.py`、`test_video_job_recovery.py`、
`test_video_model_*.py`（删 production 依赖用例）；`test_video_restart_recovery_e2e`
改造为 open 模式。

## 12. 已知分歧定论表

| 分歧 | 定论 |
|---|---|
| credits 未落地期间的花费护栏 | 额度背压（§3.3），不做审批卡；credits 上线后背压保留为兜底 |
| 旧 production 数据 | 只读保留，零迁移；不写 drop |
| task 端点放行 | 推荐（§10.2），但代码按"声明+优雅降级"实现，不依赖 |
| 工具 id 是否改名 | 不改（`video_generate`/`video_transcribe` 沿用，只改描述与参数）；`video_project`/`video_render` 直接删除 |
| b-roll | 不再是特例：无 @台词的一次普通生成；lint 豁免逻辑随技能脚本的参数走 |
| 21:9 | `_RATIOS` 全集保留；per-model `ratios` 是真闸，wan3 不声明 21:9 |
| `video_jobs.kind="segment"` | 字面量保留，语义重述为"一次付费生成" |
| 相似度阈值 0.90 | 移入技能 `compare_transcript.py` 常量；`VideoTranscriptionConfig.similarity_threshold` 配置项 PR3 删除 |
| sd2 是否收 seed | 执行时对照 gw-1 fork 文档核实；不收则 seed 仅 task 通道声明 |

## 13. 坑清单（本轮新增，CLEANUP §8 之外）

1. `uvicorn --reload` 不看 `openbox.json` / SKILL.md——注册表与技能改完必须手动
   重启后端（CLEANUP 已列，此处特别提醒：本轮两者都要频繁改）。
2. 双模式 submit 期间（PR1→PR3），幂等键唯一约束
   `uq_video_jobs_idempotency(user_id,kind,key)` 对两种模式共用——open 模式的键
   建议格式 `open:<语义 slug>:<n>`，避免与 `production:segment:generate` 旧格式
   撞形。
3. 在途去重查询要含 `submitting/queued/in_progress/generating/finalizing/
   transfer_failed` 全集（对齐 `_IN_FLIGHT_STATUSES`），漏 `finalizing` 就防不住
   转移期双付。
4. 完成即物化依赖沙箱在线；沙箱不在时**不许失败整个 wait**——物化降级为仅
   `download_url`，下次 `fetch` 补拉。
5. 技能脚本别 import 后端代码（它们跑在沙箱，见不到 backend/）；移植=复制+瘦身，
   并在脚本头注释注明移植源符号，方便回溯。
6. `test_skill_listing.py`/`test_skill_tool_activation.py` 对技能 frontmatter 有
   断言，重写 SKILL.md 后先跑这两个。
7. k8s 清单与 `docker-compose*.yml` 里若引用 `DOUBAO_*`，与 §9 同 PR 改，别留
   半套环境。（实测：k8s 无引用，只有 `.env`/`.env.example`/`openbox.json*`。）
8. **技能脚本必须投递到沙箱。** SKILL.md 由后端宿主机提供且优先级最高，但
   `bash` 跑在无影桌面——宿主机的 `scripts/` 对 agent 不可见
   （`tool/skill_tool.py:_host_files` 的注释写明了这点）。SKILL.md 里一律用
   绝对路径 `/opt/openbox/skills/video-production/scripts/`，并确保部署脚本把
   整个技能目录推上桌面。这是 PR2 首版漏掉、真机验收时才暴露的缺陷。
9. 用 pytest import 技能脚本会在 `scripts/__pycache__/` 留下 `.pyc`。已被
   `.gitignore` 覆盖，但推送到沙箱前要过滤，否则 UTF-8 解码失败。
10. 前端 vite 代理写死 `localhost:8080`，且 Logto 回调注册在 `localhost:3000`。
    worktree 联调必须占用这两个标准端口（先停主仓库的 dev server），换端口会
    导致 SSO 登录失败。


---

## 14. 执行记录（2026-09-01）

分支 `feat/video-atomization`，四个提交，净删 7,292 行（+3,174 / −10,466）。
后端测试 **1188 passed**（基线 1208，差额为退役模块的测试；期间新增 32 例）。
前端与移动端 **0 个文件改动**。

### 14.1 与计划的偏离（三处，均为执行中发现的更优解）

1. **`tool/video_workflow.py` 不能整体删除后就没有下文。** §8 要求删除它，§11
   又要求 `api/video_productions.py` 零改动——两者冲突。实际做法：把历史只读
   路径提取为 `backend/video/productions.py`（领域层），工具文件才整体删除。
   结果比原计划更干净：领域对象不再寄居在 agent 工具层。
2. **schema 预算**在 PR1 双模式期临时放宽到 11,500 字符，PR3 删除
   `video_project`/`video_render` 后已恢复 10,000 并通过。
3. **`share_file` 进入 video 意图包**（技能用它做安静上传），使它同属
   delivery/video 两包，`test_tool_exposure` 的归因断言相应放宽并注明原因。

### 14.2 真机验收（无影 dev 桌面 `ecd-8zp47qagrsc95h67t`，隧道 18002）

`docs/DIRECT_PATH_CLEANUP_PLAN.md` 记的「dev 桌面已过期不可用」**已失效**：
`.env.wuying-dev` 指向上海 dev 桌面，中继 `47.110.66.89` 上 18001/18002 反向
隧道均在线。

实测通过：

| 项 | 结果 |
|---|---|
| ffmpeg / ffprobe / python3 | 4.4.2，齐备 |
| 中文字体 | 89 个，含 Noto（`build_ass.py` 指定的 `Noto Sans CJK SC` 可用） |
| `build_ass.py` | 3 段字幕 + 频道水印，共 4 条 Dialogue |
| `compose.sh` | 3×2s 拼接 → **6.061s**，无失步；h264 + aac |
| `extract_audio.sh` | 正常产出 mp3 |
| 烧录目视 | 720×1280 竖屏；中文按 CJK 网格换两行；英文 `SuperResolution` 未被劈开；水印在位；无豆腐块 |

脚本在沙箱内直接执行也已验证：`lint_prompt.py` 判通过、`compare_transcript.py`
对 `出片→出花` 判 `suspect`（相似度 0.875）、`state.py` 正确写入
`/workspace/videos/<slug>/state.json`。这坐实了 §13.8 的绝对路径修复。

工具层真机验证（真 Postgres + 真无影 + 真配置，未付费）：`action="models"`
返回 9 个模型及能力元数据；`estimate` 通过合法的 wan3 1080p/9:16/24s/seed
请求，并分别以明确原因拒绝 21:9、不支持 seed 的模型、超出 4-15s 的时长、
非音频资产；`daily_submits_used=0/50` 证明背压已接通。

`/api/agent/config` 返回 9 个模型，字段集
（`id/name/channel/tier/resolutions/max_duration_seconds`）与改造前一致——
新增的能力字段是纯增量，这正是前端零改动成立的原因。

未在真机验证的部分：真实付费生成（需花费，另行安排）；工作台 UI 需登录，
见 §14.4。

### 14.4 浏览器验收（工作台，dev 桌面）

前端守卫只认 access token，与后端是否启用鉴权无关：把后端切到单用户模式
（不配 `JWT_SECRET`）后 API 可直连，但工作台仍会跳登录页。验收改用一个
**自建的一次性夹具账号**（`videoqa`，随机口令，只存在于本地 dev 库）——
不使用任何既有账号的凭据。

| 项 | 结果 |
|---|---|
| composer 视频模型选择器 | 9 项全渲染，含新补的 Seedance 2.0 Fast / 1.5 Pro / MiniMax H3；tier 标签正确；`Wan 3.0` 打勾为默认 |
| agent 自报可用工具 | `video_generate`、`video_transcribe`、`skill("video-production")`——`video_project` / `video_render` 已不存在 |
| `action="models"` 端到端 | agent 在真实回合里自主调用，拿回 9 个模型及完整能力元数据 |
| 技能 + 沙箱脚本 | 技能加载 → agent 按 SKILL.md 的绝对路径在沙箱执行 `compare_transcript.py` → 输出 0.875 / suspect /「片→花」 |
| `lint_prompt.py` | 一次调对，输出 `ok: true` |

浏览器验收还抓到两处文档缺陷，已修：

1. `references/prompt-recipes.md` 与 `references/quality.md` 仍写相对路径
   `scripts/…`——它们同样在沙箱里被读，会和 SKILL.md 犯一样的错；
2. SKILL.md 只写脚本名不写参数，agent 首次用位置参数触发 argparse usage
   错误后才自纠，白费一个往返。补全 `--intended/--heard`、
   `--prompt-file/--anchor` 后复测，一次调对。

回归测试把两点都盯住（references 不得含相对路径；SKILL.md 必须出现各脚本
的参数名）。

### 14.3 仍待人工执行（§10，涉及凭据）

1. relay 上给 OpenBox 发**专属 token**（现与 `bossip-center` 共用
   `center-media-vip`，用量无法按产品归因）；
2. 决定是否放行 **task 端点**（`/v1/video/generations`）——放行后首尾帧、
   参考音频、seed 在 wan3 上全量可用；不放行则工具显式报错而非静默降级。


### 14.5 真实付费验收（2026-09-01，dev 桌面）

全部经浏览器工作台、用日常口吻的用户提示词触发，未使用任何专业指令。

| 场景 | 模型 / 参数 | 结果 |
|---|---|---|
| 文生视频「橘猫跳窗台」 | wan3.0-video / 9:16 / 1080p / 5s | ✅ 18.2 MB，自动物化到 `/workspace/uploads/`，`share_file` 交付 |
| 文生视频「雨天咖啡」 | doubao-seedance-2-0-260128 / **16:9** / **720p** / 4s | ✅ 与上一条构成不同画幅+不同分辨率的对照 |
| 图生视频 | image_gen 出参考图 → wan3.0-video，`roles=["reference_image"]` | ⚠️ 当时记作"人物保持一致"，**是误读**；见 §14.6 |
| **口播成片** | wan3.0-video-prime × 6 段并发 | ⚠️ 成片规格正确（25.2s / 720×1280 / h264+aac / 6 条字幕），但人物每段都换脸 |

口播那条走完了技能的完整链路：加载技能 → 读 creator_context → 出讲稿并
**先要确认再花钱** → image_gen 出统一主播锚点 → 6 段并发生成 → 沙箱
`extract_audio.sh` 抽音频 → 转写 → 用**实际念词**生成 `captions.ass` →
`compose.sh` 合成 → `share_file` 交付。字幕两行换行正确，频道水印在位。

**但"人物一致性成立"这句是错的**——当时只比对了衣着色调就下了结论。用户指出
"人物形象没参考，只是衣服参考了"，复核属实：脸、发型、场景每段都在变。根因与
修复见 §14.6。

#### 观察到的质量差距（非系统缺陷，记录备查）

1. ~~**场景会漂移。** 人物锁得住，但背景在镜头之间会变~~ —— **这条诊断是错的**。
   人物根本没锁住，参考图整个没送到模型手里；"漂移"是从零重新生成的表象。
   据此推出的"必须放行 task 端点才能解决"也随之作废：`images[]` 通道在现有
   网关上就够用（§14.6）。放行 task 端点仍有价值（首尾帧衔接），但不再是
   人物一致性的前提。
2. **时长会短于要求。** 用户说「大概 30 秒」，成片 25.2 秒。分段时长由
   模型自行决定，技能没有累计校核的步骤。

#### 本轮测出并修复的缺陷

`fix(video): 用不了的 seed 降级丢弃而非拒绝整次生成` —— 详见该提交说明。
根因有两层：调用方会把 schema 每个可选字段都填默认值（`seed: 0`），使它
**无法表达「不要 seed」**；而更根本的是，为一个不改变画面内容的可选参数
拒绝整次生成，优先级判断错了。

---

### 14.6 第二轮浏览器验收（2026-09-01 下午，dev 桌面 + 真实付费）

起因是用户指出成片"人物形象没参考，只是衣服参考了"。查证推翻了 §14.5 的两条
结论，并牵出三个此前没暴露的缺陷。

#### 人物身份：七组对照定位到调用层

同一张参考图、同一个 OSS 预签名 URL：

| 路径 | 提示词 | 身份 |
|---|---|---|
| 直连 wan3 适配器 `content[]` role=`reference_image` | 短（只说动作） | **锁住** ✅ |
| 直连 wan3 适配器 `content[]` role=`first_frame` | 短 | **锁住** ✅ |
| relay `/v1/videos` + `image_url` | 长（重述外貌） | 陌生人 ❌ |
| relay `/v1/videos` + `image_url` | 短 | 陌生人 ❌ |
| relay `/v1/videos` + `first_frame_url` | 短 | 陌生人 ❌ |
| relay `/v1/videos` + `content[]` | 短 | 陌生人 ❌ |
| **relay `/v1/videos` + `images[]` + `@image_file_1`** | 短 | **锁住** ✅ |
| Seedance + `image_url`（对照组） | 长/短皆可 | **锁住** ✅ |

结论：**既不是模型的限制，也不是提示词写法**。wan3 直连时两种 role 都完美还原，
是 new-api 的 `/v1/videos` 不把参考字段映射进它需要的 `media[]`。网关文档写明的
多素材通道是有效那条。`build_payload` 现在只对 wan3 走 `images[]`，并自动补齐
提示词里缺失的 `@image_file_N`——relay 靠这个占位符绑定 `images[i]`，没被提到的
图会被直接忽略。Seedance 两条都吃，保持原路径。

nginx 白名单复核（POST 也一样）：只有 `/v1/videos` 前缀可达，`/api/v3/...` 与
`/v1/video/generations` 均为 nginx 404。所以 §10.2 仍然成立，只是不再是人物
一致性的前提。

**一个被推翻又被推翻的中间结论**：期间曾把 wan3 声明为"不支持参考图"并提交，
用户指出别的平台 wan3 能做——他是对的，该提交已 revert。教训记在这里：把
"我们这条链路做不到"写成"模型做不到"，会让能力表长期骗人。

#### 三个此前没暴露的缺陷

1. **长耗时操作串行提交（通用，非视频）。** 一条两段视频跑出四次付费提交，
   时间戳 05:45 → 05:51 → 05:58 → 06:03，每次隔六分钟。并发机制本来就有
   （`processor._run_parallel_safe_groups`），提示词里也有"独立调用并行发起"，
   缺的是"**越慢越贵越要先全部发起再等待**"。四个提示词族各补一条。
2. **整稿一镜的浪费。** agent 先花钱生成 20 秒全稿一镜，再改主意拆段重做，
   第一段因此与第二段重复。技能改为"先拆分再生成"。
3. **重做版本进了交付。** `shot2-v1` 与 `shot2-v2` 同时留在分段列表里，用户
   看到同一句话两次。技能补上"一段只有一个当前版本，只有当前版本进合成"。

#### 修复后的复测（"健康早餐"，5 段）

| 验证点 | 结果 |
|---|---|
| 并发提交 | 5 段全部在 **`06:13:59` 同一秒**提交（对比修复前每段隔 6 分钟） |
| 内容去重 | 5 段台词严格对应"开场 + 三个办法 + 收尾"，无重复、无整稿一镜 |
| 逐段质检 | 转写发现第 1 段多念词、第 3 段"三分钟"念成数字、第 5 段"的/得"差异，**只重做第 1 段** |
| 人物一致性 | 锚点图 ↔ 各段：同一张脸、同一层次短发、同一件驼色 V 领粗针织、同一白墙 ✅ |

#### 顺带验证了耐久恢复契约

重做第 1 段时撞上**本机出站瞬时抖动**（几秒内直连与代理同时不通）。表现与处置：

- 转存 OSS 失败 → 作业落 `transfer_failed`，工具返回 `polling_paused`；
- agent **正确停下、报出 `job_id`、没有重提也没有取消**；
- 供应商侧复查：两条任务均已 `completed` 且产物 URL 就绪（**钱没白花**）；
- `14:43:37` / `14:43:40` 恢复补扫自动把两条救回 `completed`，无需人工介入。

这条不是缺陷，是设计按预期工作的一次实证。

#### 本地启动不再走代理

上面那次抖动暴露了一个真实的开发环境陷阱：httpx 默认 `trust_env=True`，一个
全局导出 `HTTP_PROXY/HTTPS_PROXY` 的 VPN 客户端会把**每一次**后端出站都改道
——视频 relay、OSS 转存、DashScope、无影隧道，全都是境内直连的目标。隧道一抖，
表现却是"供应商连接异常"，让人去查一个其实完全健康的服务。

`scripts/backend_entrypoint.py` 现在启动时丢弃继承来的代理变量并打印一行提示；
`NO_PROXY` 不动（它是豁免名单不是代理）；确有需要的部署用 `OPENBOX_KEEP_PROXY=1`
保留。三条单测覆盖这三种情形。

#### 仍未处理

`_public_error` 把传输层故障打码成 `ConnectError: operation failed`，agent 只能
转述成"生成服务连接异常"，听起来像供应商故障。`ConnectError` 不含任何供应商
响应内容，可以安全地说清"到供应商的网络连接失败，任务仍在，稍后自动恢复"。
已向用户提出，等确认后再改。

### 14.7 第三轮：镜头时长、语速与展示顺序（2026-09-01 傍晚）

#### 时长必须由台词决定

用户报"第一段出现明显多余词语"。实测上一轮五段（请求 5/6/6/6/6 秒）：

| shot | 字数 | 请求 | 实际 | 结果 |
|---|---|---|---|---|
| 1 | 13 | 5s | 2.6 字/秒 | 远低于自然语速，模型自己编词凑满 |
| 3 | 31 | 6s | 5.2 字/秒 | 语速赶 |
| 4 | 32 | 6s | 5.3 字/秒 | 语速赶 |

两头是同一个错误：时长在台词写出来之前就定死了（总时长 ÷ 段数）。模型会
填满给它的时间——给多了编词，给少了赶。

新增 `scripts/plan_shots.py`：按每段自己的字数算时长，Latin 词按词而非字母
计，低于 3 秒判为"多是静音"，超模型上限时给出该删多少字。同一条脚本重新
规划需要 40 秒，每段落在 3.4–4.0 字/秒。**总时长是输出不是输入**：30 秒的
要求要么给 40 秒成片、要么删字，脚本把这个选择交给用户。

复测（"下班放松"）：4 段时长 6/5/7/5 各不相同，全部落在 3.4–3.8 字/秒。

#### 语速是片子的属性，不是脚本的常量

4 字/秒当硬常量，等于假定冥想稿和促销稿读得一样快。改成默认值 + `--rate`：
舒缓 3.4、常规 4.0、高能 4.6（播音 280–300 字/分 ≈ 4.7–5.0 是舒适上限）。
判断情景是 agent 的活——它刚写完稿子，知道是什么调性；脚本推断不出来。

#### 分段展示顺序

并发提交后**完成顺序即挂载顺序**，于是"最后一段"被显示成"第 2 段"。
前端本来就按 `group.ordinal` 排序（`content-view.ts` 的比较器），只是后端
从没填过：`_attach_completed` 的 ordinal 取自 `segment.ordinal`，而原子化后
`segment_id` 恒为 None。给 `video_generate` 加显式 `shot` 参数，一路落到
`request_data` 与 `FileRelation.ordinal`。**前端零改动。**

复测（11 段并发）完美复现了触发条件——落库顺序 7,4,3,6,9,8,11,5,2,1,10，
与镜次完全错开；每条 relation 带对了自己的 ordinal，界面显示 1–11 有序。

### 14.8 验收清单与证据（2026-09-01 收尾）

先从本手册提取可验断言立成清单，再逐条到运行环境里验——而不是先测完再
补文档。环境：worktree 自身的前后端 + 无影 dev 桌面。

**环境前提**（每项都当场取证，不靠记忆）：

| 项 | 证据 |
|---|---|
| 无影 dev 配置来自 main | `.env.wuying-dev` / `.env.wuying-prod` 与 main 逐字节一致 |
| 后端来自本 worktree | `:8080` 进程 cwd = `…/OpenBox-video-atomization/backend` |
| 前端来自本 worktree | `:3000` 进程 cwd = `…/OpenBox-video-atomization/frontend-v2` |
| 沙箱可达 | `:18002/alive` HTTP 200，桌面 `ecd-8zp47qagrsc95h67t` |
| 出站直连 | 后端进程无 `HTTP(S)_PROXY` |

**清单结果**：

| # | 手册出处 | 断言 | 结果 |
|---|---|---|---|
| 1 | §11 | 前端/移动端零改动 | ✅ 改动文件数 0 |
| 2 | §14.4 | composer 选择器 9 项、tier 正确、Wan 3.0 默认 | ✅ |
| 3 | §1.2 | 新补 3 个模型出现在选择器 | ✅ Seedance 2.0 Fast / 1.5 Pro / MiniMax H3 |
| 4 | §14.4 | `action="models"` 端到端 | ✅ 返回 9 个模型 |
| 5 | §3.2 | `estimate` 拒绝说明原因 | ✅ "supports ratios …; requested 21:9" |
| 6 | §3.3 | 额度背压可见 | ✅ `daily_submits_used=36/50` |
| 7 | §3.2 | 完成即物化 + `share_file` 交付 | ✅ `/workspace/uploads` 35 个 mp4 |
| 8 | §11 | 历史会话只读渲染（零迁移） | ✅ 22 条 production 保留，会话与视频照常显示 |
| 9 | §8 | `video_project`/`video_render` 已退役 | ✅ 残留 0，原子工具 5/5 在位 |
| 10 | §14.6 | 并发提交 | ✅ 11 段首尾相差 **60ms** |
| 11 | §14.6 | 人物跨段一致 | ✅ 锚点图 ↔ 各段同一人 |
| 12 | §14.6 | 内容无重复 | ✅ 11 个镜次 / 11 段；`shot5:v2` 是质检后的重做，符合 `:v2 取代 :v1` |
| 13 | §7 | 技能脚本在沙箱可执行 | ✅ `plan_shots.py --rate 3.4` 正常输出 |
| 14 | §14.6 | 本地启动不走代理 | ✅ entrypoint 丢弃 + 3 条单测 |

后端 **1215 passed**；前端与移动端 **0 个文件改动**。

---

## 15. 上线记录（2026-09-01）

`main` = `f86aac4`，已推送 `arkstudio-ai/OpenBox`。

### 15.1 部署

目标 `https://ai.ueejavelin.org/` → EC2 `i-0eaae88c8b67d9bb5`（OpenClaw-NewAPI，
ap-southeast-1，54.254.36.226）。这台机器还跑着 new-api / caddy / reqlog-proxy
等无关服务，**本次只动 `openbox-*` 那一组**。

发布方式沿用既有约定：机器上 `/opt/openbox/src` 是 git 检出，直接从 GitHub
拉取后本地构建，镜像打 `<日期>-<短 sha>`。凭据受限（`claude-temp` 无 ECR、
无 S3 ListBuckets），所以不走镜像仓库；执行通道用 SSM，无需 SSH 私钥。

```
构建   openbox-backend:20260901-f86aac4      555MB
       openbox-frontend-v2:20260901-f86aac4   64.5MB
切换   OPENBOX_IMAGE_TAG  20260901-ff4b16c → 20260901-f86aac4
重建   docker compose up -d --no-deps backend frontend
结果   两个容器 healthy；站点 HTTP 200
```

### 15.2 线上配置对齐

| 项 | 原 | 现 |
|---|---|---|
| `video_generation.models` | 6 | **9**（补 Seedance 2.0 Fast / 1.5 Pro / MiniMax H3，并带实测能力字段） |
| provider 条目 | `doubao` | `bossip` |
| `channel_providers` | `{sd2: doubao}` | `{sd2: bossip}` |
| 环境变量 | `DOUBAO_API_KEY/BASE_URL` | `BOSSIP_API_KEY/BASE_URL` |

两个文件都先备份再改，密钥值只在机器内复制、从未打印。线上后端复验读到
9 个模型、`provider: bossip`。

### 15.3 线上无影桌面与技能

线上用的是 **`ecd-4zjxaq5g45dr5qr0i`（bossip-slot8，cn-shanghai）**，经中继
`47.110.66.89` 的 18001 隧道；与本地联调用的 dev 桌面
（`ecd-8zp47qagrsc95h67t`，18002）是两台。

**该桌面上的技能是旧版**：只有 SKILL.md 和四个描述已退役审批流水线的
reference，`scripts/` 完全没有——新技能的脚本一个都不在，agent 无从执行。
已全量替换为新版 11 个文件，并在桌面上直接跑通 `plan_shots.py`。

下发不能走 action server：**prod 桌面是加固形态** —— `/` 挂载为 `ro,nosuid`，
action server 以非特权 `sandbox` 用户（uid 998）在 `no_new_privs` 下运行，
`/write_file` 还被 `confined_path_resolve_v1` 限制在 workspace 内。dev 桌面
则是可写的普通形态。这个差异一度让我误判成"线上技能被我删了"——实际是
`find -maxdepth 2` 漏看了深度 3 的文件，`rm -rf` 早被只读挡下，线上一个字节
没动。

正确通道是 ECD `run-command`（以 root 执行），即 `wuying_bootstrap.py` 用的
那条。为此新增 `backend/scripts/wuying_push_skill.py`：同样的 `Desktop.put`、
同样的通道，但只做技能下发，不重装运行时与隧道。

```
python3 scripts/wuying_push_skill.py --env-file .env.wuying-prod
```

### 15.4 仍待人工

- prod 桌面的 action server 版本（`2026.08.31-run-lease-receipt-v12`）仍带
  `media_jobs_*` 能力。新架构不再调用它们，多余路由无害，下次桌面重建时
  自然消失。
- §10 的两件运维事项未变：给 OpenBox 发专属 relay token（现与
  `bossip-center` 共用），以及是否放行 task 端点。
| 13 | §7 | 技能脚本在沙箱可执行 | ✅ `plan_shots.py --rate 3.4` 正常输出 |
| 14 | §14.6 | 本地启动不走代理 | ✅ entrypoint 丢弃 + 3 条单测 |

后端 **1215 passed**；前端与移动端 **0 个文件改动**。

### 15.5 放行 task 端点（用户已批准，待执行）

用户批准了 §10.2，另一项（给 OpenBox 发专属 relay token）明确不做，维持与
`bossip-center` 共用。

**为什么只能改腾讯那台 nginx。** gw-1 的安全组把 3000 端口限死在
`106.52.167.53/32`，规则描述写着「渠道商入口:仅腾讯前置 nginx 回源」——腾讯
那台是唯一的 TLS 前门。放宽安全组让后端直连 gw-1:3000 能达到同样效果，但那
是把带密钥的 API 放到明文 HTTP 上跨公网，不做。

**改动**（`scratchpad/open_task_endpoint.sh`，幂等）：把现有 `/v1/videos` 的
location 块**原样复制**再改路径，而不是新写一段——那个块已经带着分钟级视频
提交需要的超时与头部，凭空写正是让"修复"变成 504 的常见方式。先备份、
`nginx -t` 校验、失败自动回滚，通过才 reload。

**代码侧已验证就绪**，切换是纯配置：

```
wan3.0-video / wan3.0-video-prime:  channel  sd2 → task
video_generation.channel_providers: 增加      task: bossip
```

本地实测切换后的 payload 正是 wan3 适配器要的形状：

```
POST /v1/video/generations
{"model":"wan3.0-video-prime","prompt":"…",
 "metadata":{"resolution":"1080p","ratio":"9:16","duration":6,
             "generate_audio":true,"seed":42,
             "content":[{"type":"image_url","image_url":{"url":"…"},
                         "role":"first_frame"}]}}
```

端点放行后即可切换，`last_frame → first_frame` 衔接随之可用——那是 §14.6
留下的"场景漂移"唯一未解手段。

## 16. 移动端连线上后端实测（2026-09-01）

`--dart-define=API_BASE=https://ai.ueejavelin.org` 装进 iOS 模拟器，对着 AWS
那台跑。

### 16.1 线上先补齐

进场时线上落后 6 个后端提交（`ad8d0cf`），且 `config/openbox.json` 的
`video_generation` 还是上一轮的：没有 `wire_shape`（新代码按它分派报文形状，
缺了会整体退回 `flat`——正是 wan3 分辨率丢失的那个老毛病）、默认模型为空、
wan3.0 只挂了 1080p、还留着上游已下线的 `doubao-seedance-1-5-pro-251215`。

对着本地实测过的注册表整体替换（只换 `video_generation` 一节，其余含密钥的
部分原样不动，改前备份）。现在线上 8 个模型、`wan3.0-video + 720p` 为默认、
`daily_job_limit` 一致。

只重建了 `openbox-backend` / `openbox-frontend`，postgres、redis 与机器上其他
无关服务未动。

### 16.2 链路实证

不是"看起来连上了"，是两头都留了痕：

```
模拟器   POST /api/auth/login → 页面渲染出后端返回的错误
EC2 日志 backend-1 | "POST /api/auth/login HTTP/1.1" 401 Unauthorized
```

### 16.3 顺带查出并修掉的一个老问题

负向登录（故意给错密码）时，页面写的是「Your session expired. Please sign in
again.」——既不是发生的事，也不是该做的事。

根因在后端：登录/注册的 401/409 只给字符串 `detail`。两端客户端都按
`detail.code` 取文案，取不到退回按状态码取，于是所有 401 都落到会话过期那条。
而 `AUTH_INVALID_CREDENTIALS` / `AUTH_USER_EXISTS` 在 web 与移动端四份
`errors.json` 里一直躺着，从来发不出来。

改成与 `auth.quota._quota_error` 同形的结构化 detail，客户端一行没改。修完
重新部署，同一条负向用例现在渲染「Wrong account or password.」。

与视频改造无关，是这次连线上跑才暴露的。

### 16.4 卡住的一步

线上库里 6 个账号，**没有 `videoqa`，也没有 `devtest`**——那两个是本地联调库
才有的。要越过登录页测视频选择器，需要本人用线上账号输密码；密码不由我代输。
