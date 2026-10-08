# Seedance 2.5 接入与 sd2 事故处置（2026-10-08）

## 1. 背景：sd2 为什么用不了

- 2026-09-29 11:36Z 起，TokenSpace 旧地址 `https://api.tokenspace.net.cn` 对 `doubao-seedance-2-0-260128`
  一律返回 `{"code":"InvalidEndpointOrModel.NotFound","message":"model doubao-seedance-2.0 not found"}`，
  `/v1/models` 只剩这一条，fast / 2.5 名字都是 "No available channel"。
- 受影响链路：gw2 → 自有 new-api `10.100.1.76:3000` → 渠道 113（`video-sd-*` flat 形状）与 120（ark 名字）
  → bossip-gw-1 上 `bossip-tokenspace-sora-adapter` → TokenSpace。两条渠道共用同一把 key。
- gw2 质量档 `video-sd-1080p-pro` 09-29 14:18Z 起 17 连败（上游 400 被归类为 `video_provider_request_rejected`，
  不重试、不扣积分、new-api 预扣已返还）。wan3（渠道 123）与 MiniMax（渠道 114）不受影响。
- 旧适配器还要靠 TokenSpace 的素材接口（`/api/material?Action=CreateAsset`）把图片 URL 换成 `asset://`；
  10-03 / 10-08 另有几次 `moma_official 400: Failed to download media from the provided URL`，同样在上游侧。

## 2. 新地址：`https://tokenhub.moligroup.com`

TokenSpace 方给的说明：**与火山方舟官方 API 完全一致，只把地址换成 `https://tokenhub.moligroup.com`。**

实探（2026-10-08，用旧 key）：
- 这是一台 new-api（错误体 `"type":"new_api_error"`），TLS 正常，`118.145.146.62`。
- 旧 key 在新地址 **401 Invalid token** —— 新地址需要新 key（待 TokenSpace 提供）。
- `/api/material` 在新地址 **404** —— 没有素材接口，图片 / 视频 / 音频必须像火山官方一样直接传公网 URL。
  所以新地址**不能再经过 `bossip-tokenspace-sora-adapter`**（它会先调素材接口再转发），要让 new-api 渠道直连。

## 2.1 实测结论（2026-10-08，用新 key）

- `/v1/models` 含 `doubao-seedance-2-0-260128`、`-2-0-fast-260128`、`-2-5-260628`；三者空 prompt 探测都打到火山的
  `MissingParameter`，说明模型池已开。
- **tokenhub 没有火山的 `GET /api/v3/contents/generations/tasks/{id}`（404）**，只有 `GET /v1/videos/{id}`
  （new-api 统一格式，`status` 小写、结果在 `metadata.url`）和 `GET /v1/video/generations/{id}`（`{code,data}` 信封）。
  因此自有 new-api 上按火山协议（type 54）建的渠道 128 提交成功但轮询永远停在 30%；改成 type 55 后，tokenhub 在任务刚建好的
  头几秒回 `status: "unknown"`，我们 new-api 的 type 55 直接判「upstream returned unrecognized message」失败。
  两次测试上游都正常出片（2 分 22 秒 / 2 分 29 秒），问题只在状态查询的协议差异。
- **最终路线：openbox 后端直连 tokenhub，不经自有 new-api。** 后端的 `sd2` 通道本来就说 new-api 的 `/v1/videos` 协议
  （POST `/v1/videos` metadata 形状、GET `/v1/videos/{id}` 读 `metadata.url`、未知状态当 in_progress），与 wan3 / MiniMax
  现在走自有 new-api 的方式完全一样，只是 base_url 换成 tokenhub。容器内实测：2.0 480p 4s 121 秒完成、2.5 480p 4s 136 秒完成。
- 代价：这三个模型的调用暂不经过自有 new-api（无网关侧日志/预扣）；等 TokenSpace 补上火山的 GET-by-id 再切回渠道 128（type 54）。
  渠道 128 保留（type 55，priority 30），渠道 120 已手动停用（status 2）。

## 3. 目标形态

| 档位 | tier | 模型参数 | 渠道 | 分辨率 |
| --- | --- | --- | --- | --- |
| 极致（新增） | `ultra` | `doubao-seedance-2-5-260628` | `sd2` 通道 + `wire_shape: metadata`，provider `tokenspace`（tokenhub 直连） | 480p / 720p / 1080p，默认 1080p |
| 质量 | `high` | `doubao-seedance-2-0-260128`（原 `video-sd-1080p-pro`） | 同上 | 480p / 720p / 1080p，默认 1080p |
| 标准 / 灵活 / 快速 | 不变 | | | |

### 3.1 代码（本分支 `feat/seedance-25-tier`）

- `backend/core/config.py`：`VideoTierConfig.tier` 允许 `"ultra"`（排在 `high` 之上；chat 仍只有三档）。
- `backend/tool/video_providers.py`、`backend/tool/video_production.py`：Seedance 2.5 允许 `duration=-1`
  （官方文档：默认 -1，模型在 4–30 秒内自选；显式值 4–30）。
- `backend/billing/rates.json`：新增 `doubao-seedance-2-5-260628` 每秒价（火山刊例折算，见 §5）。
- 前端 `VideoTier` 类型加 `"ultra"`；Web / App 文案加 `tier.video.ultra`（极致 / Ultra）作为无 label 时的回落。
- 单测：`tests/unit/test_model_tiers.py`（ultra 档排序与价格）、`tests/unit/test_video_production.py`（2.5 时长）。

### 3.2 new-api（bossip-gw-1）—— 已做，但因 §2.1 的协议差异暂不在链路上

2026-10-08 已按下面的 JSON 新建渠道 128（之后改成 type 55），并把 `ModelPrice` 补了 `doubao-seedance-2-5-260628: 0.5`，渠道 120 已停用：

```json
{"mode":"single","channel":{
  "name":"seedance-tokenhub","type":54,"status":1,
  "base_url":"https://tokenhub.moligroup.com","key":"<新 key>",
  "models":"doubao-seedance-2-0-260128,doubao-seedance-2-0-fast-260128,doubao-seedance-2-5-260628",
  "group":"default,vip,openbox","priority":30,"weight":0,
  "test_model":"doubao-seedance-2-0-260128",
  "remark":"TokenSpace new endpoint (tokenhub.moligroup.com), ark-compatible, no material API; 2026-10-08"}}
```

- type 54 = 火山方舟视频（`/api/v3/contents/generations/tasks`，结果在 `metadata.url`，PR #32 已适配）。
- priority 30 > 渠道 120 的 20，openbox 组会优先命中；之后把 120 置 `status=2`（手动停用）。
- `options.ModelPrice` 补 `"doubao-seedance-2-5-260628": 0.5`（与其他视频模型一样的占位按次价，只影响 new-api 侧预扣）。
- 验证不花钱的探测：空 prompt POST，期望火山式 `InvalidParameter … text content must contain a prompt description`，
  而不是 `model not found` / `Invalid token`。
- 渠道 113（`video-sd-*`）保持现状即可：质量档已不再引用它；要彻底下线时把 `channel_providers.sd2` 也一起清理。

### 3.3 gw2 `config/openbox.json`（已随 `20261008-sd25-69d3834` 写入；脚本 `releases/20261008-sd25-69d3834/deploy_gw2_v6.sh`）

`provider` 追加 `"tokenspace": {"api_key": "{env:TOKENSPACE_API_KEY}", "base_url": "https://tokenhub.moligroup.com", "options": {"wire_format": "bossip_videos"}}`，
`config/backend.env` 追加 `TOKENSPACE_API_KEY=…`；两条 Seedance 2.0 条目改为 `"channel": "sd2", "wire_shape": "metadata", "provider": "tokenspace"`；
`video_generation.models` 追加：

```json
{"id":"doubao-seedance-2-5-260628","name":"Seedance 2.5","channel":"sd2","wire_shape":"metadata","provider":"tokenspace",
 "resolutions":["480p","720p","1080p"],
 "ratios":["21:9","16:9","4:3","1:1","3:4","9:16","adaptive"],
 "duration_range":[4,30],"max_duration_seconds":30,
 "supports_smart_duration":true,"supports_first_last_frame":true,
 "supports_reference_image":true,"supports_reference_video":true,"supports_reference_audio":true,
 "supports_seed":true,"tier":"极致"}
```

`model_tiers.video` 改为：

```json
[{"tier":"ultra","model":"doubao-seedance-2-5-260628","label":"极致",
  "description":"Seedance 2.5：30 秒长叙事、全模态参考、1080p 10bit",
  "resolutions":["480p","720p","1080p"],"resolution":"1080p"},
 {"tier":"high","model":"doubao-seedance-2-0-260128","label":"质量","description":"画质优先，适合成片",
  "resolutions":["480p","720p","1080p"],"resolution":"1080p"},
 … 标准 / 灵活 / 快速 三条原样 …]
```

改法照旧：先定位 `"video_generation"` 再找 `models`（别匹配到 LLM 的 `models`），备份后
`docker compose up -d --no-deps backend`（配置 ro 挂载，`restart` 不够时用 `up -d`），启动日志看 `Loaded config`。

## 4. Seedance 2.5 要点（官方文档 2026-09-30 版）

- Model ID `doubao-seedance-2-5-260628`；接口与 2.0 相同：`POST /api/v3/contents/generations/tasks`。
- 时长 4–30 秒或 -1；分辨率 480p / 720p / 1080p（1080p 为 10bit H.265，部分播放器不兼容）；
  宽高比 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16 / adaptive；输出 mp4 或 `output_format:"mov"`。
- 参考素材上限 50 个（30 图 + 10 视频 + 10 音频，音/视频各总时长 ≤ 30 s）；**纯音频参考**可单独使用（2.0 不行）。
- 任务类型由素材与提示词判定：首帧 / 首尾帧（`role: first_frame/last_frame`，ratio 必须 adaptive）；
  视频编辑（ratio adaptive 且 duration -1，参考视频 4–30 s）；视频延长（ratio adaptive）。
  可传 `omni_reference_task_type: auto|reference|edit|extend` 让错误在提交时同步返回。
- 新增 `draft:true` 样片模式（只能 480p；再用 `draft_task.id` 出 1080p 成片，7 天内有效）。
- 不支持含真人人脸的参考图 / 视频（与 2.0 同一套便利创作方案）。
- 2.5 响应时间戳（"0–3 秒…"），2.0 只响应镜头序号；美学风格与 2.0 差异大，混用需用延长任务衔接。
- 限流：企业 RPM 600 / 并发 10，个人 RPM 180 / 并发 3。视频 URL 24 小时过期、100 次下载上限。

## 5. 价格（火山刊例，2026-10-08 查 docs.volcengine.com/docs/ark/model-pricing）

| 模型 | 480p/720p 无视频输入 | 1080p 无视频输入 | 含视频输入 |
| --- | --- | --- | --- |
| doubao-seedance-2.5 | ¥70 / 百万 token | ¥77 | ¥42（480p/720p）/ ¥46（1080p） |
| doubao-seedance-2.0 | ¥46 | ¥51 | ¥28 / ¥31 |

token ≈ (输入视频时长 + 输出时长) × 宽 × 高 × 帧率 / 1024。官方 16:9 每秒价：
2.5 → 480p 0.67、720p 1.51、1080p 3.74；2.0 → 480p 0.46、720p 0.99、1080p 2.48。
`rates.json` 的 2.5 条目按此写入；2.0 条目仍是 09-02 的旧折算（1080p 2.25），未在本次改动。
TokenSpace 是分销，实际成本以其账单为准；售价由运营在 `rates.json` 调。

## 6. 状态与待办

已完成（2026-10-08）：PR #62 合并；gw2 发布 `20261008-sd25-69d3834`（backend / frontend / worker），配置与 key 按 §3.3 写入；
容器内实测 2.0 与 2.5 各一条 4 s 480p 成片；Autopilot 高档改为 `doubao-seedance-2-0-260128`（PR #63，backend-only 发布
`20261008-autopilot-f4028667`）。详见 DEPLOY.md 当天条目。

待办：
1. 请 TokenSpace 补 `GET /api/v3/contents/generations/tasks/{id}`；补上后可把三条 Seedance 改回经自有 new-api 渠道 128（type 54），
   恢复网关侧预扣与日志。
2. `video-sd-1080p-pro` 条目与 new-api 渠道 113 仍在，无人引用；下次清理配置时一并下架。
3. App 端随下次发版带上 `tier.video.ultra` 文案（配置里给了 label，老 App 也能正常显示）。
4. AWS 开发环境未发。
