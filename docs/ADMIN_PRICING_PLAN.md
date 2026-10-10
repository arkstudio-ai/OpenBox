# 管理员定价控制页（成本 × 售价）方案

状态：v1 已拍板（2026-10-10）：① 同 PR 直接把 Seedance Fast / 2.0 1080p 售价抬到成本线；② 低于成本允许但需勾选确认；③ 30 秒内生效可接受；④ 页面做成独立的「定价」板块，不放计费 tab 下。摸底数据见 `work/model-channel-survey-20261010.md`（§5 为成本口径）。

## 0. 一句话

把 `backend/billing/rates.json` 从「唯一且只能发版改的价目表」变成「基础价目 + 数据库覆盖」，管理员在后台一张表里看到每个计费项的**成本、售价、毛利、近 30 天用量**，改售价立即生效、有审计有历史；所有现有计费入口（LLM 计量、视频/图片/转写/语音/合成/热点结算、选择器价格展示）**代码路径不变**，只是读到的价目变了。

## 1. 现状与问题

| 现状 | 问题 |
|---|---|
| 价目只有 `rates.json`，随镜像打包；`catalogue()` 每次报价重新读文件 | 改价 = 发版，或在机器上挂 `BILLING_RATES_FILE` 手改 JSON |
| 售价 = 上游刊例成本价，没有毛利字段 | 实际成本与刊例脱节：gemini 走账号池、H3 走秘塔 0.09/s、Seedance Fast 按 ¥23/M 误算 |
| 自有 new-api 的 ModelPrice/ModelRatio 是内部占位（视频 0.5/次） | 容易被当成成本看 |
| `usage_events.pricing` 只存售价快照 | 算不出毛利 |
| 配置里声明了但 rates.json 没价的模型，enforce 下直接拒绝 | 没有地方能看到「哪些在用的模型没定价」 |
| gemini 借 3.7-flash 别名的优惠价 12-31 到期 | 到期当天全站聊天被拒，没人提醒 |

## 2. 目标 / 非目标

**目标**
1. 管理员页面 `/app/admin/billing/pricing`：所有计费项一张表，含成本、售价、毛利率、30 天用量/收入/成本、状态标记。
2. 售价可在页面修改，带原因、乐观锁、幂等、审计、历史；改完下一次报价即生效（≤ 60 秒）。
3. 成本口径入库：每个计费项记录成本单价、成本来源（渠道/供应商/页面/核价日期）。
4. 每条 `usage_events` 同时留下成本快照，毛利可按模型/天汇总。
5. 低于成本、未定价、优惠将到期、30 天零使用 四类状态在页面标出。

**非目标（本轮不做）**
- 套餐价格（`plans.json`）、桌面/ECD 成本、支付渠道。
- 自有 new-api 的倍率同步（它只是内部账，不动）。
- 记忆嵌入/重排/JEV、语音 handover/router 旁路调用的计量接入（另开任务，见 §9）。
- 按用户/工作区差异化定价、促销码。

## 3. 计费项与键

一个「计费项」= 一个可独立定价的最小单位，键与 `usage_events.model_id` / rates.json 的层级一一对应，这样历史用量能直接按键聚合：

| 模态 | 键格式 | 例子 | 单位 | 售价字段 |
|---|---|---|---|---|
| 对话 LLM | `llm:<model>` | `llm:gemini-3.8-flash` | 每百万 token | input / output / cache_read / cache_write / cache_write_1h / cache_read_explicit |
| 视频生成 | `video-gen:<model>:<res>` | `video-gen:wan3.0-video:720p` | 每秒 | per_second |
| 图片生成 | `image-gen:<model>` | `image-gen:gpt-image-2` | 每张 | per_image |
| 语音转写 | `stt:<model>` | `stt:fun-asr` | 每分钟（不足 1 分钟按 1） | per_minute, min_minutes |
| 语音通话 | `voice-realtime:<model>` | `voice-realtime:qwen3.8-omni-flash-realtime` | 每百万 token × 四模态 | per_million.{input_text,input_audio,output_text,output_audio} |
| 视频合成 | `ims-compose:<tier>` | `ims-compose:720p` | 每分钟 | per_minute |
| 热点采集 | `hot-trends:<source>` | `hot-trends:douyin_public` | 每次 | per_fetch |

计费项的**全集**在运行时由两部分并出来：rates.json 里有的 ∪ 配置里声明的（`models`、`video_generation.models × resolutions`、`image_generation.model`、`video_transcription.model`、`voice.model` 及其备选、`ims-compose` 五档、热点源）。配置里有而 rates 没价的项以「未定价」出现在表里，这是目前最容易踩的坑的可视化。

LLM 的 `aliases`（gemini-3.8-flash → 3.7）在表里显示为「别名 → 实际价」，并允许管理员直接给别名键定独立价（定了之后别名失效）。

## 4. 数据模型

### 4.1 rates.json 增加 `cost` 字段（基础成本，随仓库版本管理）

每个条目旁边加一个 `cost` 对象，与售价字段同形，但多了来源信息。示例：

```jsonc
"gemini-3.7-flash": {
  "vendor": "google", "currency": "USD", "input": "0.75", "output": "3.75", "cache_read": "0.075",
  "valid_until": "2026-12-31",
  "cost": {                                  // 新增
    "basis": "rovinai",                      // 成本口径：按哪家算
    "channel": "own-newapi ch116 → UEE → CPA Antigravity",   // 当前实际走向（展示用）
    "currency": "CNY", "input": "6", "output": "30", "cache_read": "0.7",
    "source": "RovinAI 模型广场充值折算价", "verified_at": "2026-10-10"
  }
},
"video-gen": { "MiniMax-H3": {
  "vendor": "minimax", "currency": "CNY", "per_second": {"512p":"0.33","768p":"0.50","2k":"0.80"},
  "cost": {
    "basis": "metaso", "channel": "own-newapi ch114 → metaso.cn",
    "currency": "CNY", "per_second": {"480p":"0.06","512p":"0.06","768p":"0.09","2k":"0.15"},
    "duration_bands": [{"above_seconds": 15, "multiplier": "2"}],   // 16–30 秒翻倍
    "source": "秘塔定价页", "verified_at": "2026-10-10"
  }
}}
```

首批 `cost` 数据直接用摸底 §5 的口径填：gemini=RovinAI、Qwen=百炼、Wan=百炼刊例、H3=秘塔、Turbo=RunningHub 实测、Seedance=TokenHub（火山刊例）、gpt-image-2/fun-asr/IMS/语音=各自刊例。同一个 PR 顺手修两处明确错价：Seedance 2.0 Fast 售价 0.23/0.48 → 不低于成本 0.36/0.80；Seedance 2.0 1080p 2.25 → 2.48。

### 4.2 新表 `pricing_rules`（管理员覆盖，追加写）

| 列 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | `pricing_…` ULID |
| key | String(160) | §3 的键 |
| revision | Integer | 同 key 递增；当前生效 = 最大 revision 且 `superseded_at IS NULL` |
| sale | JSON | 售价片段，形状与 rates.json 该条目一致（只含价格字段，不含 vendor/source） |
| cost | JSON NULL | 成本片段；NULL = 沿用 rates.json 的 `cost` |
| status | String(16) | `active` / `disabled`（disabled = 该项 unpriced，enforce 下拒绝） |
| valid_from / valid_until | timestamptz NULL | 可选生效窗口；到期后自动回落到上一条或 rates.json |
| reason | Text | 必填 |
| actor_user_id | String(64) | 操作者 |
| audit_id | String(64) | 对应 `audit_log` 行 |
| created_at / superseded_at | timestamptz | |

唯一索引 `(key, revision)`，部分索引 `(key) WHERE superseded_at IS NULL`。历史就是这张表本身，不另建 history 表。

### 4.3 `usage_events` 增加成本快照

- 新列 `cost_credits NUMERIC(28,12) NULL`（NULL = 当时没有成本口径，不是 0）。
- `pricing` JSON 里加 `cost` 子对象（单价、basis、verified_at、规则 id）和 `rule_id`（命中的 pricing_rules.id，没命中为 null）。
- 历史行不回填（成本口径是今天才定的，回填会制造假数据）；报表对 NULL 单列「无成本口径」。

迁移一条：建表 + 加列 + 索引，revision 接在 `pc06b7c8d9e0` 之后。

## 5. 价目合成（`catalogue()` 的新实现）

```
catalogue() = overlay(rates.json 基础, 当前生效的 pricing_rules)
```

- 输出形状与现在**完全一致**，13 个调用点（`service.py`、`media.py`×6、`metadata.py`、`backfill.py`、`voice/meter.py`、`pricing.py`）零改动。合成后 `version` 变为 `"<rates.json version>+db<最大 rule id 前 8 位>"`，快照里能看出命中了覆盖。
- `catalogue()` 保持同步函数：进程内持有 `_overlay` 缓存（规则列表 + 加载时间），由 ① 启动时加载 ② 管理员写入后同进程立即刷新 ③ 后台任务每 30 秒刷新 三条路径维护；读文件那部分也缓存到 mtime 变化。多副本/多进程最坏 30 秒内一致，文档写明。
- 覆盖规则按 key 精确替换价格字段；`disabled` 把该条从 `models` / `media.*` 里摘掉并写进 `unpriced[key]`，复用现有 unpriced 语义。
- `valid_until` 到期的覆盖在合成时被跳过，自然回落。
- 校验在写入时做：Decimal 字符串、非负、有限；视频键的分辨率必须是配置声明过的；LLM 键的字段集合必须是 rates 认识的六个之一。

## 6. 成本计算与毛利

- `quote()` / `quote_*()` 返回的 snapshot 增加 `cost_per_unit` 与 `cost_credits`（用同一份 usage 计数乘成本单价，USD 成本用 rates 的 `usd_cny` 换算；H3 的 `duration_bands` 在 `quote_generation` 里按请求秒数套乘数）。`UsageMeter.finish` / `media.settle` 把 `cost_credits` 落列。
- 毛利率 = (售价 − 成本) / 成本，页面按行显示；30 天汇总 = Σcredits − Σcost_credits（只算 status=charged 的行，shadow 单列）。
- gemini 这种实际边际成本≈0 但口径按 RovinAI 的，页面「渠道」列显示实际走向，「成本」列显示口径，并在 basis 上打 tooltip 说明，避免两种数字打架。

## 7. 管理 API（`/api/admin/pricing`，沿用 `require_admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 全表：每项 {key, modality, model, unit, label, channel, base_sale, sale, cost, margin, flags[], rule{id,revision,reason,actor,at,valid_until}, usage30d{events,quantity,credits,cost_credits}}。flags ∈ below_cost / unpriced / expiring(≤30 天) / unused_30d / alias / overridden |
| GET | `/{key}/history` | 该键全部 revision + 对应审计 |
| PUT | `/{key}` | body {request_key, reason, expected_revision, sale?, cost?, status?, valid_until?, allow_below_cost?}。售价低于成本且没带 allow_below_cost → 422 `BELOW_COST`；revision 不符 → 409；request_key 重放 → 返回上次结果 `replayed:true`（复用 `billing/admin.py` 的 AuditLog 幂等套路） |
| POST | `/{key}/revert` | 撤销当前覆盖，回到 rates.json |
| POST | `/preview` | body {key, usage}（如 {input:100000, output:5000} 或 {seconds:10}）→ 当前价与草稿价各算一次，页面改价前预览 |
| GET | `/export` | 下载合成后的完整价目 JSON（备份 / 换环境导入） |

所有写入 `audit.record(..., "admin.pricing.update", "pricing_rule", key, {before, after, reason})`。GET 不记审计（和订阅/订单页不同：这页会被频繁刷新，刷出一堆 view 记录没有意义）。

## 8. 页面

独立板块：`ADMIN_SECTIONS` 加 `pricing`，路径 `/app/admin/pricing`，新 feature 目录 `features/admin-pricing/`（api / components / types），复用 admin-billing 的 `SectionCard` / `FilterSelect` / `SearchField` / `useDraft`。后端路由同样独立成 `api/admin_pricing.py`，前缀 `/api/admin/pricing`。

- 顶部摘要卡：近 30 天 收入 / 成本 / 毛利 / 毛利率；标红项数量（低于成本、未定价、将到期）。
- 主表按模态分组（对话 → 视频 → 图片 → 转写 → 语音 → 合成 → 热点），列：模型、渠道/成本口径、单位、成本、售价（可点开编辑）、毛利率、30 天用量、30 天收入、状态。LLM 行展开显示 input / output / cache 三列；视频行一行一个分辨率。
- 行内编辑抽屉：售价字段、原因（必填）、可选生效截止、预览（输入一个样例用量，看改前改后积分），低于成本需勾选确认。保存走 PUT，成功后刷新全表。
- 历史抽屉：revision 列表、谁改的、原因、前后值；「回到基础价」按钮。
- 筛选：模态、只看标红、只看有覆盖、关键字。
- 文案走 `locales/*/admin-billing.json` 新增 `pricing.*`；移动端不做（管理台本来就是桌面）。

## 9. 实施顺序

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 | 本地 main 同步到 origin/main；在临时 worktree 开分支 `feat/admin-pricing` | — |
| M1 后端价目 | rates.json 加 `cost`（首批口径）+ 修 Seedance Fast/2.0 1080p 售价；`pricing_rules` 表与迁移；`catalogue()` 叠加与缓存；snapshot 加成本；`usage_events.cost_credits` | 单测：叠加/回落/disabled/到期；现有 `test_billing*.py` 全绿；gw2 影子对比 100 条新事件 cost_credits 非空 |
| M2 管理 API | §7 六个接口 + 幂等/乐观锁/审计 + 用量聚合 | `test_admin_pricing_api.py`；postgres 集成测试一条 |
| M3 页面 | §8 | 浏览器验收：改一条 H3 售价 → `/api/agent/config` 的档位价随之变化 → 新视频任务按新价扣 |
| M4 报表与提醒 | 按模型/天的收入-成本曲线；到期/低于成本的管理员站内信（复用消息中心） | 到期前 30 天收到提醒 |
| 后续 | 记忆嵌入/重排/JEV、语音旁路调用接入计量；批量按毛利率一键定价；按工作区差异价 | 另立方案 |

M1+M2 约 2 个工作日，M3 约 2 个工作日，M4 1 天。M1 合并即可单独发布：没有任何覆盖行时行为与今天完全一致，只是多了成本快照。

## 10. 风险与对策

- **多进程缓存不一致**：最坏 30 秒；写接口返回里带 `effective_within_seconds: 30`，页面提示。
- **把成本口径当真实账单**：页面明确写「成本口径」与「实际渠道」两列，并在导出 JSON 里带 `basis`。
- **enforce 下误把模型 disable**：PUT status=disabled 要求二次确认字段 `confirm_disable: true`，且页面显示该项 30 天用量。
- **gemini 别名到期**：M1 起 `expiring` 标记 + M4 站内信；临时手段仍是在页面给 `llm:gemini-3.8-flash` 定独立价。
- **本地 main 落后 origin/main**（语音、记忆、Seedance 2.5 都在远端）：M0 必须先同步，否则 rates.json 会冲突。
- **主工作树与 Codex 共用**：全程在临时 worktree 开发，见 `docs/DEVLOG.md` 的既有约定。

## 11. 验收清单（上线前）

1. 不建任何覆盖行，跑一遍对话 / 视频 / 图片 / 转写 / 语音，扣费与上线前逐条相等。
2. 页面改 `video-gen:MiniMax-H3:768p` 售价，30 秒内 `/api/agent/config` 的灵活档价变化，新任务 `usage_events.pricing.rule_id` 命中。
3. 页面把售价改到低于成本：不带确认 422；带确认成功且行标红。
4. 撤销覆盖后价目回到 rates.json，快照 `version` 不再带 `+db`。
5. 导出 JSON 能被 `BILLING_RATES_FILE` 直接加载（形状兼容）。
6. 审计表能查到每次改价的前后值与原因。
