# 首条视频（后端）

> 2026-10-31。方案见 `LIGHT_MODE_PLAN.md` §3，实施拆解见 `LIGHT_MODE_IMPL_PLAN.md` §3。本文记录已落地的接口契约、流程与运维事实，App 侧照此对接。

## 接口

全部带现有鉴权头与 `X-Workspace-Id`（缺省用默认工作空间）。

| 调用 | 作用 |
|---|---|
| `GET /api/first-video` | 本工作空间首条视频的状态 |
| `POST /api/first-video` | 提交表单，启动生成；同一工作空间只跑一次，重复 POST 返回同一作业 |
| `GET /api/first-video/templates` | 表单选项：行业、模板、预计耗时 |

```
POST body: { "industry": "food"|"beauty"|"retail"|"other",   // 缺省 other
             "shop_name": string|null, "headline": string|null,
             "template": "signature"|"ambience"|"promo",      // 招牌推荐 / 门店氛围 / 活动促销
             "asset_id": string|null }                        // 参考图：本人已 complete 的 image/* 资产
→ FirstVideoState = { "status": "none"|"skipped"|"submitted"|"running"|"ready"|"failed",
                      "session_id", "job_id", "asset_id",       // asset_id 只在 ready 时给，是最终成片
                      "eta_seconds", "error": {"code","message"}|null }
```

- 行业会回写 `assistant_profile.business`（只接受 food / beauty / retail）。
- 状态存在 `user_preferences.extra.first_video`（独立键；不进 `onboarding`，那张表被手机整体替换）。App 跳过整个流程时写 `PUT /api/auth/me/preferences {"extra":{"first_video":{"status":"skipped"}}}`。
- 完成/失败各推一条通知：kind `first_video_ready` / `first_video_failed`，链接是成片所在会话。
- 错误：`400 FIRST_VIDEO_INVALID`（参考图不是本人的图片等）。余额从不成为拒绝理由（见下）。

## 流程（`backend/light/first_video.py`）

1. 建真实会话「我的第一条视频」，`video_model / video_resolution` 由档位 `low` 解析（`api/v1/quality.resolve_quality`），落一条用户消息（表单摘要）和一条占位助理消息。
2. 用与 agent 相同的 `ToolContext`（带 `message_id`）直接调 `video_generate`：`submit` 后循环 `wait`，完成时工具自己把成片 FilePart 贴到占位消息上。提示词来自 `backend/light/first_video_templates.json`（行业 4 × 模板 3，各有带图/不带图两版，店名不进提示词）。
3. 有店名时再过一次 `video_compose`：店名 + 一句 slogan（主打优先）叠在成片下方，最终成片是合成结果；合成失败则交付原片。
4. 写固定助理文案 + 3 条后续建议，`finish=stop`，偏好 `status=ready`，推送。
5. 带参考图失败时自动去图重试一次（幂等键 `first_video:{ws}:retry`）；再失败 → `status=failed` + 推送。
6. 进程重启等导致驱动中断：`video/job_recovery.py` 的补扫识别 `first_video:` 前缀的作业，交给 `first_video.on_job_settled` 贴片、交付或标失败，**不会**唤醒 agent。

幂等键：生成 `first_video:{workspace_id}`，花字 `first_video_text:{job_id}`。无照片、同模板的用户命中现有的跨用户成片复用（秒出、不计费），这是拍板允许的。

## 赠送（`backend/billing/signup_gift.py`）

- 注册即送：`plans.json` free 档 `signup_gift_credits`（现 4 积分），kind `signup_gift`，幂等键 `signup_gift:{ws}`，密码注册与 Logto 首登两条路径都发；账单标签「新人首条视频」。
- 提交首条视频时若余额不够一条（15 秒 H3 768p = 3.75 积分），一次性补差额，幂等键 `first_video_topup:{ws}`。只在 `BILLING_MODE=enforce` 下执行。

## M0 实测（2026-10-31，gw2，MiniMax-H3 768p 9:16 15 秒，20 条不同提示词，4 并发）

| 指标 | 值 |
|---|---|
| 成功率 | 20/20 |
| 最短 / p50 / p90 / 最长 | 40.8s / 67.3s / 91.5s / 96.4s |

纯供应商生成耗时，不含拷 OSS（几秒）与花字合成（约 20–30 秒）。模板文件 `eta_seconds=110` 据此设定。带参考图的出片率尚未测（需要真实菜品/门头照片）。

## 运维

- 模板文件随镜像走；`LIGHT_FIRST_VIDEO_TEMPLATES_FILE` 可指向挂载路径，改文案只需重启后端。
- 需要 `openbox.json` 的 `model_tiers.video` 含 `low`（gw2 已是 MiniMax-H3 768p）。没有档位配置时走部署默认模型。
- 无数据库迁移。
