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

## 3. 目标形态

| 档位 | tier | 模型参数 | 渠道 | 分辨率 |
| --- | --- | --- | --- | --- |
| 极致（新增） | `ultra` | `doubao-seedance-2-5-260628` | ark，TokenSpace（tokenhub） | 480p / 720p / 1080p，默认 1080p |
| 质量 | `high` | `doubao-seedance-2-0-260128`（原 `video-sd-1080p-pro`） | ark，TokenSpace（tokenhub） | 480p / 720p / 1080p，默认 1080p |
| 标准 / 灵活 / 快速 | 不变 | | | |

### 3.1 代码（本分支 `feat/seedance-25-tier`）

- `backend/core/config.py`：`VideoTierConfig.tier` 允许 `"ultra"`（排在 `high` 之上；chat 仍只有三档）。
- `backend/tool/video_providers.py`、`backend/tool/video_production.py`：Seedance 2.5 允许 `duration=-1`
  （官方文档：默认 -1，模型在 4–30 秒内自选；显式值 4–30）。
- `backend/billing/rates.json`：新增 `doubao-seedance-2-5-260628` 每秒价（火山刊例折算，见 §5）。
- 前端 `VideoTier` 类型加 `"ultra"`；Web / App 文案加 `tier.video.ultra`（极致 / Ultra）作为无 label 时的回落。
- 单测：`tests/unit/test_model_tiers.py`（ultra 档排序与价格）、`tests/unit/test_video_production.py`（2.5 时长）。

### 3.2 new-api（bossip-gw-1，`docker exec bossip-pg psql -U newapi -d newapi` / 管理 API）

拿到新 key 后，推荐**新建**一条渠道而不是改 120（保留旧链路可回退）：

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

### 3.3 gw2 `config/openbox.json`（新后端镜像上线后再改，`ultra` 字面量旧后端不认）

`video_generation.models` 追加：

```json
{"id":"doubao-seedance-2-5-260628","name":"Seedance 2.5","channel":"ark","wire_shape":"metadata",
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

## 6. 待办

1. **向 TokenSpace 要 tokenhub.moligroup.com 的新 key**（阻塞 §3.2）。
2. 合并本分支 → 构建 backend / frontend → 发 gw2 → 写 §3.3 配置 → 用 4 s 最小任务各验一条 2.0 / 2.5。
3. App 端随下次发版带上 `tier.video.ultra` 文案（配置里给了 label，老 App 也能正常显示）。
