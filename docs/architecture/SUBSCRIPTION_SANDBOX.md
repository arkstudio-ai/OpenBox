# 付费后自动开通无影云 / Free 普通对话

本次实现适用于 `SANDBOX_PROVIDER=wuying`、`WUYING_ROUTING=per_desktop`（线上同时设置 `WUYING_MODE=per_user`）。工作空间是套餐和云电脑的归属边界，空间成员共享空间权益。本文覆盖此前“用户先手动开通桌面才能聊天”的行为；生命周期机制不调整积分额度与模型计费规则。2026-09-18 已按用户要求恢复正式套餐价格（Pro ¥499/月、Max ¥1,999/月，年付均为月价的 12 倍），见 [套餐价格说明](../operations/CREDIT_BILLING.md)。此前 ¥0.10 临时测试价只保留在显式启用的支付测试配置；云厂商费用不随测试价降低。

## 用户行为

- Free 仍可进行普通 LLM 对话，按现有免费积分规则计量。没有桌面、正在开通、或套餐到期都不再拒绝整个聊天请求。
- Free 调用需要 sandbox 的工具时得到正常的工具错误 `SANDBOX_SUBSCRIPTION_REQUIRED`。付费但桌面未就绪时返回 `DESKTOP_NOT_READY`；不会回退到共享机器或在服务器本机执行命令。
- 只有已验证的成功支付才会触发开通：购买 Pro/Max，或已有有效付费套餐的用户额外充值。创建订单、前端支付跳转、打开侧边栏/状态接口都不购买机器；Free 的历史积分充值到账不授予云电脑权益。
- 付费成功后全局弹窗展示“付款确认 → 分配/创建 → 启动 → 连接验证 → 就绪”。支付页面同样挂载弹窗。关闭弹窗可继续聊天，也可从状态按钮重新打开；刷新、关闭浏览器、换设备后从服务器恢复进度。完成提示按用户、空间、支付请求隔离，确认后不反复弹出。
- 到期即按 Free 权限处理。新付费周期生效后恢复原工作空间的原机器，不重置磁盘、不清空文件、不重新分配给他人。

## 持久化与恢复

新增迁移 `a4b6c8d0e2f5`（父版本 `f3a5b7c9d1e4`），新增 `desktop_activations`。付款状态、订阅期限/积分与开通 outbox 在**同一数据库事务**提交，付款回调重放不会创建第二条任务。

每个工作空间一条状态记录，包含 `request_id`、`state`、`step`、尝试次数、用户可见错误、下次执行时间、租约及未确认购买意图。浏览器只读状态；`POST /api/desktop/provision` 保留为 owner/admin 的付费重试入口，不能绕过订阅，也不会清除未确认购买标记。

API lifespan 启动独立消费者，不依赖浏览器或 Cron 正常运行。每 5 秒扫描、每进程最多 4 个任务；数据库条件 UPDATE 仲裁跨进程租约（90 秒，15 秒续租），进程崩溃后由其他/重启进程接管。暂时性故障退避重试，最长 5 分钟。启动和后续扫描还会补齐升级前已付款的有效订阅任务；历史已绑定的 Free 桌面只补建停用任务。

状态概要：`queued` → `working`（保存具体步骤）→ `ready`；可重试故障为 `retrying`；无法确认购买结果为 `needs_attention`；无有效付费期为 `suspended`。`GET /api/desktop/status` 实时计算 `entitled`，因此停用消费者延迟不会延长后端访问权限。

开通优先使用空闲、有效期超过 3 天的 PrePaid 预热机器；否则新建 **1 Month / PrePaid / AutoPay=true / AutoRenew=false**。这是已付款用户的按需容量，与 `POOL_AUTO_PURCHASE`（额外预购库存）的开关分开。原有 PostPaid 机器不会在本任务中自动转包月，部署前需确认存量计费类型。

## 阿里云购买的特殊边界

当前官方 [CreateDesktops](https://help.aliyun.com/zh/wuying-workspace/developer-reference/api-ecd-2020-09-30-createdesktops) 和 [RenewDesktops](https://help.aliyun.com/zh/wuying-workspace/developer-reference/api-ecd-2020-09-30-renewdesktops) 接口以及本仓库 SDK 均没有 `ClientToken`。因此不能承诺在网络结果未知时既无条件重发、又绝不重复扣费。

实现先持久化购买意图再发请求。创建响应丢失/进程被杀后，先查环境与工作空间标签，找到原机器便自动绑定并继续；渠道安装中断会用同一机器和已保存密钥重装/验证。云端查询失败不能解释成“没有机器”。已绑定机器不见了，也不会自动删记录、买第二台。

若创建结果持续查不到（包含“购买意图提交后、实际发请求前”崩溃的窄窗口），或续费后尚未能确认有效期延长，状态为 `needs_attention`：持续自动查询，前端明确提示联系管理员核实云订单，**不盲目重复购买/续费**。它不是“开通已成功”。

运维处理：先用工作空间 ID、环境标签和时间窗核对 ECD 实例及云订单；找到原机器后由下一次扫描自动恢复。只有证实没有任何已接受的云订单、且不存在机器时，才可在该空间无有效租约的前提下清除对应 `purchase_*` 标记并重新排队；必须保存核对证据和审计记录，禁止批量清空任务表或删除保留机器。多个匹配机器需人工确认，不能任意选一台。

## 到期、保留与续费

WebSocket 票据记录经过成员校验的当前工作空间，终端与浏览器 relay 用它检查套餐；旧票据及扩展登录仍兼容个人默认空间，不能用请求参数随意指定其他空间。

- `billing_subscriptions.starts_at <= now < ends_at` 且非 Free 才可访问，和积分余额、`BILLING_MODE=off/shadow/enforce` 无关。缓存 SandboxClient 每次 HTTP 请求重新检查；桌面票据发放前再次检查；预览转发同样检查归属空间的订阅。
- 终端与浏览器 relay WebSocket 每次用户输入检查，另有 5 秒停用 watchdog；无影云面板轮询后关闭 SDK 会话。消费者撤销 ECD 授权并调用 [DisconnectDesktopSessions](https://help.aliyun.com/zh/wuying-workspace/developer-reference/api-ecd-2020-09-30-disconnectdesktopsessions) 断开已有云端会话，通常在到期后一个扫描周期内完成。已经提交的远端命令不在本次实现中强制终止。
- 云授权撤销需要服务端和 ECD 可用。停机期间无法即时调用云 API，重启后补做；后端请求权限仍按订阅时间即时判断。ECD 的 [ModifyEntitlement](https://help.aliyun.com/zh/wuying-workspace/developer-reference/api-ecd-2020-09-30-modifyentitlement) 只支持 Running 状态，Stopped/过期机器不会为了撤权而被启动，后续扫描会继续检查。
- 停用**不调用** stop/delete/release/recycle/rebuild，不软删除记录，不清空 `workspace_id`，不移回预热池。桌面/磁盘/密钥和归属保留；机器 ID 不变。
- 为已付款且仍有有效期的用户按需维护月付容量：当 ECD 包月即将结束、付费服务期仍需这台机器时，为**原机器**续一个月。购买意图同样落库、防止重复续费；不启用云厂商 AutoRenew。原库存自动续费任务跳过这些受订阅任务管理的机器。Free 不触发基础设施续费。
- “保留”是 OpenBox 不主动回收，不等于保证阿里云无限期保存过期资源。云厂商到期释放/运维删除后不能保证原盘仍存在，本次不自动重建来掩盖数据丢失。用户续费复用的前提是原机器仍存在。

## 部署与验证

1. 先备份数据库，再在目标部署的 `backend` 目录执行 `alembic upgrade head`；启动前 schema readiness 会检查新表。单用户 SQLite 使用 metadata 建表，保留旧非无影云/共享开发模式行为。
2. 同步发布后端与 `frontend-v2`。滚动升级期间**不能保留旧版付款/手动开通实例承接流量**：旧代码没有 outbox 和订阅访问检查；先迁移，再切换所有应用进程，避免两套编排同时操作同一机器。
3. 确认 `WUYING_MODE=per_user`、`WUYING_ROUTING=per_desktop`、镜像/办公网络/策略、渠道密钥和连接设置正确。确认阿里云余额与按需采购成本，并为服务 RAM 身份补充 `ecd:DisconnectDesktopSessions`，保留创建、启动、查询、授权、标签及按月续费权限。本地验证不会发出真实云购买或续费请求。
4. 先验证 Free 聊天与 sandbox 拒绝，再在测试环境验证支付回调、开通过程重启、浏览器刷新、到期拒绝、续费复用。生产切换需单独安排，本次代码任务没有部署到阿里云。

回归入口：`backend/tests/unit/test_desktop_activation.py`、`backend/tests/integration/test_billing_postgres.py`（仅 localhost 临时 schema），前端 `DesktopActivationDialog.test.tsx` / `DesktopTab.test.tsx`。隔离的真实浏览器测试：在 `frontend-v2` 执行 `npx playwright test --config playwright.desktop.config.ts`，仅使用本地 fixture 和拦截响应，无需真实账号，不会触发付款或云 API。

## 下次任务：回收策略（本次明确不实现）

- 决定套餐失效后的保留宽限期、用户提醒、数据导出/备份和阿里云资源续费成本承担方式。
- 制定“保留 → 待回收 → 实际释放”的审批、审计和可恢复状态机，明确云厂商自动释放的风险及应对。
- 制定磁盘清理、租户隔离、回到库存池或销毁的标准，以及回收与用户重新付款同时发生时的仲裁。
- 本次不添加回收定时器，不停机省费、不重装、不释放机器，不实现无限期保留保证。
