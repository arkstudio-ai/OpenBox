# OpenBox 计费方案

> 状态：设计稿 v1（2026-09-02），待评审后进入实施。<br>
> 参照实现：bossip `apps/center/src/billing/`（套餐目录、用量计量、对账）与
> `admin-v2/model-gateway-admin.service.ts`（new-api 管理 API 封装）。<br>
> 姊妹文档：`docs/WUYING_SANDBOX.md`（per_user 桌面）、`docs/VIDEO_ATOMIZATION_PLAN.md`（视频计费口径来源）。

---

## 现状核对（2026-09-02，回应「openbox 已有内置计费」）

代码里有的是**用量计量与估算**，不是计费：每步 token 统计（messages / sessions.token_usage）、
LiteLLM 价格表估算的费用（网关自定义模型名查不到，估为 0）、设置页「用量与消耗」、
会话/并发/容器配额 429、视频每日提交上限与幂等键、OSS 5 GiB 配额。
没有余额、账本、套餐、发放、充值、支付、到期回收、每用户网关 token；分支与 PR 中也没有。
`users.monthly_cost_limit` 与 `check_monthly_cost` 存在但从未被任何接口调用，是死代码。
本计划复用「用量与消耗」页作为 /billing 的基础，复用 step_finish 的 token 事实数据作为账本输入。

**「用量与消耗」页能否直接当扣款依据：不能。** 它读的是 `GET /api/agent/session` 返回的
`sessions.token_usage` 前端求和，问题有六处：
1. 费用永远是 0：两处 `litellm.completion_cost(model=, prompt_tokens=, completion_tokens=)` 在
   锁定的 litellm 1.81.11 上抛 TypeError（参数名不对），被 except 吞成 0.0。
2. 压缩会把会话累计清零：compaction 结束后 token_usage 的 input/output/total/cost 重置为 0
   （为了上下文进度条），长会话的累计被抹掉。
3. 删除会话即消失：软删后不再进列表，用户删掉对话就能把用量归零。
4. 子 agent 会话不计：task 工具创建的 parent_id 子会话被列表过滤。
5. 不是所有 LLM 调用都记：标题生成、bash 的 LLM 判官、MCP 过滤模型直接调 litellm，不写 usage；
   压缩本身的 token 只写在消息上、不进会话累计。
6. 非 LLM 花费不在里面：视频、图片、ASR、桌面。
可复用的是 `messages.tokens`（每步一条、不会被压缩清零、软删不影响行）作为事实源，以及这个页面的 UI。
M1 的 usage_events 必须在 step_finish 之外把标题/判官/过滤/压缩四处调用一并落账。

---

## 0. 已定决策

> **2026-09-03 更新（覆盖下表第 1、3 条）**：积分面值改为**每 200 元 100 积分**（1 积分 ≈ 2 元），
> 套餐三档：体验（免费赠送，15 天 + 100 积分，运营/邀请码发放）、专业版 499 元/259 积分、旗舰版 1999 元/1000 积分；
> 模型与视频对外定价做成可配置（文件默认 + 后台覆盖表），先填占位。执行细节见 `DETAILED_PLAN_M1_M2.md`。
> 注意 499 按面值应为 249.5 积分，259 是用户给定值，待确认。
> **2026-09-03 晚拍板**：桌面归 workspace，一个订阅一台桌面；网关 token 一 workspace 一个（不再是每用户）。
> 本文 §3 表里 `user_id / user_balances / gateway_tokens(user_id)` 按 workspace 理解；执行细节与审查回写见 `DETAILED_PLAN_M1_M2.md` v2 与 `DETAILED_PLAN_M1_M2_REVIEW.md`。


| # | 决策 | 含义 |
|---|---|---|
| 1 | **套餐制，两档** | 专业版 ¥499/月、旗舰版 ¥1999/月，档位与 bossip 一致。 |
| 2 | **开通套餐 = 开通 ECD** | 云桌面按月随套餐，不按时长计量；没有有效套餐就没有桌面。 |
| 3 | **积分 1 : 1 元** | 套餐内含积分按月发放（专业版 280、旗舰版 1680，沿用 bossip），可额外充值。 |
| 4 | **免费档退役** | 不再有面向用户的免费档。原免费档的定位转为「开发模式」。 |

**开发模式（已确认）**：部署级开关 `BILLING_MODE=off`，加上用户级豁免
`users.role = developer`。开关关闭时账本只记不扣、桌面走 `WUYING_MODE=shared` 或 docker；
生产环境 `BILLING_MODE=enforce`，内部账号靠 developer 角色豁免。它不是一个套餐档位，
前端不展示它。

**免费试用（推迟，见 §9）**：曾计划用 ECD 池给新用户送 10 天、拉新再送 10 天、上限 40 天。
初期难验证且干扰 ECD 管理，本轮不做。初期需要拉人测试时由后台代付开通套餐。

**支付随实施接入**：不再后置到最后一个里程碑，M2 套餐上线时就带支付渠道；测试期用后台代付。

---

## 1. 总体架构：计量下沉网关，账本留在 OpenBox

三条路线的取舍见对话记录，结论是混合方案：

- **new-api 负责硬闸与归因**：每个 OpenBox 用户在 new-api 里有一个专属 token，
  `remain_quota` 是账本余额的影子。agent 跑飞时网关先拒绝，不等账本追上。
  日志按 `token_name` 天然分用户。
- **OpenBox 负责唯一事实源**：套餐、积分账本、用量事件、发放与充值全部在自己的表里。
  云桌面月费、ASR 这类不过网关的成本项只有这里能计。
- **两者之间有两条线**：余额变化后异步推影子上限到 token；每晚拉网关日志与账本对账。

```
用户回合 ──► 回合前预检(套餐有效 + 余额>0) ──► LLM 调用(带用户 token) ──► new-api(硬闸)
                                                       │
                                            step_finish 产生 usage_event
                                                       │
                                            credit_ledger 扣积分 ──► 推 remain_quota
                                                       ▲
套餐开通 / 充值 / 后台调账 ──► credit_ledger 记增 ─────┘
每晚 ◄── /api/log 按 token_name 汇总 ──► 与 usage_events 比对 ──► 漂移报警(不自动补账)
```

**为什么不让 new-api 直接当账本**：它计不了云桌面与直连 DashScope 的 ASR；没有周期发放和
审计式调账；余额展示要靠拉日志；财务事实源会落在一个我们不控制 schema 的 MySQL 里。

**为什么不放弃网关闸**：账本是回合后落账，一次失控循环能在落账前烧掉几十元；token 上限是
零成本的熔断器。

---

## 2. 计费对象与口径

| 对象 | 计费方式 | 计量点 | 幂等键 |
|---|---|---|---|
| 云桌面 | 套餐月费包含，不单独计量 | 套餐状态机 | — |
| LLM 对话 | token × 费率表 | `agent/loop.py` 的 step_finish（已有 input/output/cache 数） | part id |
| 视频生成 | 按秒或按次 × 模型价 | `tool/video_production.py` 提交成功时 | 已有 `idempotency_key` |
| 图片生成 | 按张 | `tool/image_gen.py` 成功返回 | 调用 id |
| 语音转写 | 按分钟 | `video_transcribe` 成功返回 | job id |
| OSS 存储 | 不计费，沿用 5 GiB 配额 | — | — |

价格口径：**费率表里写的是最终售价（积分）**，new-api 的 ModelRatio 只维持成本价。毛利系数
由运营定，不要把毛利藏进网关的 GroupRatio，否则两处都能改价、对不上账。

费率表 `backend/rates.yaml`（结构照 bossip `rates.config.yaml`）：

```yaml
version: 1
credit: { currency: CNY, cnyPerCredit: 1 }
models:
  gpt-5.6-luna:            # 网关成本 0.8 / 2.4 每百万 token（bossip 实测）
    input_per_million: 1.0
    cache_read_per_million: 1.0
    output_per_million: 3.0
video:
  wan3.0-video:
    720p:  { per_second: 0.5 }
    1080p: { per_second: 0.8 }
image:
  default: { per_call: 0.3 }
stt:
  fun-asr: { per_minute: 0.05 }
```

以上数字是占位，上线前按网关实际成本填。`litellm.completion_cost` 的估算退役，
step_finish 的 `cost` 字段改由费率表算出。

---

## 3. 数据模型（新增四张表）

| 表 | 关键列 | 说明 |
|---|---|---|
| `subscriptions` | user_id, plan_id, status, period_start, period_end, source, grace_until | status: `active / past_due / expired / canceled`。source: `direct_grant / redeem / payment`。一个用户同一时刻至多一条 active（部分唯一索引）。 |
| `credit_ledger` | user_id, kind, amount(带符号), balance_after, ref_type, ref_id, idempotency_key(唯一), note | kind: `grant / topup / usage / adjust / refund`。余额 = 最后一条 balance_after；写入必须在事务里对 `user_balances` 行加锁（bossip 铁律：读-改-写包事务）。 |
| `usage_events` | user_id, session_id, part_id, modality, provider, model, unit_type, quantity, input/output/cache_tokens, cost_credits, ledger_id, status, idempotency_key(唯一), raw | modality: `llm / video / image / stt`。status: `charged / shadow / failed`。事实账，永不删。 |
| `gateway_tokens` | user_id(主键), newapi_token_id, token_name, key_ciphertext, synced_quota, synced_at | token key 加密存储，只在后端解密用于 LLM 调用。 |

同时新增 `user_balances(user_id, balance, updated_at)` 作为加锁行与快速读。

**退役**：`users.monthly_cost_limit` 与 `auth/quota.py` 的月度花费检查；
`video_generation.daily_job_limit` 默认改 0（配置项保留，开发模式可开）。

---

## 4. 关键流程

### 4.1 开通套餐

1. 写 `subscriptions`（active，period 按北京时间自然月）。
2. 发放套餐积分：`credit_ledger` 一条 grant，幂等键 `grant:<sub_id>:<period_start>`，
   同周期重复调用不重复发。
3. 确保网关 token 存在（`gateway_tokens` 缺则经管理 API 建），推影子上限。
4. 触发桌面开通：调现有 `wuying_desktop_service.provision`。DesktopTab 的
   `not_provisioned` 状态改成先查套餐：无套餐显示「开通套餐」并跳 `/billing`，
   有套餐才显示「开通云电脑」。

### 4.2 续费与到期

- `period_end` 前续费：追加下一周期，积分在新周期首日发放。
- 到期未续费：`past_due`，桌面**停机不删**（StopDesktops，数据保留），宽限 **7 天**。
- 宽限内续费：桌面 StartDesktops，秒级恢复。
- 宽限后：`expired`，桌面 DeleteDesktops 释放，积分余额冻结但不清零（续费后可继续用）。
- 以上由一个每小时的订阅巡检任务驱动，复用 `wuying_desktop_service` 已有的巡检骨架。

宽限天数与是否清零余额是可调项，先按上面执行。

> **2026-09-02 更新（桌面改包月）**：per_user 桌面改为上海 PrePaid 包月（见
> `docs/RELAY_CUTOVER.md` §5），到期语义随之变成：不续费 = 关自动续费，桌面到期后由
> 阿里云保留约 15 天再释放，这就是宽限期；宽限内续费 = RenewDesktops + 重开自动续费。
> 上面「停机不删」那条对包月桌面不适用。

### 4.3 每个回合

1. **回合前预检**：套餐 active 或 past_due 且余额 > 0；否则拒绝，错误码
   `SUBSCRIPTION_REQUIRED` / `CREDIT_EXHAUSTED`，沿用 `auth/quota.py` 的
   机器可读 429 结构，前端映射到「开通套餐」或「充值」按钮。
2. **LLM 调用带用户 token**：`agent/llm.py` 的 provider kwargs 已支持 `api_key`，
   改成按 session 的 user_id 解出 `gateway_tokens` 里的 key 覆盖。
3. **step_finish 落账**：写 `usage_events` + `credit_ledger` 扣减，幂等键为 part id。
   落账失败进重试队列，不阻塞回合。
4. **视频/图片/ASR** 在各自工具成功点落账，幂等键用工具已有的键。

### 4.4 影子上限同步

每次 `credit_ledger` 写入后，异步把 `remain_quota := balance × QuotaPerUnit` 推到用户 token。
QuotaPerUnit 从网关 `/api/option/` 读取，不写死。影子按成本价折算，天然略宽于账本，
只做熔断，不做记账依据。推送失败重试，连续失败报警。

### 4.5 支付、充值与调账

- 支付走 `PaymentProvider` 接口（bossip `billing/payment/` 的口子），M2 带首个真实渠道上线。
- 后台代付：运营在后台以 `source = direct_grant` 开通套餐，用于测试期拉人，不经支付渠道。
- 兑换码作为补充入口。
- 所有人工调账走 `credit_ledger.kind = adjust`，带操作人与原因，写 `audit_logs`。

### 4.6 对账：计量权威在 new-api，记账权威在 OpenBox（2026-09-02 定稿）

**分工**：网关经手的每一次 LLM / 图片 / 视频调用，token 数、缓存命中、是否中断，以
new-api 的 logs 表为准；OpenBox 自己在 step_finish 记的数只是「先记先扣」的估计。
积分余额、套餐、发放、调账、以及不经网关的花费（包月桌面、直连 ASR），以 OpenBox 账本为准。

**可行性已核实**（bossip-gw-1 实例）：logs 表有 `request_id` / `upstream_request_id`，
响应头 `X-Oneapi-Request-Id` 返回同一个 id，可逐笔精确关联；`other` 里有 `cache_tokens`
（OpenBox 现记为 0，网关能看到 11776 这种真实命中）、`stream_status.end_reason`（可判中断）、
`request_path`；日志自 2026-08-04 起未清理，无自动清理选项。

**机制**：

1. 每次 LLM / 图片 / 视频调用把响应头里的 `X-Oneapi-Request-Id` 存到 `usage_events.gateway_request_id`
   （消息的 tokens 里也带一份）。
2. OpenBox 在 step_finish 立即写 usage_event（status=estimated）并扣积分，保证实时余额与预检可用。
3. 对账任务每 5 分钟拉一次最近 30 分钟、每晚拉整天：`GET /api/log/self` 按 token_name 与时间窗分页。
   - 按 request_id 匹配到的：用网关的 prompt / completion / cache 数重算费用，差额写一条 adjust，
     事件改 status=confirmed。
   - 网关有、OpenBox 没有的（标题生成、判官、过滤、中断、重试）：补一条 usage_event
     （status=gateway_only），按 token_name 归属到用户并扣费。这一条是「权威在 new-api」的落地。
   - OpenBox 有、网关 30 分钟后仍没有的：status=unbilled，不扣费并报警（说明调用没到网关或日志丢了）。
4. 视频例外：网关现在按次记 ¥0.5 占位，费用以 OpenBox 的 video_jobs（时长、分辨率、终态）×
   费率表为准，网关日志只用来核对「提交发生过」。fork 改成按秒计价后再把视频并入 3。
5. 前提：M2 的每用户 token，否则 token_name 全是 openbox-shared，网关侧无法按用户归属，
   对账只能做到平台总量核对。

漂移超阈值（按日 5%）或 unbilled 堆积即报警；所有调整都走 credit_ledger 的 adjust，有据可查。

---

## 5. 网关侧准备（自有 new-api，bossip-gw-1）

> **2026-09-02 决策更新**：网关定为我们自己部署的 new-api（bossip-gw-1 上的 `bossip-newapi`），
> 不再用 api.ueejavelin.org 做 openbox 的入口；ueejavelin 退为它的一个上游渠道。
> 理由：自己的服务器、后续多上游渠道、计费和日志自己管。切换操作单见
> `docs/RELAY_CUTOVER.md`。本节原文保留作历史，"ueejavelin"处按操作单理解。
> 5.1 里「M0 需要拍板的两件事」已定：视频走自有 new-api（全部通道），
> 网关价格按 720P 档每秒成本价过渡，fork 改按秒×分辨率计费。


1. **先修视频按次价格**：现在 wan3 一条 5 秒片预扣 4687 万 quota，只因 token 无限额才没炸。
2. 建平台用户 `openbox-platform`（用户级 quota 无限），所有用户 token 挂它名下，
   不给每个用户建 new-api 账号。
3. 读出 `QuotaPerUnit`、各模型 `ModelRatio / CompletionRatio`，与费率表成本列对齐。
4. 现有共享 token（`openbox-video-gateway` 等）保留给开发模式。
5. 管理 API 调用封装移植 bossip 的 `model-gateway-admin.service.ts`：
   建 token、改 `remain_quota`、拉日志、读 option 四个动作。

### 5.1 M0 调研结果（2026-09-02 实查）

**流量现状**：生产 `/opt/openbox/config/openbox.json` 里 `channel_providers = {"sd2": "bossip"}`，
视频直连 bossip 中转站 `openapi.bossipai.com.cn`，用的是与 bossip-center 共用的 relay token
`center-media-vip`。api.ueejavelin.org 的 new-api 上 ch14（→bossip 中转）/ch15（→百炼直连）
已建但生产没走（只有 8/28 一次测试调用）。

**当前可路由的视频模型**（bossip 中转站 vip 组 abilities 表 + 生产 openbox.json 声明）：

| 模型 | 中转站渠道 → 上游 | 规格 | openbox 已声明 |
|---|---|---|---|
| wan3.0-video | ch123 → 百炼 wan3 适配器 | 480/720/1080P，2–30s，带音频 | ✅ |
| wan3.0-video-prime | ch123 | 同上，加速版 | ✅ |
| doubao-seedance-2-0-260128 | ch120 tokenspace(P20) / ch106 火山官方 | 720p/1080p，4–15s | ✅ |
| doubao-seedance-2-0-fast-260128 | ch120 / ch106 | 480p/720p，4–15s | ✅ |
| seedance-2.0-480-fastⅠ | ch113 tokenspace（上游同 seedance-2.0） | 480p，无音频 | ✅ |
| video-sd-720p-proⅠ | ch113 | 720p | ✅ |
| video-sd-1080p-pro | ch113 | 1080p；有 484 条上游报错记录 | ✅ |
| MiniMax-H3 | ch114 → metaso | 480/512/768p/2K，4–15s | ✅ |
| doubao-seedance-1-5-pro-251215 | ch106 | — | ❌ 未声明 |

**中转站现在怎么向我们记账**（不是真实成本，只是 bossip 实例的占位价）：

| 路径 | 记账 | 证据（logs.other） |
|---|---|---|
| wan3 / seedance（ark、/v1/video/generations） | 固定 ¥0.5 / 次，与时长分辨率无关 | model_price 0.5，quota 250000 |
| sd2 三模型（/v1/videos，tokenspace 适配器） | ¥0.5 / 秒 | 4s→1.0M、5s→1.25M、12s→3.0M quota |
| MiniMax-H3 | 按 token，ratio 0.1 | actual_quota 350000–750000 |
| ueejavelin new-api 上的 wan3 | **未配价**，回落默认倍率 37.5 | 5s 一条扣 46.875M quota ≈ $93.75 |

**上游官方刊例价**（成本基准，人民币/秒，2026-09-02 查得）：

| 模型 | 480P | 720P | 1080P / 2K | 备注 |
|---|---|---|---|---|
| wan3.0-video（百炼） | 0.30 | 0.60 | 1.20 | 8/24–9/23 限时 7 折；另有来源称 0.15/0.30/0.60，需在百炼控制台核对 |
| wan3.0-video-prime（百炼） | ≈0.45 | ≈0.90 | ≈1.80 | 官方英文文档 USD 0.0636/0.1272/0.2544 折算 |
| doubao-seedance-2.0（火山） | ≈0.45 | ≈0.95 | ≈2.25 | 无视频输入 ¥46/百万 token，token = W×H×(24s+1)/1024 |
| doubao-seedance-2.0-fast | 未查到 | 未查到 | — | mini 版为 ¥23/百万 token，fast 待火山控制台确认 |
| sd2 三模型（tokenspace 转售） | 未知 | 未知 | 未知 | 中转站按 ¥0.5/秒 记，疑为转售价透传，待确认 |
| MiniMax-H3（官方） | 0.33 | 0.50（768P） | 0.80（2K） | 参考图超 5 张每张 ¥0.2 |

**M0 需要拍板的两件事**：

1. **视频走哪条网关**。要让每用户 token 硬闸覆盖视频，生产的 `channel_providers` 必须从
   `bossip` 改回 `newapi`（ueejavelin），wan3 走 ch15 直连百炼（自己的 DashScope key，成本
   即官方价），其余走 ch14 转 bossip 中转。
2. **网关价格的粒度**。new-api 的 ModelPrice 每模型一个数，表达不了分辨率档；网关只做
   影子上限，建议按 720P 档配每秒价（USD 单位，ueejavelin 实例 QuotaPerUnit 是按美元），
   精确的分辨率×时长定价放 openbox 费率表。配完后用一条 2 秒 480P 的 wan3 实测网关
   是否按秒×ModelPrice 扣费。

ueejavelin 实例是公司共用网关（tony、员工、mac-fleet 等用户都在），不要改 QuotaPerUnit
之类全局项；只加 ModelPrice 条目和 openbox 自己的 token。

---

## 6. 前端与移动端

- 新增 `/billing`：两张套餐卡、当前余额、本月用量明细（按 modality）、兑换码输入。
- DesktopTab：无套餐时 CTA 改为「开通套餐」。
- 回合前错误码的两条文案与按钮。
- ContextPanel 的花费显示改读 `usage_events`。
- 移动端按 1:1 移植惯例跟进（locale 文件逐字节复制）。

---

## 7. 实施顺序

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 网关准备 | §5 全部 | 视频价格生效；管理 API 四个动作在 gw-1 跑通 |
| M1 影子记账 | 费率表、四张表、step_finish/视频/图片/ASR 落账，`BILLING_MODE=shadow` 只记不扣 | 跑一周，对账脚本能对上网关日志 |
| M2 套餐与硬闸 | subscriptions、开通=开桌面、到期巡检、回合前预检、每用户 token、影子上限、**支付渠道首个实现 + 后台代付** | 新用户真实付款走完开通→使用→到期停机→续费恢复；运营能代付开通测试用户 |
| M3 运营面 | 兑换码、后台 adjust、每晚对账、报警 | 人工调账有审计记录 |
| M4 免费试用（推广前） | §9 的 ECD 池 + 赠天 + 拉新 | 测试稳定、进入推广阶段前再启动 |

M1 与 M2 之间必须留观察期：账本先在 shadow 模式下和网关日志对上，再打开扣减。

---

## 8. 未决与风险

- **旗舰版差异**：2026-09-02 已拍板 Workspace 进第一版，旗舰版的团队席位按 workspace 落，
  本条不再是风险（见 GAP_ANALYSIS.md 第五节）。
- **存量用户**：现有生产用户（含 ecd-demo）需要一次性给 developer 角色或手动 grant，
  否则 M2 上线即被拒绝。
- **token 覆盖的并发**：同一用户多会话并发时共用一个 token，网关 pre-consume
  可能让最后一个回合误判余额不足；回合前预检留 5% 余量可缓解。
- **网关日志保留期**：对账依赖 `/api/log/`，需确认 gw-1 的日志保留策略不短于 7 天。

---

## 9. 免费试用（推迟，不在本轮实施）

原计划：

- **ECD 池**：预开一批桌面放池子里，新用户领试用时从池子分配，不走按用户实时创建。
- **赠天**：新用户送 10 天；每拉新一人再送 10 天；累计上限 40 天。
- **到期**：试用到期未付费，桌面回池（重置或重建），积分按试用档清零。

推迟原因：初期用户量小，这套机制验证不了效果；池子的预开、回收、重置会和 per_user
桌面的标签归属与巡检互相干扰，增加 ECD 管理面。

启动条件：M2、M3 稳定，测试期结束，进入推广阶段前。届时需要补的东西：

| 项 | 说明 |
|---|---|
| `subscriptions.source = trial` | 试用作为一种订阅来源，复用到期状态机，宽限天数为 0 |
| `referrals` 表 | 邀请码、被邀请人、奖励是否已发，幂等键 `referral:<inviter>:<invitee>` |
| 池子管理 | `cloud_desktops` 增加 `pool_state`（idle / assigned / resetting），巡检任务负责补池与回收 |
| 赠天上限 | 40 天在发放点强校验，超出的拉新只记录不发放 |
| 反滥用 | 同设备/同支付账号限一次试用；bossip 的 `Referral*` 表可作参考 |

初期替代：运营后台代付开通套餐（§4.5），效果等价于送一个月，且不引入池子。
