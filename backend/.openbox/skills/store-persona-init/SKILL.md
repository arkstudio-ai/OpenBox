---
name: store-persona-init
description: Bootstrap a merchant's store persona (店铺人设) right after their 抖音来客 / 美团经营宝 login state is bound — read the store's own back-office pages on the cloud desktop, infer five persona facts, and confirm them with ONE summary card via creator_context propose_bundle. Use when a system task says 完善门店人设 / 自动完善人设, or when a person asks to 生成/重新生成/完善 店铺人设 or 经营定位.
allowed-tools:
  - creator_context
  - desktop_login
  - skill
  - bash
---

# 店铺人设初始化（store-persona-init）

目标：用店主自己后台里已有的信息，替他写出五条「创作者人设」记忆，最后**只出一张汇总卡**让他改完确认。
这是无人值守任务：**执行过程中不提问、不用 `question`、不做任何提交类操作**；唯一的交互是最后 `creator_context(action="propose_bundle")` 弹出的卡。卡的答案由系统在店主点击后接回本会话，你不需要等待。

时间预算 5 分钟。任何一页读不到就跳过那一段，不要重试超过一次，也不要因此中断整个流程。

## 1. 读档案与已有记忆

1. 任务提示里带有 `[门店档案]` JSON：店名、行业、主平台、各平台绑定信息（`account_name` / `account_id` / `shop_id`）。以它为准，不要猜店名。
2. `creator_context(action="get_user_context")`：已经是 ACTIVE 的类型（身份、内容定位、受众画像、招牌案例、表达风格）**不要再提议**，只补空缺；`BOUNDARY` 永远不由你写。

## 2. 读平台后台（只读）

先 `desktop_login(action="status")` 看哪些站点是 `bound`。没有任何站点绑定时跳到第 3 步，只用档案推断（置信度 ≤ 30，证据写「仅档案」）。

有绑定站点时 `skill(skill="dev-browser")` 载入浏览器（云电脑 `local` 模式，带店主登录态），用 `getAISnapshot` 读页面，**不点任何「保存 / 提交 / 发布 / 删除」**：

**抖音来客**（`douyin_laike`）
- 首页 `https://life.douyin.com/p/home`：账号名、经营品类、门店数。
- 左侧导航「门店管理」：门店名、地址、营业时间、人均（有就记）。
- 「商品管理」或「团购」：前 10 个在售商品的名称、价格、销量，标出销量最高的 1–2 个。
- 「评价管理」：最近 30 条评价里被反复夸的点（新鲜、分量、服务、环境…）和被反复抱怨的点，各取前 3。
- 接口兜底：`https://life.douyin.com/life/gate/v1/account/detail`（JSON，`data.account_name`）。

**美团经营宝**（`meituan_merchant`）
- 首页 `https://e.dianping.com/app/merchant-platform`：店名、评分、所属品类。
- 「团购」页：在售套餐名称与价格。
- 「评价」页：同上取被夸/被抱怨的点。

每读到一段就用一两句话记在心里（不要写文件）：来源页面 + 关键事实。

## 3. 推断五类人设

每条 `summary` ≤ 60 字、口语、具体；`evidence` 写来源（例：「来客·评价管理 30 条中 12 条提到新鲜」）；`confidence` 按证据条数给：有 3 条以上证据 70–85，1–2 条 50–60，仅档案 30。

| type | 写什么 | 例 |
|---|---|---|
| `IDENTITY` | 店是谁：店名、品类、城市/商圈、人均 | 南宁·泽岚鲜果，社区鲜果切与果汁，人均 25 |
| `OFFERING` | 卖什么、主推什么、团购占比 | 主推 19.9 双人果切与 9.9 鲜榨券，团购单为主 |
| `AUDIENCE_PROFILE` | 谁在买、什么时段 | 附近上班族与宝妈，午后与晚间为主 |
| `SIGNATURE_CASE` | 评价里被反复夸的点（不写差评） | 果切新鲜、分量足、老板娘热情 |
| `VOICE` | 建议的表达风格（从店主回复评价的语气推断，没有就按品类给保守默认） | 亲切、直接、带本地口语，不用网络梗 |

第 1 步里已 ACTIVE 的类型直接跳过。差评与抱怨点**不要**写进人设，留给运营建议用。

## 4. 出汇总卡

一次调用：

```
creator_context(action="propose_bundle", items=[
  {"type": "IDENTITY", "summary": "…", "evidence": "…", "confidence": 70},
  {"type": "OFFERING", "summary": "…", "evidence": "…", "confidence": 60},
  …
])
```

调用后本轮结束（工具会挂起等待店主）。店主点击后系统会把结果接回：
- 「确认」→ 记忆已保存，回一句话：人设已保存，首页的创作建议会按它来，之后可在运营中心修改。
- 「稍后」→ 回一句话：随时可在运营中心确认，然后停止。
- 回了一段文字 → 当作修改意见，改好后**再调用一次** `propose_bundle`（只这一次）。

## 边界

- 只读；不发布、不改店铺任何设置、不下载文件。
- 不把评价原文整段抄进记忆，只写归纳。
- 读不到后台就用档案推断并如实标低置信度，不编造销量与评价。
