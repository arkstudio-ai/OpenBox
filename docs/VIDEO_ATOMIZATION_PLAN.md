# 视频能力原子化——执行手册

> 文档状态：Execution Handbook v1（2026-08-31）<br>
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

`VideoModelConfig` 注释里"relay 静默吞参数并按默认值计费"对 wan3 路径**已过时**
（适配器正是为修它而写）；对 sd2 720p 丢 `extra_videos` 仍然为真（代码已有防御）。

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
   半套环境。
