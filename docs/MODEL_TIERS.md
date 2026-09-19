# 模型档位（Composer 三档选择）

2026-09-19 起，Composer 的模型选择可以从「模型目录」换成「三档」：语言模型 **深度 / 专业 / 快速**，视频 **高 / 中 / 低**。
档位不是新的路由概念，而是部署侧声明的**预设**：每一档解析成一个真实模型（视频还带一个分辨率），前端点选后仍然按具体
`model` / `variant` / `video_model` + `video_resolution` 发请求。session 记录、计费、对话元信息、`video_generate`
工具全部不变，对话元信息和用量页继续显示真实模型名。

## 配置

`openbox.json` 顶层加 `model_tiers`。两个列表都为空（默认）时，前端保持原来的完整选择器，所以未配置的部署不受影响。

```jsonc
"model_tiers": {
  "chat": [
    { "tier": "high",   "model": "openai/qwen3.8-max",      "variant": "xhigh" },
    { "tier": "medium", "model": "openai/gemini-3.8-flash", "variant": "medium" },
    { "tier": "low",    "model": "openai/qwen3.8-flash",    "variant": "low" }
  ],
  "video": [
    { "tier": "high",   "model": "video-sd-1080p-pro", "resolution": "1080p" },
    { "tier": "medium", "model": "wan3.0-video",       "resolution": "720p" },
    { "tier": "low",    "model": "MiniMax-H3",         "resolution": "768p" }
  ]
}
```

规则：

- `tier` 只能是 `high` / `medium` / `low`，每个列表里不能重复。
- `chat[].model` 必须出现在 `models` 里（`models` 为空时只能是顶层 `model`）。
- `video[].model` 必须出现在 `video_generation.models` 里，且不能被 `allowed_models` 排除；`resolution` 必须是该模型声明的档位之一，留空取 `default_resolution`。
- 以上任一条不满足，**后端启动即报错**，而不是让用户点到一个提交会被拒的组合。
- `chat[].variant` 是随档位一起发送的思考强度，留空用模型自身默认。它在 `/api/agent/config` 出口按模型的
  reasoning profile 校验，不合法的值降级为默认并打 warning（Qwen 3.8 只接受 `none/low/medium/xhigh`）。

改了 `openbox.json` 后要 `docker compose up -d --no-deps backend`，配置只在启动时读。

## 前端行为

- Web `ModelControls` / 移动端 `showChatTierPicker` `showVideoTierPicker`：有档位就画三档，菜单里每一档的小字写它解析到的模型（视频是「模型 · 分辨率」）。
- 当前选择落不进任何档（老会话钉在已下线的模型、管理员从目录里挑的）时，胶囊显示真实模型名，不强行归档。
- 语言模型的「思考强度」下拉在有档位时**撤掉**，强度随档位走。
- `role=admin` 的账号在档位菜单底部有「更多模型…」，展开完整目录和思考强度；普通用户看不到。

## 线上映射（gw2，2026-09-19 拍板）

| 档 | 语言模型 | 视频 |
|---|---|---|
| 高（深度） | Qwen3.8 Max @ xhigh | SD 1080p Pro @ 1080p |
| 中（专业） | Gemini 3.8 Flash @ medium（现默认） | Wan 3.0 @ 720p（现默认） |
| 低（快速） | Qwen3.8 Flash @ low | MiniMax H3 @ 768p |

DeepSeek 两条不带视觉，不进档位；GPT-5.6 Luna、Seedance 系列、Wan 3.0 Prime 留在目录里给管理员。
生图（gpt-image-2 的 quality）这次没有做档位。
