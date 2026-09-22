# 全仓文档目录

由 `python3 scripts/check_docs.py --write-index` 生成。覆盖 Git 管理及未忽略的新文档；不收录私有本地文件。

按阅读任务进入[文档首页](README.md)。本表的记录类型由目录表示，历史结果不代表当前线上状态。

## backend

| 文档 | 路径 |
|---|---|
| [发布到抖音（云电脑创作者中心）](../backend/.openbox/skills/douyin-desktop-publish/SKILL.md) | `backend/.openbox/skills/douyin-desktop-publish/SKILL.md` |
| [扫码投稿（开放平台兜底）](../backend/.openbox/skills/douyin-publish/SKILL.md) | `backend/.openbox/skills/douyin-publish/SKILL.md` |
| [OpenBox Image Generation](../backend/.openbox/skills/imagegen/SKILL.md) | `backend/.openbox/skills/imagegen/SKILL.md` |
| [自动营销（marketing-autopilot）](../backend/.openbox/skills/marketing-autopilot/SKILL.md) | `backend/.openbox/skills/marketing-autopilot/SKILL.md` |
| [形态 → 配方（B4）](../backend/.openbox/skills/marketing-autopilot/references/recipes.md) | `backend/.openbox/skills/marketing-autopilot/references/recipes.md` |
| [定时任务引导(聊天创建)](../backend/.openbox/skills/scheduled-tasks/SKILL.md) | `backend/.openbox/skills/scheduled-tasks/SKILL.md` |
| [OpenBox Skill Creator](../backend/.openbox/skills/skill-creator/SKILL.md) | `backend/.openbox/skills/skill-creator/SKILL.md` |
| [Spoken video production](../backend/.openbox/skills/video-production/SKILL.md) | `backend/.openbox/skills/video-production/SKILL.md` |
| [Cloud composition: the timeline you hand to `video_compose`](../backend/.openbox/skills/video-production/references/compose-timeline.md) | `backend/.openbox/skills/video-production/references/compose-timeline.md` |
| [Choosing a model, and holding a look steady](../backend/.openbox/skills/video-production/references/model-guide.md) | `backend/.openbox/skills/video-production/references/model-guide.md` |
| [口播段 prompt 工艺](../backend/.openbox/skills/video-production/references/prompt-recipes.md) | `backend/.openbox/skills/video-production/references/prompt-recipes.md` |
| [生成结果、STT 与时长质检](../backend/.openbox/skills/video-production/references/quality.md) | `backend/.openbox/skills/video-production/references/quality.md` |
| [Deletion boundary / ECD 与浏览器统一删除边界](../backend/AGENTS.md) | `backend/AGENTS.md` |
| [OpenBox Backend](../backend/README.md) | `backend/README.md` |

## container

| 文档 | 路径 |
|---|---|
| [执行环境与浏览器运行时](../container/README.md) | `container/README.md` |
| [Dev Browser 运行时](../container/dev-browser/README.md) | `container/dev-browser/README.md` |
| [Dev Browser Skill](../container/dev-browser/SKILL.md) | `container/dev-browser/SKILL.md` |
| [Data Scraping Guide](../container/dev-browser/references/scraping.md) | `container/dev-browser/references/scraping.md` |

## deploy

| 文档 | 路径 |
|---|---|
| [部署文件](../deploy/README.md) | `deploy/README.md` |
| [gw2 runbook: trajectory worker topology](../deploy/gw2/RUNBOOK.md) | `deploy/gw2/RUNBOOK.md` |

## docs

| 文档 | 路径 |
|---|---|
| [OpenBox 文档](README.md) | `docs/README.md` |

## docs/architecture

| 文档 | 路径 |
|---|---|
| [Agent 能力与代码职责](architecture/AGENT_CAPABILITIES.md) | `docs/architecture/AGENT_CAPABILITIES.md` |
| [Agent Kernel 与当前主线的整合](architecture/AGENT_KERNEL_ARCHITECTURE.md) | `docs/architecture/AGENT_KERNEL_ARCHITECTURE.md` |
| [风控/验证码阻塞时提醒用户接管 + 一键跳转云桌面](architecture/DESKTOP_TAKEOVER.md) | `docs/architecture/DESKTOP_TAKEOVER.md` |
| [图片生成与 external-effect ledger](architecture/MEDIA_EFFECT_SAFETY.md) | `docs/architecture/MEDIA_EFFECT_SAFETY.md` |
| [消息中心（M1 后端 + M2 Web + M3 App + M4 后台界面）](architecture/MESSAGE_CENTER.md) | `docs/architecture/MESSAGE_CENTER.md` |
| [架构与生命周期](architecture/README.md) | `docs/architecture/README.md` |
| [Skill Provider lifecycle and scope contract](architecture/SKILL_PROVIDER_LIFECYCLE.md) | `docs/architecture/SKILL_PROVIDER_LIFECYCLE.md` |
| [付费后自动开通无影云 / Free 普通对话](architecture/SUBSCRIPTION_SANDBOX.md) | `docs/architecture/SUBSCRIPTION_SANDBOX.md` |
| [聊天输入框上方的下一步建议](architecture/next-step-suggestions.md) | `docs/architecture/next-step-suggestions.md` |

## docs/archive

| 文档 | 路径 |
|---|---|
| [OpenAgent 前端设计文档](archive/FRONTEND_DESIGN.md) | `docs/archive/FRONTEND_DESIGN.md` |
| [OpenAgent: Python 通用 Agent 框架实现方案](archive/OPENAGENT_DESIGN.md) | `docs/archive/OPENAGENT_DESIGN.md` |
| [历史与搁置方案](archive/README.md) | `docs/archive/README.md` |
| [【已归档】OpenBox 通用 Skill Script 作业运行时整体改建规划](archive/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md) | `docs/archive/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md` |
| [云老板团购接入 —— 搁置记录与调查结果（2026-09-08）](archive/YUNLAOBAN_GROUPBUY_SHELVED.md) | `docs/archive/YUNLAOBAN_GROUPBUY_SHELVED.md` |

## docs/contributing

| 文档 | 路径 |
|---|---|
| [新增和整理 Agent 能力](contributing/ADDING_CAPABILITIES.md) | `docs/contributing/ADDING_CAPABILITIES.md` |
| [Shared deletion boundary](contributing/DELETION_BOUNDARY.md) | `docs/contributing/DELETION_BOUNDARY.md` |
| [OpenBox 本地运行命令](contributing/DEV_COMMANDS.md) | `docs/contributing/DEV_COMMANDS.md` |
| [文档维护规则](contributing/DOCUMENTATION.md) | `docs/contributing/DOCUMENTATION.md` |
| [开发与维护](contributing/README.md) | `docs/contributing/README.md` |

## docs/evaluations

| 文档 | 路径 |
|---|---|
| [Agent Team 评测](evaluations/README.md) | `docs/evaluations/README.md` |
| [Bounded random regression sample — 2026-09-21](evaluations/agent-team-random-20260921/PROTOCOL.md) | `docs/evaluations/agent-team-random-20260921/PROTOCOL.md` |
| [随机抽样与修复结果](evaluations/agent-team-random-20260921/RESULTS.md) | `docs/evaluations/agent-team-random-20260921/RESULTS.md` |
| [Agent Team evaluation v1 — preregistration](evaluations/agent-team-v1/PROTOCOL.md) | `docs/evaluations/agent-team-v1/PROTOCOL.md` |
| [Agent Team evaluation v2 — preregistration](evaluations/agent-team-v2/PROTOCOL.md) | `docs/evaluations/agent-team-v2/PROTOCOL.md` |
| [Agent Team evaluation v3 — preregistration](evaluations/agent-team-v3/PROTOCOL.md) | `docs/evaluations/agent-team-v3/PROTOCOL.md` |
| [Agent Team evaluation v4 — account credit preregistration](evaluations/agent-team-v4/PROTOCOL.md) | `docs/evaluations/agent-team-v4/PROTOCOL.md` |

## docs/evidence

| 文档 | 路径 |
|---|---|
| [原始证据](evidence/README.md) | `docs/evidence/README.md` |

## docs/mockups

| 文档 | 路径 |
|---|---|
| [交互草稿](mockups/README.md) | `docs/mockups/README.md` |

## docs/operations

| 文档 | 路径 |
|---|---|
| [Agent Team operations and recovery](operations/AGENT_TEAM_OPERATIONS.md) | `docs/operations/AGENT_TEAM_OPERATIONS.md` |
| [Android 厂商推送配置](operations/ANDROID_PUSH_VENDORS.md) | `docs/operations/ANDROID_PUSH_VENDORS.md` |
| [Token 积分计费与支付接入](operations/CREDIT_BILLING.md) | `docs/operations/CREDIT_BILLING.md` |
| [部署与发布](operations/DEPLOY.md) | `docs/operations/DEPLOY.md` |
| [Logto SSO — production deployment (Aliyun)](operations/LOGTO_PROD.md) | `docs/operations/LOGTO_PROD.md` |
| [运行与运维](operations/README.md) | `docs/operations/README.md` |
| [OpenBox 切换到自有 new-api 操作单](operations/RELAY_CUTOVER.md) | `docs/operations/RELAY_CUTOVER.md` |
| [Large Trace inputs and checkpoint recovery](operations/TRACE-LARGE-INPUT-REPLAY.md) | `docs/operations/TRACE-LARGE-INPUT-REPLAY.md` |
| [视频转存失败的恢复策略](operations/VIDEO_TRANSFER_RECOVERY.md) | `docs/operations/VIDEO_TRANSFER_RECOVERY.md` |
| [WUYING Cloud Desktop as a Sandbox](operations/WUYING_SANDBOX.md) | `docs/operations/WUYING_SANDBOX.md` |

## docs/plans

| 文档 | 路径 |
|---|---|
| [方案与实施计划](plans/README.md) | `docs/plans/README.md` |
| [OpenBox Agent Team 完整架构与实施计划](plans/agent/AGENT_TEAM_ARCHITECTURE_PLAN.md) | `docs/plans/agent/AGENT_TEAM_ARCHITECTURE_PLAN.md` |
| [OpenBox Cron 定时任务系统 — 完整实施计划](plans/agent/CRON_SYSTEM_PLAN.md) | `docs/plans/agent/CRON_SYSTEM_PLAN.md` |
| [直连路径清理与强化——执行手册](plans/agent/DIRECT_PATH_CLEANUP_PLAN.md) | `docs/plans/agent/DIRECT_PATH_CLEANUP_PLAN.md` |
| [方案与实施计划：agent](plans/agent/README.md) | `docs/plans/agent/README.md` |
| [技能与工具解耦——执行手册](plans/agent/SKILL_TOOL_DECOUPLING_PLAN.md) | `docs/plans/agent/SKILL_TOOL_DECOUPLING_PLAN.md` |
| [Todo 卡片生命周期修复计划](plans/agent/TODO_LIFECYCLE_PLAN.md) | `docs/plans/agent/TODO_LIFECYCLE_PLAN.md` |
| [工具目录与 Schema 延迟物化——执行手册](plans/agent/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md) | `docs/plans/agent/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md` |
| [A5 · 授权中心（抖音开放平台 OAuth + H5 投稿）—— 调研结论与执行单](plans/media/A5_AUTHORIZATION_CENTER.md) | `docs/plans/media/A5_AUTHORIZATION_CENTER.md` |
| [A5 二期 · 云电脑浏览器登录态纳入授权中心 —— 执行单](plans/media/A5_DESKTOP_LOGIN_STATE.md) | `docs/plans/media/A5_DESKTOP_LOGIN_STATE.md` |
| [C5 · 口播视频技能细化 —— Codex 开工引子](plans/media/C5_EXECUTION_BRIEF.md) | `docs/plans/media/C5_EXECUTION_BRIEF.md` |
| [C5 · 口播视频技能对照 bossip 细化 —— 调研结论 + 独立执行单](plans/media/C5_VIDEO_SKILL_ALIGNMENT.md) | `docs/plans/media/C5_VIDEO_SKILL_ALIGNMENT.md` |
| [方案与实施计划：media](plans/media/README.md) | `docs/plans/media/README.md` |
| [视频能力原子化——执行手册](plans/media/VIDEO_ATOMIZATION_PLAN.md) | `docs/plans/media/VIDEO_ATOMIZATION_PLAN.md` |
| [A1+B1 合并与归属接缝 —— 独立执行单](plans/platform/A1B1_MERGE_SEAM.md) | `docs/plans/platform/A1B1_MERGE_SEAM.md` |
| [A1 · 每桌面执行通道 —— 独立执行单](plans/platform/A1_DESKTOP_CHANNEL.md) | `docs/plans/platform/A1_DESKTOP_CHANNEL.md` |
| [A2 · 包月开通参数 —— 独立执行单](plans/platform/A2_PREPAID_DESKTOP.md) | `docs/plans/platform/A2_PREPAID_DESKTOP.md` |
| [A3 · ECD 预热池 + A4 · 舰队观测与告警 v0 —— Codex 开工引子](plans/platform/A3_A4_FLEET_POOL.md) | `docs/plans/platform/A3_A4_FLEET_POOL.md` |
| [B1 · Workspace + 审计 + 内置周期任务原语 —— 独立执行单](plans/platform/B1_WORKSPACE_AUDIT.md) | `docs/plans/platform/B1_WORKSPACE_AUDIT.md` |
| [D1 · ECD 浏览器调试监控（方案 v1，2026-09-07；L0–L2 + L4 已实现，见 §7）](plans/platform/D1_ECD_BROWSER_DEBUG.md) | `docs/plans/platform/D1_ECD_BROWSER_DEBUG.md` |
| [多用户隔离 + 数据存储架构重构计划](plans/platform/MULTI_USER_STORAGE_PLAN.md) | `docs/plans/platform/MULTI_USER_STORAGE_PLAN.md` |
| [OpenBox 终端升级计划: 命令-响应模式 → PTY 交互模式](plans/platform/PTY_UPGRADE_PLAN.md) | `docs/plans/platform/PTY_UPGRADE_PLAN.md` |
| [方案与实施计划：platform](plans/platform/README.md) | `docs/plans/platform/README.md` |
| [超管系统 + 第一方技能商店 —— 设计规划与执行单](plans/product/ADMIN_CONSOLE_SKILL_STORE_PLAN.md) | `docs/plans/product/ADMIN_CONSOLE_SKILL_STORE_PLAN.md` |
| [自动营销定时任务模版（Autopilot）—— 现状对照、设计与分期](plans/product/AUTO_MARKETING_AUTOPILOT_PLAN.md) | `docs/plans/product/AUTO_MARKETING_AUTOPILOT_PLAN.md` |
| [OpenBox 计费方案](plans/product/BILLING_PLAN.md) | `docs/plans/product/BILLING_PLAN.md` |
| [里程碑一、二详细计划（给编码执行用）](plans/product/DETAILED_PLAN_M1_M2.md) | `docs/plans/product/DETAILED_PLAN_M1_M2.md` |
| [前端操作优化：新对话 / 返回主页 / Profile 页 —— 规划](plans/product/FRONTEND_NAV_PROFILE_PLAN.md) | `docs/plans/product/FRONTEND_NAV_PROFILE_PLAN.md` |
| [消息中心方案（v1 草案，2026-09-11）](plans/product/MESSAGE_CENTER_PLAN.md) | `docs/plans/product/MESSAGE_CENTER_PLAN.md` |
| [OpenBox 性能优化计划](plans/product/PERFORMANCE_OPTIMIZATION.md) | `docs/plans/product/PERFORMANCE_OPTIMIZATION.md` |
| [OpenBox 产品推进计划（面向市场与产品同事）](plans/product/PLAN_SHARE.md) | `docs/plans/product/PLAN_SHARE.md` |
| [方案与实施计划：product](plans/product/README.md) | `docs/plans/product/README.md` |
| [OpenBox 待办计划（粗版，供排优先级）](plans/product/ROADMAP_DRAFT.md) | `docs/plans/product/ROADMAP_DRAFT.md` |
| [方案与实施计划：trajectory](plans/trajectory/README.md) | `docs/plans/trajectory/README.md` |
| [会话执行轨迹与回放：超管后台完整实施计划](plans/trajectory/SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md) | `docs/plans/trajectory/SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md` |

## docs/reference

| 文档 | 路径 |
|---|---|
| [Agent Team API 接入说明](reference/AGENT_TEAM_API_HANDOFF.md) | `docs/reference/AGENT_TEAM_API_HANDOFF.md` |
| [OpenBox Backend API Interfaces](reference/API_INTERFACES.md) | `docs/reference/API_INTERFACES.md` |
| [移动通知接口与单手机登录](reference/MOBILE_NOTIFICATIONS.md) | `docs/reference/MOBILE_NOTIFICATIONS.md` |
| [接口与能力参考](reference/README.md) | `docs/reference/README.md` |
| [会话轨迹协议 v2](reference/SESSION_TRAJECTORY_PROTOCOL.md) | `docs/reference/SESSION_TRAJECTORY_PROTOCOL.md` |
| [超管会话轨迹监控：前端展示与详情字段规格](reference/SESSION_TRAJECTORY_UI_SPEC.md) | `docs/reference/SESSION_TRAJECTORY_UI_SPEC.md` |
| [技能、指令与资源目录](reference/SKILLS.md) | `docs/reference/SKILLS.md` |
| [工具目录](reference/TOOLS.md) | `docs/reference/TOOLS.md` |

## docs/reports

| 文档 | 路径 |
|---|---|
| [OpenBox 开发日志](reports/DEVLOG.md) | `docs/reports/DEVLOG.md` |
| [实现、验收与发布记录](reports/README.md) | `docs/reports/README.md` |
| [同模型压缩、80% 阈值与请求预算核查](reports/agent/AGENT_COMPACTION_REVIEW.md) | `docs/reports/agent/AGENT_COMPACTION_REVIEW.md` |
| [长会话、200 条分页与上下文压缩核查](reports/agent/AGENT_LONG_HISTORY_PARITY.md) | `docs/reports/agent/AGENT_LONG_HISTORY_PARITY.md` |
| [Agent 重构与最新 main 的整合记录](reports/agent/AGENT_MAIN_INTEGRATION.md) | `docs/reports/agent/AGENT_MAIN_INTEGRATION.md` |
| [Agent 重构分支响应延迟排查（2026-09-17）](reports/agent/AGENT_RESPONSE_LATENCY.md) | `docs/reports/agent/AGENT_RESPONSE_LATENCY.md` |
| [Agent Team acceptance evidence](reports/agent/AGENT_TEAM_ACCEPTANCE.md) | `docs/reports/agent/AGENT_TEAM_ACCEPTANCE.md` |
| [Agent Team implementation and verification](reports/agent/AGENT_TEAM_IMPLEMENTATION.md) | `docs/reports/agent/AGENT_TEAM_IMPLEMENTATION.md` |
| [实现、验收与发布记录：agent](reports/agent/README.md) | `docs/reports/agent/README.md` |
| [部署历史与旧版操作说明（2026 年 9 月）](reports/deployment/DEPLOYMENT_HISTORY_202609.md) | `docs/reports/deployment/DEPLOYMENT_HISTORY_202609.md` |
| [阿里云聊天建议补发记录（2026-09-10）](reports/deployment/FRONTEND_SUGGESTIONS_DEPLOY_20260910.md) | `docs/reports/deployment/FRONTEND_SUGGESTIONS_DEPLOY_20260910.md` |
| [阿里云移动端通知后端发布记录（2026-09-10）](reports/deployment/MOBILE_PUSH_DEPLOY_20260910.md) | `docs/reports/deployment/MOBILE_PUSH_DEPLOY_20260910.md` |
| [实现、验收与发布记录：deployment](reports/deployment/README.md) | `docs/reports/deployment/README.md` |
| [三条建议完整展示（2026-09-10）](reports/deployment/SUGGESTION_LAYOUT_DEPLOY_20260910.md) | `docs/reports/deployment/SUGGESTION_LAYOUT_DEPLOY_20260910.md` |
| [建议生成流光占位发布（2026-09-10）](reports/deployment/SUGGESTION_LOADING_DEPLOY_20260910.md) | `docs/reports/deployment/SUGGESTION_LOADING_DEPLOY_20260910.md` |
| [建议卡滚动修复发布（2026-09-11）](reports/deployment/SUGGESTION_SCROLL_RELEASE_20260911.md) | `docs/reports/deployment/SUGGESTION_SCROLL_RELEASE_20260911.md` |
| [实现、验收与发布记录：interaction](reports/interaction/README.md) | `docs/reports/interaction/README.md` |
| [Ask 卡片分页验收](reports/interaction/ask-pagination.md) | `docs/reports/interaction/ask-pagination.md` |
| [Ask 状态与异常回归验收](reports/interaction/durable-ask-state-matrix.md) | `docs/reports/interaction/durable-ask-state-matrix.md` |
| [Durable ask implementation and acceptance checklist](reports/interaction/durable-ask.md) | `docs/reports/interaction/durable-ask.md` |
| [MiniMax 视频提交故障修复与验收（2026-09-09）](reports/media/MINIMAX_VIDEO_FIX_QA_20260909.md) | `docs/reports/media/MINIMAX_VIDEO_FIX_QA_20260909.md` |
| [P0 发布保护验收（2026-09-11）](reports/media/P0_PUBLISH_ACCEPTANCE_20260911.md) | `docs/reports/media/P0_PUBLISH_ACCEPTANCE_20260911.md` |
| [实现、验收与发布记录：media](reports/media/README.md) | `docs/reports/media/README.md` |
| [视频排版与前端资源故障回归（2026-09-09）](reports/media/VIDEO_LAYOUT_AND_FRONTEND_RECOVERY_QA_20260909.md) | `docs/reports/media/VIDEO_LAYOUT_AND_FRONTEND_RECOVERY_QA_20260909.md` |
| [超管通知测试与移动端默认通知（2026-09-10）](reports/mobile/ADMIN_NOTIFICATION_TEST_20260910.md) | `docs/reports/mobile/ADMIN_NOTIFICATION_TEST_20260910.md` |
| [Android 后台通知排查（2026-09-11）](reports/mobile/ANDROID_BACKGROUND_PUSH_20260911.md) | `docs/reports/mobile/ANDROID_BACKGROUND_PUSH_20260911.md` |
| [Android 登录回跳修复与验收 — 2026-09-09](reports/mobile/ANDROID_LOGIN_QA_20260909.md) | `docs/reports/mobile/ANDROID_LOGIN_QA_20260909.md` |
| [移动端 1.0.17（28）打包记录](reports/mobile/MOBILE_RELEASE_1_0_17_20260910.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_17_20260910.md` |
| [移动端 1.0.19 (30) 发版记录（2026-09-15）](reports/mobile/MOBILE_RELEASE_1_0_19_20260915.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_19_20260915.md` |
| [原生移动端 1.0.20+31：构建与生产联调记录](reports/mobile/MOBILE_RELEASE_1_0_20_20260916.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_20_20260916.md` |
| [iOS 1.0.21 (32) TestFlight 发布记录](reports/mobile/MOBILE_RELEASE_1_0_21_20260916.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_21_20260916.md` |
| [iOS 1.0.22 (33)：Ask 卡片滚动修复](reports/mobile/MOBILE_RELEASE_1_0_22_20260916.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_22_20260916.md` |
| [移动端 1.0.23 (34) 发版记录（2026-09-16）](reports/mobile/MOBILE_RELEASE_1_0_23_20260916.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_23_20260916.md` |
| [移动端 1.0.24 (35) 发布记录（2026-09-18）](reports/mobile/MOBILE_RELEASE_1_0_24_20260918.md) | `docs/reports/mobile/MOBILE_RELEASE_1_0_24_20260918.md` |
| [Web → 移动端对齐清单](reports/mobile/MOBILE_WEB_PARITY.md) | `docs/reports/mobile/MOBILE_WEB_PARITY.md` |
| [实现、验收与发布记录：mobile](reports/mobile/README.md) | `docs/reports/mobile/README.md` |
| [移动端早期接入与验收记录（2026 年 9 月）](reports/mobile/README_HISTORY_202609.md) | `docs/reports/mobile/README_HISTORY_202609.md` |
| [A5 授权中心 · 验证清单（给 Codex）](reports/platform/A5_VERIFY_CHECKLIST.md) | `docs/reports/platform/A5_VERIFY_CHECKLIST.md` |
| [超管技能管理 CRUD 与虚拟机安装管理](reports/platform/ADMIN_SKILL_MANAGEMENT_QA.md) | `docs/reports/platform/ADMIN_SKILL_MANAGEMENT_QA.md` |
| [B1 Workspace 审计执行记录](reports/platform/B1_EXECUTION_RECORD.md) | `docs/reports/platform/B1_EXECUTION_RECORD.md` |
| [OpenBox Backend 重构总结](reports/platform/BACKEND_REFACTOR.md) | `docs/reports/platform/BACKEND_REFACTOR.md` |
| [计费实现审查（队友 bfd08a4 「积分套餐 + 支付宝」）](reports/platform/BILLING_REVIEW_2026-09-07.md) | `docs/reports/platform/BILLING_REVIEW_2026-09-07.md` |
| [对 DETAILED_PLAN_M1_M2 的对抗审查](reports/platform/DETAILED_PLAN_M1_M2_REVIEW.md) | `docs/reports/platform/DETAILED_PLAN_M1_M2_REVIEW.md` |
| [里程碑一「打通」验收步骤](reports/platform/M1_ACCEPTANCE.md) | `docs/reports/platform/M1_ACCEPTANCE.md` |
| [里程碑一验收证据（2026-09-04）](reports/platform/M1_ACCEPTANCE_EVIDENCE.md) | `docs/reports/platform/M1_ACCEPTANCE_EVIDENCE.md` |
| [QWH 云电脑启动卡住与页面遗忘修复](reports/platform/QWH_BROWSER_RECOVERY_20260909.md) | `docs/reports/platform/QWH_BROWSER_RECOVERY_20260909.md` |
| [实现、验收与发布记录：platform](reports/platform/README.md) | `docs/reports/platform/README.md` |
| [实现、验收与发布记录：trajectory](reports/trajectory/README.md) | `docs/reports/trajectory/README.md` |
| [会话轨迹前端验证记录](reports/trajectory/SESSION_TRAJECTORY_FRONTEND_VERIFICATION.md) | `docs/reports/trajectory/SESSION_TRAJECTORY_FRONTEND_VERIFICATION.md` |
| [Web v2 设计对齐与早期实施记录（2026 年 8 月）](reports/web/FRONTEND_V2_ALIGNMENT_202608.md) | `docs/reports/web/FRONTEND_V2_ALIGNMENT_202608.md` |
| [Web 设计与实施记录](reports/web/README.md) | `docs/reports/web/README.md` |

## docs/research

| 文档 | 路径 |
|---|---|
| [DeepSeek Harness 与 OpenBox 源码级对比分析（重构基线与当前状态）](research/DeepSeek-Harness-vs-OpenBox-source-analysis.md) | `docs/research/DeepSeek-Harness-vs-OpenBox-source-analysis.md` |
| [OpenBox vs opencode - Feature Comparison & Roadmap](research/FEATURE_COMPARISON.md) | `docs/research/FEATURE_COMPARISON.md` |
| [OpenBox 对 bossip 的差距盘点（查漏补缺）](research/GAP_ANALYSIS.md) | `docs/research/GAP_ANALYSIS.md` |
| [移动端系统通知迁移分析](research/MOBILE_NOTIFICATIONS_ANALYSIS.md) | `docs/research/MOBILE_NOTIFICATIONS_ANALYSIS.md` |
| [研究与选型](research/README.md) | `docs/research/README.md` |
| [视频合成引擎选型：调研、实测与切换手册](research/VIDEO_RENDER_ENGINE_SELECTION.md) | `docs/research/VIDEO_RENDER_ENGINE_SELECTION.md` |
| [Cron 触发 LLM 架构分析](research/cron-llm-trigger-architecture.md) | `docs/research/cron-llm-trigger-architecture.md` |

## docs/spikes

| 文档 | 路径 |
|---|---|
| [M0 spike 记录：自动营销模版（2026-09-09 晚）](spikes/M0_AUTOPILOT_SPIKES_20260909.md) | `docs/spikes/M0_AUTOPILOT_SPIKES_20260909.md` |
| [探测实验](spikes/README.md) | `docs/spikes/README.md` |

## docs/trajectory-rearch

| 文档 | 路径 |
|---|---|
| [聊天容量与 Trace 资源隔离实施计划](trajectory-rearch/CHAT_CAPACITY_PLAN.md) | `docs/trajectory-rearch/CHAT_CAPACITY_PLAN.md` |
| [Trace 批量入库修复与聊天混合压测（2026-09-15）](trajectory-rearch/LOADTEST-2026-09-15-MIXED.md) | `docs/trajectory-rearch/LOADTEST-2026-09-15-MIXED.md` |
| [Trace 百万事件压测（2026-09-15）](trajectory-rearch/LOADTEST-2026-09-15.md) | `docs/trajectory-rearch/LOADTEST-2026-09-15.md` |
| [会话轨迹改造专题](trajectory-rearch/README.md) | `docs/trajectory-rearch/README.md` |
| [Session trajectory re-architecture: implementation spec (v1)](trajectory-rearch/SPEC.md) | `docs/trajectory-rearch/SPEC.md` |
| [Wave 3 work packages (trajectory re-architecture, code only)](trajectory-rearch/WAVE3.md) | `docs/trajectory-rearch/WAVE3.md` |
| [改造前源码调查](trajectory-rearch/maps/README.md) | `docs/trajectory-rearch/maps/README.md` |
| [Trajectory admin API, WebSocket, auth and frontend contract: map for the re-architecture](trajectory-rearch/maps/api.md) | `docs/trajectory-rearch/maps/api.md` |
| [Infrastructure, Deploy and Dependencies: Current State Before the Trajectory Re-architecture](trajectory-rearch/maps/infra.full.md) | `docs/trajectory-rearch/maps/infra.full.md` |
| [Infrastructure migration notes — historical excerpt](trajectory-rearch/maps/infra.md) | `docs/trajectory-rearch/maps/infra.md` |
| [Trajectory producers and error handling in `backend/` (excluding `backend/trajectory/` and `backend/tests/`)](trajectory-rearch/maps/producers.md) | `docs/trajectory-rearch/maps/producers.md` |
| [Trajectory map: projection, reads, payloads, media, export and deletion](trajectory-rearch/maps/projection.md) | `docs/trajectory-rearch/maps/projection.md` |
| [Recorder core and event model: current-state map](trajectory-rearch/maps/recorder.full.md) | `docs/trajectory-rearch/maps/recorder.full.md` |
| [Recorder migration notes — historical excerpt](trajectory-rearch/maps/recorder.md) | `docs/trajectory-rearch/maps/recorder.md` |
| [Execution runtime and fencing: current state and re-architecture map](trajectory-rearch/maps/runtime.md) | `docs/trajectory-rearch/maps/runtime.md` |
| [Trajectory tests and failure semantics — historical excerpt](trajectory-rearch/maps/tests.md) | `docs/trajectory-rearch/maps/tests.md` |
| [轨迹阶段报告](trajectory-rearch/reports/README.md) | `docs/trajectory-rearch/reports/README.md` |
| [Wave 1 outcomes and contract decisions for wave 2](trajectory-rearch/reports/wave1/NOTES.md) | `docs/trajectory-rearch/reports/wave1/NOTES.md` |
| [Wave 2 integration (`integ/wave2`)](trajectory-rearch/reports/wave2/INTEGRATION.md) | `docs/trajectory-rearch/reports/wave2/INTEGRATION.md` |
| [w2-ingest independent verification (second run)](trajectory-rearch/reports/wave2/w2-ingest.verify-r2.md) | `docs/trajectory-rearch/reports/wave2/w2-ingest.verify-r2.md` |
| [w3-analytics report](trajectory-rearch/reports/wave3/w3-analytics.md) | `docs/trajectory-rearch/reports/wave3/w3-analytics.md` |
| [w3-frontend report](trajectory-rearch/reports/wave3/w3-frontend.md) | `docs/trajectory-rearch/reports/wave3/w3-frontend.md` |
| [w3-harden-producers report](trajectory-rearch/reports/wave3/w3-harden-producers.md) | `docs/trajectory-rearch/reports/wave3/w3-harden-producers.md` |
| [w3-harden-service report](trajectory-rearch/reports/wave3/w3-harden-service.md) | `docs/trajectory-rearch/reports/wave3/w3-harden-service.md` |
| [w3-harden-worker report](trajectory-rearch/reports/wave3/w3-harden-worker.md) | `docs/trajectory-rearch/reports/wave3/w3-harden-worker.md` |
| [w3-ops report](trajectory-rearch/reports/wave3/w3-ops.md) | `docs/trajectory-rearch/reports/wave3/w3-ops.md` |
| [w3-spool-blobs report](trajectory-rearch/reports/wave3/w3-spool-blobs.md) | `docs/trajectory-rearch/reports/wave3/w3-spool-blobs.md` |

## extension

| 文档 | 路径 |
|---|---|
| [浏览器扩展](../extension/README.md) | `extension/README.md` |

## frontend

| 文档 | 路径 |
|---|---|
| [旧版 Web 客户端](../frontend/README.md) | `frontend/README.md` |

## frontend-v2

| 文档 | 路径 |
|---|---|
| [OpenBox Web](../frontend-v2/README.md) | `frontend-v2/README.md` |
| [OpenBox Frontend v2 · 工程规范](../frontend-v2/docs/ENGINEERING_SPEC.md) | `frontend-v2/docs/ENGINEERING_SPEC.md` |

## k8s

| 文档 | 路径 |
|---|---|
| [Kubernetes 遗留部署模板](../k8s/README.md) | `k8s/README.md` |

## mobile

| 文档 | 路径 |
|---|---|
| [OpenBox Mobile](../mobile/README.md) | `mobile/README.md` |
| [移动客户端接入与平台约定](../mobile/docs/INTEGRATION.md) | `mobile/docs/INTEGRATION.md` |
| [iOS 启动画面资源](../mobile/ios/Runner/Assets.xcassets/LaunchImage.imageset/README.md) | `mobile/ios/Runner/Assets.xcassets/LaunchImage.imageset/README.md` |
| [CHANGELOG](../mobile/third_party/video_thumbnail/CHANGELOG.md) | `mobile/third_party/video_thumbnail/CHANGELOG.md` |
| [video_thumbnail (vendored)](../mobile/third_party/video_thumbnail/README.md) | `mobile/third_party/video_thumbnail/README.md` |

## 项目入口

| 文档 | 路径 |
|---|---|
| [开发与文档维护](../CONTRIBUTING.md) | `CONTRIBUTING.md` |
| [OpenBox](../README.md) | `README.md` |
| [OpenBox](../README.zh-CN.md) | `README.zh-CN.md` |
