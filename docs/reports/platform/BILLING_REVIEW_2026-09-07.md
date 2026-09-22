# 计费实现审查（队友 bfd08a4 「积分套餐 + 支付宝」）

> 文档类型：实施 / 验收记录。版本、测试数量和部署状态只对应文内记录时点。

> 目录说明（2026-09-22）：本文保留原阶段的设计与实现上下文，工具路径可能属于旧布局。
> 当前职责划分、工具清单与开发入口见[能力架构](../../architecture/AGENT_CAPABILITIES.md)、
> [工具目录](../../reference/TOOLS.md)和[文档索引](../../README.md)。

> 2026-09-07。用户拍板：B 线以这套实现为准，不回退；价格按 499/1999 改（已改，`9b298e0`）。
> 本文是对照 `BILLING_PLAN.md` 与 `DETAILED_PLAN_M1_M2.md` B2–B4 的差距清单，供下一轮执行单使用。证据为 `main` 上的文件行号。

## 0. 一句话

支付与账本的工程质量很高（Decimal 全程、余额行锁、支付幂等、原始体验签、中断结算、真 Postgres 并发测试），
但它移植的是 bossip 的商业口径，不是本计划的 B2/B3/B4：面值 1:1 写死、免费档复活、无回合前预检与 developer 豁免、
无网关 token / 影子上限 / 对账、视频图片转写不落账、订阅与桌面不联动、无后台代付。另有两个会算错钱的 bug。

## 1. 已做且达标（保留）
| 项 | 证据 |
|---|---|
| 账本以 workspace 为键；余额行 `with_for_update()`；金额只经 `post_ledger` 变动 | `billing/service.py:36-57` |
| 支付宝 RSA2 验签用原始体、回调幂等、跨单复用拒绝、取消前先查真实支付态 | `billing/alipay.py:164-189`、`payments.py:254-279, 178-245` |
| 五处 LLM 调用点都记账（chat / title / bash_judge / compaction / cron_summary）；中断流用已收到的 usage 结算 | `agent/llm.py:1420-1508, 1470-1483`、`tool/bash.py:89`、`agent/compaction.py:262,406`、`cron/executor.py:335` |
| `litellm.completion_cost` 退役；压缩不再清零累计 | `bfd08a4` diff |
| 无 usage 的调用记 `unreported` 而非 0 元 | `service.py:119` |
| `/api/billing/*` 鉴权：成员校验 + owner/admin + 订单归属；webhook 体 64 KiB 上限；仓库无真实密钥 | `api/billing.py:234-238`、`payments.py:111,145,183` |

## 2. 必须修（钱与可用性）
| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| F1 | **同一自然月内重新订阅拿不到积分**：发放幂等键 `allowance:{ws}:{plan}:{月首日}` 不含订阅 id；2/10 到期、2/15 再买 → 键已存在直接 return，付了钱 0 积分 | `subscriptions.py:52-60` | 键改为 `allowance:{order_id}:{period_start}`（计划原文 `grant:<sub_id>:<period_start>`）；补测试「到期后同月重购」 |
| F2 | **月末下单双份额度**：1/31 开通即发 1 月全额，2/1 又发 2 月全额，订阅 2/28 结束 | `subscriptions.py:76-81` + `ensure_period_allowance` | 首期按订阅起始日发放、周期按订阅周期而非自然月对齐（`period_start = starts_at + n 月`），或首期按天折算；二选一写进执行单 |
| F3 | **流式路径里 `normalize_usage` 抛异常**：provider 返回不自洽 usage（缓存数 > 输入等）会让正常对话中途报错 | `pricing.py:65-66`；调用点 `agent/llm.py:1138,1558,1572` | 计价层保留严格校验；LLM 路径改为宽容取数 + 记 `status=unreported` 事件并打日志 |
| F4 | **免费档复活并自动周发 10 积分**；`BILLING_MODE` 默认 `shadow` 而非 `off`；无 developer 豁免；无存量用户切换脚本 → 一旦切 enforce，所有存量与内部账号被 `INSUFFICIENT_CREDITS` 拦住 | `subscriptions.py:54-60`、`service.py:26`、全仓无 `developer` 判断 | 免费档改为不发放（`credits: 0` 或删档）；默认 `off`；`users.role=developer` 在 `UsageMeter.start` 与预检处豁免；写 `scripts/billing_cutover.py` |
| F5 | `usage_events.idempotency_key` 是自生成 id，无去重能力；表无 `part_id` | `service.py:95`、`db/models/billing.py:20-43` | 键改 `llm:{message_id}:{part_id}`（对应计划「幂等键 = part id」），加列 |
| F6 | `GET /balance`、`GET /subscription` 会写账本并取排他锁，前端每 15 秒轮询 | `api/billing.py:70-71,84-85`、`shared/api/billing.ts:143,195` | 发放只在写路径（用量开始、支付入账、订阅生效、每日巡检）触发；GET 只读 |
| F7 | `PAYMENT_PROVIDERS_JSON` 每次请求重解析并重读 PEM；格式错时公开 webhook 500 | `providers.py:126-138`、`alipay.py:93-94`、`api/billing.py:240` | 启动时解析一次缓存；webhook 捕获配置错误返回 503 并报警 |
| F8 | `billing_subscriptions` 无「同 workspace 至多一条 active」部分唯一索引，允许区间重叠 | `subscriptions.py:38-42` | 加索引或在 `apply_subscription` 里禁止重叠（现在是排队，可接受，但要写明） |

## 3. 商业口径（需按拍板改，部分待定）
| 项 | 现状 | 拍板 / 待定 |
|---|---|---|
| 月价 | ~~599/2100~~ → **499/1999**（已改 `9b298e0`），年付按 12 倍 598800/2398800 分 | 年付倍数待定（bossip 12 倍；可给折扣） |
| 积分面值 | 1 元 = 1 积分，`pricing.py:21` 断言写死 | 计划是每 200 元 100 积分（1 积分 = 2 元）。**面值改动牵一发**：`rates.json` 19 个模型单价、套餐积分数、充值换算全部要同比缩放。建议**保留 1:1**、把套餐积分数改成对应金额的用量（见下），因为 1:1 用户最容易理解——需用户确认 |
| 套餐积分 | pro 280 / max 1680（bossip 数） | 若保留 1:1：专业 499 元含多少积分、旗舰 1999 元含多少，需重新定（bossip 的 280/1680 是按 499/1999 定的，恰好可沿用） |
| 免费档 | free 每周 10 积分 | 计划：退役；体验档 15 天 100 积分只由运营/邀请码发放（grant_only）。**现状不能保留**，见 F4 |
| 充值 | 1 元 = 1 积分，仅付费档可充 | 保留；「仅付费档可充」是套餐进了权限门（铁律一），建议放开 |
| 费率后台覆盖表 `credit_rates` + `GET/PUT /api/admin/rates` | 无，只有 `BILLING_RATES_FILE` 整文件替换 | 里程碑三补 |

## 4. 缺整层（对应计划里的项，需重排）
| 计划项 | 现状 | 去向 |
|---|---|---|
| B2 每 workspace 网关 token + 影子上限（new-api `remain_quota`） | 无，仍用全局共享 key | **B2'**：新执行单，只做 token + 影子上限 + 请求 ID 采集 |
| B3 对账（`/api/log/self`，adjust / gateway_only / unbilled） | 无，`usage_events` 无 `gateway_request_id` | **B3'**：依赖 B2' |
| B2 视频 / 图片 / STT 落账 | 无（文档自述不涵盖） | **B2'**：按 `video_jobs` 终态 × 费率，图片按张，转写按分钟 |
| B4 回合前预检（`SUBSCRIPTION_REQUIRED / SUBSCRIPTION_PAST_DUE / CREDIT_EXHAUSTED`，429 结构，5% 余量） | 无；只有每次调用前 `balance <= 0` 判定，调用后结算可透支 | **B4'** |
| B4 订阅状态机（active/past_due/expired、宽限、每小时巡检） | 无 status 列，靠时间区间 | **B4'** |
| B4 开通套餐 = 分配桌面；到期释放 | 完全无联动（`grep BillingSubscription backend/sandbox` 零命中） | **B4'**：订阅生效 → `pool.assign(workspace)`；expired → `pool.release`。A3 接口已就绪 |
| B4 后台代付 / adjust / refund / 兑换码 + 审计 | 无 admin 路由 | **B4'**（代付先行，测试期要用） |
| 崩溃遗留 `pending` 事件清理 | 无 | 并入 B3' 的 internal_task |

## 5. 建议顺序
1. **修 F1–F5**（一个小会话，含测试），随价格一起发一版。
2. 用户定面值与套餐积分数（§3），改 `plans.json`/`rates.json`，再发一版。
3. B2' → B3'（对账跑一周）→ B4'。enforce 仍只在真实流量影子一周、漂移 < 5% 之后打开（计划 v3 硬规则不变）。

## 6. 给队友的话
不是推倒重来。账本、支付、落账点这三块直接沿用；改的是发放键、免费档、默认模式、豁免、预检与联动，这些都在 `billing/subscriptions.py`、`service.py` 和几处接线上，量不大。
