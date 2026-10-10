# RunningHub 图片通道

`image_gen` 新增 RunningHub 图片模型。生产上 OpenAI 兼容的 gpt-image-2 中转大量失败，本通道可替代它。原有 `openai` 路径保留：配置成其它模型 id（如 `gpt-image-2`）时，仍走 OpenAI Images API。

## 关键结论（2026-10-10 实测）

- 当前 RunningHub 凭据属于 `runninghub.cn`。在该站点上，Nano Banana Pro、Nano Banana 2、GPT Image 2 均返回业务码 **40310**：“因合规要求，该模型在当前CN节点已下线，请前往全球站 (runninghub.ai) 接入使用”。被拒绝的请求不会创建任务，也不会扣费。工具会把它报告为“模型不可用”，并提示可改用 Seedream v5 Pro。
- **Seedream v5 Pro 在 runninghub.cn 可用**：文生图和图生图各做了一次付费实测，均成功（见验证记录）。
- 因此配置示例的默认模型是 `seedream-v5-pro`，而不是原计划的 `rhart-image-n-pro`。若要使用 Nano Banana Pro，需要另配一个 runninghub.ai 凭据（见“部署配置”）。全球站的凭据与接口本次**未验证**，没有用 CN 站的 key 去请求 .ai 站。

## 模型注册表

注册表在 `backend/tool/image_runninghub.py` 的 `MODELS`，参数取自 RunningHub 官方能力表 HM-RunningHub/OpenClaw_RH_Skills（capabilities.json，2026-08-18）。`image_generation.model` 只要是下表中的 id，就走 RunningHub 任务接口，并使用 `image_generation.provider` 指定的凭据。

| id | 名称 | 文生图 | 图生图 | 尺寸参数 | 输入图 | runninghub.cn |
| --- | --- | --- | --- | --- | --- | --- |
| `seedream-v5-pro` | Seedream v5 Pro | `seedream-v5-pro/text-to-image` | `seedream-v5-pro/image-to-image` | `width`/`height` 240–8192，`outputFormat` jpeg/png | ≤10 张，每张 ≤10 MB | 可用 |
| `rhart-image-n-pro` | Nano Banana Pro | `rhart-image-n-pro/text-to-image` | `rhart-image-n-pro/edit` | `aspectRatio` + `resolution` 1k/2k/4k | ≤10 张，每张 ≤10 MB | 40310 |
| `rhart-image-n-g31-flash` | Nano Banana 2 | `rhart-image-n-g31-flash/text-to-image` | `rhart-image-n-g31-flash/image-to-image` | 同上，另加 1:4/4:1/1:8/8:1 | ≤10 张，每张 ≤30 MB | 40310 |
| `rhart-image-g-2` | GPT Image 2 | `rhart-image-g-2/text-to-image` | `rhart-image-g-2/image-to-image` | `aspectRatio`（含 empty、9:21、1:2、2:1、1:3、3:1）+ 可选 `resolution` | ≤10 张，每张 ≤30 MB | 40310 |

提示词上限：Seedream 5000 字符，其余 20000 字符。

## 路由与参数映射

- 调用带 `input_images` 时走图生图/edit 端点，否则走文生图端点。
- RunningHub 每个任务只出一张图，所以 `n>1` 会在付费前被拒绝，需要多张时请多次调用。蒙版（`mask_image`）、透明背景、超过张数或大小上限的输入图、超长提示词，也都在付费前拒绝，并说明原因。
- `size=WIDTHxHEIGHT`：
  - 按比例取值的模型：取最接近的受支持比例，误差超过 8% 就拒绝，并列出受支持的比例；分辨率档位取能覆盖长边的最小一档（长边 ×0.9 ≤ 1024/2048/4096）。结果中会注明换算，例如 `1920x1080 → 16:9 @ 2k`。
  - Seedream：直接使用该像素尺寸。
- `size=auto`：
  - 文生图：GPT Image 2 传 `empty`，由模型决定比例；Nano Banana 用 1:1；分辨率用 2k（与 1k 同价）。Seedream 用 2048×2048。
  - 图生图：比例模型传 `empty`，保留原图比例；Seedream 按原图比例换算到 2k 长边（从 PNG/JPEG/WebP 文件头读取尺寸）。
- 输入图沿用 `image_gen` 原有的归属校验：只能用本人已就绪的 FileAsset。RunningHub 拿到的是 1 小时有效的 OSS 签名 URL，不会生成公开副本。轨迹记录里签名 URL 会替换为资产引用。
- gpt-image-2 的 16px 网格对齐只作用于 `gpt-image-2`，不影响以上模型。

## 任务可靠性

1. **只提交一次**。复用视频通道的 HTTP 客户端（`video_providers.submit/status`）和 RunningHub 错误处理。HTTP 200 的业务拒绝也走同一套判断，例如 605 余额不足、40310 模型下线。
2. 拿到 `taskId` 后**立即**写入 effect ledger，作为持久回执（`provider_handle`，状态 accepted）。之后任何情况都不会因为“没有回执”而无法处理。
3. 查询 `POST /openapi/v2/query`：首次间隔 2 秒，每次 ×1.5，最长 10 秒；总时长不超过 `image_generation.timeout_seconds`（默认 600 秒）。查询连续失败 5 次就停止轮询，交由后续恢复处理。
4. 查询结果的处理：
   - SUCCESS 且有图片 URL：下载（≤50 MB），写入预先保留的 FileAsset，挂到聊天并按档位结算。
   - FAILED/CANCELLED：任务失败。说明原因，原因只用我方文字和错误码；内容审核类会单独提示。不扣积分，修正后可以重试。
   - 到时仍未完成，或成功了但图片暂时下载不下来：返回“仍在生成，完成后会自动出现在资源中心，请勿重复提交”。effect 中保留 `taskId`，预留资产保持 generating。恢复服务（`recover_external_effects_once`，运行结束后才接管）按 `taskId` 查询：成功则把图片写入原预留资产，并按 effect 只结算一次（键为 `image:effect:<id>`）；失败则标记失败；仍在运行则约 60 秒后再查。整个过程从不重新提交。
5. 明确未受理的情况：HTTP 4xx（408/409/499 除外）、业务拒绝码、连接根本没有建立。这些返回“未受理，不扣费”和清晰原因（参数不支持 / 权限或额度 / 频率限制 / 模型不可用）。effect 记为 failed，不需要人工复核。
6. 结果不确定的情况：提交超时、5xx、返回中没有 `taskId`。这时可能已经产生付费任务，所以禁止自动重新提交，进入人工核查。

## 计费

价格来自 2026-10-10 官方价格预估 API：`POST /openapi/v2/price-preview/<端点>`，请求体与正式任务相同，可免费调用。每个模型都按档位查询了文生图和图生图两种操作，两者价格相同；不同比例（1:1、16:9）价格也相同。沿用 `billing/rates.json` 中 1 CNY = 1 积分的成本定价，没有加利润系数，版本号为 `2026-10-10.1`。

| 模型 | 1k | 2k | 4k |
| --- | ---: | ---: | ---: |
| Seedream v5 Pro | 0.27（≤1024×1024） | 0.54（更大尺寸，含 4096×4096） | — |
| Nano Banana Pro | 0.40 | 0.40 | 0.50 |
| Nano Banana 2 | 0.19 | 0.19 | 0.30 |
| GPT Image 2 | 0.10 | 0.10 | 0.10 |

`per_image` 可以是按档位的映射。`settle_image(..., tier=...)` 用本次请求的档位报价；表里没有的档位视为未定价，不会套用默认价。只在图片真正交付后结算，拒绝和失败都不扣积分。RunningHub 会单独向渠道账户收取实际生成费用：两次实测的查询结果中 `usage.thirdPartyConsumeMoney` 均为 0.27，与预估一致。

## 部署配置

不要编辑被 git 忽略的 `backend/openbox.json` 去提交。生产上只合并以下字段，其它配置保持不变。`RUNNINGHUB_API_KEY` 沿用视频 Turbo 已在使用的密钥。

```jsonc
"provider": {
  // 已存在则保留，不要重复添加
  "runninghub": { "api_key": "{env:RUNNINGHUB_API_KEY}", "base_url": "https://www.runninghub.cn" }
},
"image_generation": {
  "provider": "runninghub",
  "model": "seedream-v5-pro",
  "default_size": "auto",
  "default_quality": "medium",
  "output_format": "png",
  "timeout_seconds": 600,
  "dedupe": true
}
```

以下全球站配置未验证。如果要使用 Nano Banana Pro，新增一个指向全球站的凭据，然后切换：

```jsonc
"provider": { "runninghub_ai": { "api_key": "{env:RUNNINGHUB_AI_API_KEY}", "base_url": "https://www.runninghub.ai" } },
"image_generation": { "provider": "runninghub_ai", "model": "rhart-image-n-pro" }
```

回退方式：把 `image_generation` 改回 `"provider": "openai", "model": "gpt-image-2"`。不要删除 `runninghub` 凭据，因为仍在运行的任务要靠原账号路由恢复（恢复时会核对路由指纹）。前端不需要发布。

## 验证记录

- 价格预估：4 个模型 × 1k/2k/4k × 文生图/图生图，以及 Seedream 1024²/2048²/4096²，全部返回 HTTP 200，价格见上表。
- 直接调用本适配器（`_runninghub_generate`，脚本运行，不经服务端，不写仓库）：
  - `rhart-image-n-g31-flash` 文生图 1k 1:1、`rhart-image-g-2` 文生图 1k 1:1：均返回 40310，未创建任务，未扣费。
  - Seedream v5 Pro 文生图 1024×1024：taskId `2108835570985078786`，提交、轮询、下载共 38.2 秒，输出 1024×1024 PNG（1,780,455 字节），`usage.thirdPartyConsumeMoney` 0.27。
  - Seedream v5 Pro 图生图（以上一张的结果 URL 作为输入）1024×1024：taskId `2108835853152690177`，101.4 秒，输出 1024×1024 PNG（2,188,231 字节），费用 0.27。
  - 查询返回的字段有：`taskId`、`status`、`errorCode`、`errorMessage`、`failedReason`（对象）、`usage`（`consumeMoney`、`consumeCoins`、`taskCostTime`、`thirdPartyConsumeMoney`）、`taskUsageList`、`results[]`（`url`、`outputType`=png），图片由腾讯云 COS 提供。
- 模拟网络测试（`tests/unit/test_image_runninghub.py`）覆盖：提交 → 查询 → 成功、605/40310/403/429 未受理、轮询到时后按 taskId 恢复为成功或失败、提交超时进入人工核查、图生图只发送本人资产的签名 URL、尺寸映射、付费前拒绝。
