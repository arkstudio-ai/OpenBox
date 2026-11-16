# 火山方舟 Seedream 图片通道

`image_gen` 可以直连火山方舟的 Seedream 生图。`image_generation.model` 以 `doubao-seedream-` 开头时走这条路，凭据取 `image_generation.provider` 指向的 provider。RunningHub 通道和 gpt-image-2 路径都保留，行为不变。

和 RunningHub 上的 `seedream-v5-pro` 是同一个模型，只是少一层中转。

## 实测结论（2026-11-16，`doubao-seedream-5-0-pro-260628`）

用自有 new-api 渠道 106（火山官方）的 key，在 bossip-gw-1 上直连 `https://ark.cn-beijing.volces.com/api/v3/images/generations`。

| 项目 | 结果 |
| --- | --- |
| 文生图 | 通过，1024x1536 约 54 秒，2K 约 56 秒，中文标题和价格都写对 |
| 图生图 | 同一个接口加 `image: [url]`，约 77–92 秒；改标题、改价格、换色调都对，菜品保持不变 |
| `/images/edits` | 404，不存在 |
| 输入图来源 | 火山 TOS 签名链接、阿里云 OSS 签名链接都能拉到 |
| `size` | 精确像素；面积小于 921,600 或大于 4,624,220 报 400；`auto` 报 400；4096x1024 这种 4:1 可以 |
| 图生图不传 `size` | 按原图尺寸出（1792x2400 进，1792x2400 出） |
| `n=2` | 只返回一张，不报错 |
| `output_format` | 只有 `jpeg`、`png`；`webp` 报 400 |
| `background=transparent` | 文生图报 400：“requires exactly one input image” |
| 水印 | 不传 `watermark` 时右下角有“AI生成”角标；传 `false` 没有 |
| 返回 | `data[0].url`（TOS 签名链接，24 小时），`usage.output_tokens` = 像素数 / 256 |

账号下同时可用的还有 `doubao-seedream-5-5-261028`、`doubao-seedream-5-0-flash-260915`、`doubao-seedream-4-0-20260415`，本次没有实测，也没有定价条目。

没有实测的：单张输入图透明背景出图、多张输入图合成、内容审核被拒时的返回形态。

## 参数映射

适配在 `backend/tool/image_volcengine.py`，请求仍由 `image_gen._call_provider` 用 OpenAI SDK 发出（方舟接口和 OpenAI Images 足够接近）。

- 文生图、图生图都发 `POST /images/generations`。图生图把本人资产的 OSS 签名链接（15 分钟有效）放进 `image`，不走 `/images/edits`。轨迹里只记资产引用，不记签名链接。
- 只发方舟认的字段：`model`、`prompt`、`size`、`output_format`、`watermark: false`，以及需要时的 `image`、`background: transparent`。不发 `n`、`quality`。
- `size=WIDTHxHEIGHT`：直接使用；面积超出 4,624,220 或不足 921,600 时按原比例缩放到范围内（8 像素取整），并在结果里注明。`2048x2896`（A4 海报）会缩到约 1808x2552。
- `size=auto`：文生图用 2048x2048；图生图按原图比例换算到 2048 长边。
- 付费前拒绝并说明原因：`n>1`、蒙版、`webp`、没有输入图或多张输入图时要透明背景、超过 10 张或单张超过 10 MB 的输入图。

失败处理沿用 `openai` 通道：确定的 4xx 拒绝不扣费、可改参数重试；发送后的超时属于结果未知，不自动重试。方舟是同步接口，没有任务号可查。

## 计价

官方刊例（<https://docs.volcengine.com/docs/ark/model-pricing>）：输出图不超过 261 万像素 0.30 元/张，超过 0.60 元/张；输入图第一张免费，之后每张 0.02 元。

`billing/rates.json` 的 `doubao-seedream-5-0-pro-260628` 按 1 元 = 1 积分、不加价录入，两档记作 `1k`、`2k`，由请求的像素数决定。输入图的 0.02 元没有向用户收。

对比 RunningHub 上同一模型：0.27 / 0.54，分档线是 1024x1024。所以 1024x1536 这类尺寸在火山更便宜（0.30 对 0.54），2048x2048 略贵（0.60 对 0.54）。

## 部署配置

`openbox.json`：

```jsonc
"provider": {
  "volcengine": { "base_url": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "{env:ARK_API_KEY}" }
},
"image_generation": {
  "provider": "volcengine",
  "model": "doubao-seedream-5-0-pro-260628",
  // 设置页里的 RunningHub 选项保持可用
  "runninghub_provider": "runninghub",
  "runninghub_model": "seedream-v5-pro"
}
```

`backend.env` 加 `ARK_API_KEY`。计价文件是挂载的（`BILLING_RATES_FILE`）时，要把 `doubao-seedream-5-0-pro-260628` 条目同步进去，否则 enforce 模式下按未定价处理。

回退：把 `image_generation.provider` / `model` 改回 `runninghub` / `seedream-v5-pro`，重启 backend。
