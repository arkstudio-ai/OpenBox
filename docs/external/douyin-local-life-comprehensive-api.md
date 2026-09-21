# 抖音生活服务开放平台 · 到店综合（到综）API 清单

> 2026-09-21 整理。来源：developer.open-douyin.com 的 local-life OpenAPI 文档树（sitemap + 侧边栏节点 + 文档站 loader 接口的 openApiMeta）。
> 背景：车曼（汽车服务类）在九数云走不通。本文回答「抖音侧到底有没有非餐饮的接口」。
> **结论：有，而且很完整。卡点在九数云没实现，不在抖音。**
>
> 文档 URL 前缀统一记为
> `DOC = https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/`

---

## 0. 一句话结论

| 问题 | 答案 |
|---|---|
| 抖音有没有到综的订单/账单接口 | **有**。到综团购解决方案 `solution_key=16` |
| 到综账单接口和餐饮的关系 | **同一个 Scope `life.capacity.billing.detail`、同一套入参**，只是路径从 `catering_query` 换成 `composite_query` |
| 到综订单查询 | 直接复用通用接口 `/goodlife/v1/trade/order/query/` |
| 到综有没有评价查询 | **没有**。全树只有餐饮评价 |
| 汽车/丽人有没有专属方案 | 有，「交易上翻解决方案」，一整套 `life.capacity.homed.*` 买单收银接口 |
| 我们实测的 `solution_key=1` | 是**旧的到店餐饮解决方案**，官方已标「即将下线」 |

---

## 1. 解决方案全景

文档顶层「API接口」按行业分组：`general-capabilities`（通用）、`catering`（餐饮）、`comprehensive`（综合/到综）、`JiuLv`（酒旅）、`airline`（大交通）。

「到店综合」官方叫**到综**，目录 `comprehensive/`，下辖 6 个解决方案：

| 解决方案 | 目录 | 说明 |
|---|---|---|
| **到综团购解决方案** | `comprehensive/in-store-industry` | 主力，对标我们已跑通的到店餐饮团购 |
| 到综团购预约解决方案 | `comprehensive/group-buy-reservation` | KTV/接待单元，带时段库存 |
| 综合到店提货解决方案 | `comprehensive/store-pickup` | 提货券 |
| 线索管理解决方案 | `comprehensive/lead-mgmt` | 客资/线索 |
| **丽人/汽车/家居家电交易上翻解决方案** | `comprehensive/transaction-upscaling-solution` | 买单收银形态，不是团购券 |
| 零售即送解决方案 | `comprehensive/retail-instant-delivery-solution` | |

官方公告 [解决方案场景化拆分](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/solution/solution-split-notice)：原「到综行业解决方案」拆成到综团购/阶梯次卡/券转赠/组合券包/会员 5 个；原「到店餐饮解决方案」拆成 6 个。

---

## 2. 到综团购（`solution_key=16`）的接口

**关键结构差异**：到综团购**自己只有 4 个定制接口（全是对账）**，其余全部复用通用接口。餐饮则在 `catering/` 下复制了一整套专用路径。

### 2.1 到综专属 · KA 核销对账

| 中文名 | Scope | HTTP | 文档 |
|---|---|---|---|
| **综合账单详情查询** | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/composite_query/` | `DOC comprehensive/in-store-industry/ka-reconciliation/composite_query` |
| 综合账单详情查询（连锁） | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/composite_query_new/` | `.../bill-composite-query-new` |
| 综合提现记录查询 | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/withdraw/composite_query/` | `.../withdraw_composite_query` |
| 综合提现记录（连锁） | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/withdraw/composite_query_new/` | `.../withdraw-composite-query-new` |

> 文档明写「不同行业返回参数不同，其他行业商家请勿查询此接口」。餐饮版是 `/goodlife/v1/settle/bill/catering_query/`。

### 2.2 到综团购复用的通用接口

| 能力 | 中文名 | Scope | HTTP |
|---|---|---|---|
| 订单查询 | 订单查询 | `life.capacity.order.query` | GET `/goodlife/v1/trade/order/query/` |
| 门店 | 查询门店信息 | `life.capacity.shop` | GET `/goodlife/v1/shop/poi/query/` |
| 核销 | 验券准备 | `life.capacity.fulfilment` | GET `/goodlife/v1/fulfilment/certificate/prepare/` |
| 对账 | 验券历史查询 | `life.capacity.billing` | GET `/goodlife/v1/fulfilment/certificate/verify_record/query/` |
| 会员 | 会员数据更新 | `life.capacity.member` | POST `/goodlife/v1/member/user/update/` |
| 商品 | 创建/更新商品 | `life.capacity.goods.found` | POST `/goodlife/v1/goods/product/save/` |
| 退款 | 退款单列表查询 | `life.capacity.catering_after_sale_order` | GET `/goodlife/v1/akte/after_sale/order/query/` |

> 注意：`life.capacity.catering_after_sale_order` 名字带 catering，但**位于通用目录、到综同样使用**。

### 2.3 到综没有的：评价查询

全树只有餐饮评价（`life.capacity.catering.comment`，`/goodlife/v1/akte/comment/query/`）。`comprehensive/` 下无任何评价接口。**这是确认的空白，不是漏查**。

### 2.4 丽人/汽车/家居家电 · 交易上翻解决方案

到综里**唯一按汽车/丽人命名**的方案，全部 Scope 前缀 `life.capacity.homed.*`，是**买单收银**形态。

买单收银（`payment-ability`）：
- 发起收银 `...homed.trade.payment.order.create`
- 买单订单列表查询 `...homed.trade.payment.order.query`
- 买单订单详情查询 `...homed.trade.payment.order.detail.query`
- 买单订单列表（含加密实名）`...homed.trade.payment.order.user_info.query`
- 买单门店列表查询 `...homed.poi.payment.query`
- 售后订单列表/详情 `...homed.trade.aftersale.order.querylist` / `.query`
- 商家发起退款 / 审核用户退款
- Webhook：订单支付成功 / 取消 / 用户申请退款 / 售后审核结果 / 退款成功

国补报销（`gov-subsidy-ability`）、上翻激励归因（`incentive-attribution-ability`：订单归因 `...homed.trade.ordersource.query`、线索归因 `...homed.trade.cluesource.query`）。

---

## 3. 通用能力目录（跨类目可用）

侧边栏「通用接口」共 14 组，到综与餐饮共用：

订单查询、团购核销（`life.capacity.fulfilment` 6 个）、团购对账（`life.capacity.billing` 4 个）、KA核销对账、服务费返还对账、团购退款、三方码、商品发布、商品查询、门店相关、会员接入、招商入驻、职人信息、生服免授权通用能力（加解密）。

**易踩的坑**：同一 Scope 在两个目录下有不同 HTTP 路径。`life.capacity.order.query` 既有通用的 `/goodlife/v1/trade/order/query/`，也有餐饮专属的 `/goodlife/v1/akte/order/query/`。**到综要用通用的那条**。

---

## 4. 关键字段与限制

### 4.1 订单查询 `/goodlife/v1/trade/order/query/`

主要字段：`order_id`、`sku_id`/`sku_name`、`order_status`、`order_type`（21团购/31预定/51,70次卡/60券交易/100预约品/110买单）、`original_amount`、`pay_amount`、`receipt_amount`、`discount_amount`、`create_order_time`/`pay_time`/`update_order_time`、`poi_id`、`merchant_info`、`open_id`；成交归因 `order_sale_info`；券维度 `certificate[]`。

**到综必须注意**：`amount_info` **仅餐饮使用**，其他行业（含到综）要读 `sub_order_amount_infos`（含 `sub_order_type`：1配送费/2打包费/3服务费/100商品费用）。

限制：`page_num × page_size ≤ 10000`，超出用 `cursor`；`page_size` 1~100；默认 20 QPS；不支持酒店订单；不返回券码明文；**不返回核销时间**（核销时间要从验券历史或账单接口取）。

### 4.2 综合账单详情查询 `composite_query`

入参：`account_id`（必填，来客商户根账户ID）、`bill_date`（必填，**按天查**）、`cursor`、`size`(1~50)、`biz_type`（团购1/买单3/点单5）、`root_account_id`（传了才查总户+门店户全量）、`withdraw_id`。

返回 `ledger_records[]` 关键字段：
- 标识：`ledger_id`、`bill_fund_id`、`certificate_id`、`code`（核销券码）、`verify_id`、`shop_order_id`、`item_order_id`、`sku_id`、`group_name`
- 门店：`account_id`、`poi_id`（核销门店）、`settle_account_poi_id`（收款门店）
- 时间：`settle_time`（正向=核销完成时间）、`settlement_time`（结算到货款户时间）
- 类型：`settle_type`（1正向/2退款单/3退款手续费/4欠款/5发票补偿/6逾期划转）、`settle_sub_type`、`fund_amount`/`fund_amount_type`(0正向/1逆向)
- **`amount` 是 map，单位分**：`pay`（用户实付）、`original`（交易金额）、`ledger_total`（**应结算金额**）、`goods`（**商家实际结算金额**，来客导出里叫「提现金额」）、`platform_ticket`（平台补贴）、`merchant_ticket`（商家补贴）、`total_commission`、`talent_commission`（达人佣金）、`total_agent_merchant`（服务商分佣）、`total_merchant_platform_service`（软件服务费）等

⚠️ 文档明写：`platform_ticket` / `ledger_platform_ticket` / `total_agent_merchant` / `total_merchant_platform_service` / `total_operation_agent` / `talent_commission` / `broker_commission` / `crafts_man_commission` **2024-03-31 后不再对服务商展示**。

其他：金额均为绝对值，方向看 `settle_type`；核销与撤销各一条；QPS 50。

### 4.3 数据可回溯范围

逐页查过，**文档里明确写出天数上限的只有两个**：

| 接口 | 限制 |
|---|---|
| 评价查询 `life.capacity.catering.comment` | 最多查最近 **90 天** |
| 线索查询 `life.crm.clue.query` | 跨度上限 **365 天**，最早 2022-09-01 |

订单查询、退款单列表、账单对账、验券历史**均未声明可回溯天数**。九数云文档里「退款单只能查 90 天」在抖音官方文档中**无依据**，按实测口径走。

账单类的真实约束是形态：`composite_query` / `catering_query` 必须**按 `bill_date` 逐日拉**；按券码查的分账单接口**要核销 1 小时后才生成**。

---

## 5. 服务商接入：solution_key 与 permission_keys

授权 URL：`https://auth.dylk.com/auth-isv/`，参数 `client_key / timestamp / solution_key / permission_keys / charset / sign`（SignV2 = SHA256(secret + 排序后 &k=v)），有效期 24h。
出处：`DOC general-capabilities/life.capacity.shop/auth_with_bind`

### solution_key 全量

| key | 解决方案 | 备注 |
|---|---|---|
| **1** | 到店餐饮解决方案 | **即将下线** ← 九数云现在用的就是这个 |
| **4** | 到综行业解决方案 | **即将下线** |
| 5 | 随心团 | |
| 7 | 餐饮在线点单 | |
| 8 / 9 | 酒店新预售券 / 日历房 | |
| 10 / 11 | 景区日历票 / 景区团购 | |
| 14 / 15 | 度假预售券 / 日历品 | |
| **16** | **到综团购解决方案** | 到综新主力 |
| 17~20 | 到综 阶梯次卡 / 券转赠 / 组合券包 / 会员 | |
| 21~26 | 到店餐饮 团购/组合券包/券转赠/阶梯次卡/在线订座/会员 | 餐饮拆分后 |
| 27 | 买单解决方案 | |

⚠️ 授权 URL 参数表只列了旧的 9 个（1/4/5/8/9/10/11/14/15），附录「各解决方案能力枚举」里 16~27 齐全。**参数表没跟着更新，以附录为准**。

### 到综团购（16）的 permission_keys

`1` 门店管理（必传）、`16` 商户授权（必传）、`139` 团购核销、`140` 团购核销对账、`141` 订单查询、`142` 三方码发布、`143` 商品发布、`144` 商品查询、`145` KA核销对账、`146/147` 同步品牌户/门店户、`148~152` 门店亮照/基础信息更新/装修/匹配/任务查询、`153` 资质搜索、`154` 备货建议、`155` 团购退款、`156` 云连锁共管门店查询、`157` 入驻前信息处理。

> **枚举值不跨 solution 复用语义**：同样是「团购核销」，sol.1 是 12、sol.16 是 139、sol.21 是 175。
> 账单明细归属 `145 KA核销对账` 是**推断**（文档未给直接映射，但目录把 `composite_query` 放在「KA核销对账」下）。

### 服务商资质流程

注册服务商账号 → 认证「生活服务商家应用代开发」（约 10 工作日）→ 对公认证 → 创建第三方生活服务商家应用（审核 1 工作日）→ 应用详情页「解决方案」里对「到综团购解决方案」**开通能力权限** → 自助化接入（开发信息/IP白名单/SPI/Webhooks/测试店铺/联调验收/安全验收/压测）→ 上线。

调用侧：token 走 `https://open.douyin.com/oauth/client_token/`（client_token），请求头 `access-token`，可选 `Rpc-Transit-Life-Account`（来客商户根账户ID）。

---

## 6. 商家侧「授权业务」下拉里会出现什么

文档原话：「商家在抖音来客的授权SaaS服务商的应用时，**默认展示服务商账号下的全量应用和全量解决方案**，商家需要选择具体的应用和解决方案后才能完成授权。」

所以下拉里有没有到综，取决于两点：
1. 服务商（帆软）这个 client_key 下**是否已开通**到综系解决方案的能力权限；
2. 服务商有没有在「服务商平台 → 应用详情 → 授权管理 → 授权设置 → 商家可授权的解决方案」里把它勾掉。

三种授权路径：
- **方案一**（推荐）：拼 `auth.dylk.com/auth-isv/` 授权 URL，能顺带做门店绑定（传 `out_shop_id`）。**九数云走的就是这条**。
- **方案二**：商家在来客 PC 端「店铺管理 > 服务方应用授权 > 服务商代理 > 新增授权」，选服务商、应用、**授权行业**和**授权范围**，提交后自动通过。
- **方案三**：服务商在开放平台「授权管理 > 新增授权」发起，商家在来客「未处理授权」里同意。

授权成功推 Webhook `life_saas_cooperate_auth_with_bind`，含 `account_id`、`solution_key`、`permission_keys`、`out_shop_id`、`poi_id`。
授权页**不支持 iframe 内嵌**。

---

## 7. 对我们的落地含义

到综商户的等价替换（相对已跑通的餐饮链路）：

| 用途 | 餐饮（已跑通） | 到综（替换成） |
|---|---|---|
| 订单 | `/goodlife/v1/akte/order/query/` | `/goodlife/v1/trade/order/query/`，金额读 `sub_order_amount_infos` |
| 账单明细 | `/goodlife/v1/settle/bill/catering_query/` | `/goodlife/v1/settle/bill/composite_query/`（入参完全一致） |
| 提现 | `.../withdraw/catering_query/` | `.../withdraw/composite_query/` |
| 退款/售后 | `catering/.../refund-list-query` | **同一个接口**（通用目录） |
| 评价 | `/goodlife/v1/akte/comment/query/` | **无对应接口** |
| 核销/券/商品/门店 | 通用接口 | 不变 |

授权侧：`solution_key` 从 `1` 换成 **`16`**，`permission_keys` 至少 `1,16,139,140,141,143,144,145,155`。

**给九数云工单的说法**：不是「请增加对非餐饮的支持」，而是「到综团购解决方案（solution_key=16）的账单接口 `composite_query` 与贵方已实现的 `catering_query` 同 Scope、同入参，仅路径不同；订单查询更是直接复用通用接口。请问接入到综有无排期？」

---

## 8. 未查到 / 待确认

1. 到综的评价查询接口：**确认不存在**。
2. 代运营佣金 `general-capabilities/paterner/*`：文档页已失效（空壳），但到综团购方案介绍页仍在链它。
3. 账单权限归属 `145` 是推断。
4. 帆软是否已开通到综系解决方案权限：**需要看车曼在来客「新增授权」时下拉里有没有到综**，这是最直接的判据。
5. ~~美团侧的到综 API：本次调研被中断~~ **已完成**，见同目录 `meituan-open-platform-api-index.md`。结论：美团到综叫「服务零售」，businessId 58/59，接口齐全；我们之前失败是因为 storemap 授权入口只支持 businessId 1/2/3/16，到综要走 `general/auth` 的 OAuth 模型。
