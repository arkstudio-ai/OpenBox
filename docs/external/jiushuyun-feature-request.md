# 致九数云数据源团队：抖音来客「到综」与美团「服务零售」接入需求

> 提出方：南宁贝之宝科技有限公司 ｜ 企业版有效期 2026-09-18 至 2027-09-18 ｜ 已用数据源：抖音来客、美团 ｜ 2026-09-28
>
> 本文可直接转发给九数云。网页版（需在 Share 菜单开放访问后才能分享）：https://claude.ai/artifact/GYvXpemPv7CvJBLnhDr6mH

---

## 两条诉求

**一、抖音来客：增加「到综团购解决方案」（`solution_key=16`）。**
当前产品固定使用 `solution_key=1`（到店餐饮），而该方案已被抖音官方标注「即将下线」。到综的账单接口与贵方已实现的餐饮账单接口**同 Scope、同入参，仅路径不同**。

**二、美团：增加「服务零售」业务线（`businessId=58` 与 `59`）。**
当前产品只有外卖、团购、到店广告三档，均走 `storemap` 授权模型，而该模型官方只支持 `businessId` 1/2/3/16。服务零售必须走 `general/auth` 的 OAuth 2.0 模型，需要新增一套授权实现。

---

## 1. 我们的使用现状

我们已在贵方企业版上完成来客数据源的完整接入并投入使用：一家餐饮商户在来客后台把「帆软软件有限公司 / E数通」授权为服务商，我们在数据连接里建立连接后，12 张表全部同步成功，并通过开放平台的 `/api/v2/export` 接口把分析表导出到我们自己的系统。定时同步按贵方建议设为每天 5 次。

问题出在商户类目上。我们的客户里有相当比例是**非餐饮的到店商户**，例如汽车服务、丽人、休闲娱乐。这些商户目前两个数据源都接不进来：

- **来客**：贵方只实现了「到店餐饮解决方案」的数据表，非餐饮商户即使完成授权也没有可用的表。
- **美团**：我们让一家汽车服务类商户走贵方的美团「团购」授权，跳转到 `open-erp.meituan.com/storemap` 后，商户登录美团账号，门店列表为空，弹窗提示「账号内没有美团门店，请联系您的业务经理或访问入驻系统创建门店」。商户的门店在美团后台是正常存在的。

我们排查后确认，这两种情况都**不是上游平台不支持**，而是需要接入平台方各自提供的另一套业务线。

---

## 2. 需求一 · 抖音来客「到综团购解决方案」

**当前**：贵方授权链接固定为 `solution_key=1`（到店餐饮），数据表只覆盖 `catering/dining-group-solution` 目录，非餐饮商户无表可用。

**希望增加**：`solution_key=16`「到综团购解决方案」，覆盖汽车服务、丽人、休闲娱乐、亲子、运动健身等全部非餐到店类目。建议同时评估 `solution_key=21`（拆分后的到店餐饮团购）。

**依据**：抖音官方《解决方案场景化拆分公告》已将原「到综行业解决方案」拆为到综团购、阶梯次卡、券转赠、组合券包、会员五个；原「到店餐饮解决方案」拆为六个。在《能力授权&门店绑定SDK》文档的枚举附录中，`solution_key=1` 与 `solution_key=4` 均已标注「即将下线」。

> 注：该文档正文的参数表只列了旧的 9 个 solution_key，附录枚举里 16 至 27 是齐全的，参数表未同步更新，请以附录为准。

### 改动量评估

这项改动比新增一个数据源小得多，因为到综团购**自身只有 4 个定制接口（全部是对账），其余全部复用通用接口**。

其中最关键的账单明细接口，与贵方已经实现的餐饮版本**是同一个 Scope `life.capacity.billing.detail`，入参完全一致**，只有 HTTP 路径不同：

| | 路径 |
|---|---|
| 餐饮（已实现） | `GET /goodlife/v1/settle/bill/catering_query/` |
| 到综（需新增） | `GET /goodlife/v1/settle/bill/composite_query/` |

订单查询更简单，到综直接使用通用接口 `/goodlife/v1/trade/order/query/`，无需新增适配。

### 到综专属接口（对账）

Scope 均为 `life.capacity.billing.detail`。

| 接口 | 方法与路径 |
|---|---|
| 综合账单详情查询 | `GET /goodlife/v1/settle/bill/composite_query/` |
| 综合账单详情查询（连锁） | `GET /goodlife/v1/settle/bill/composite_query_new/` |
| 综合提现记录查询 | `GET /goodlife/v1/settle/withdraw/composite_query/` |
| 综合提现记录（连锁） | `GET /goodlife/v1/settle/withdraw/composite_query_new/` |

官方文档注明「不同行业返回参数不同，其他行业商家请勿查询此接口」，因此账单接口需按行业分别落表，不能与餐饮共用一张。

### 到综复用的通用接口

| 能力 | Scope | 方法与路径 |
|---|---|---|
| 订单查询 | `life.capacity.order.query` | `GET /goodlife/v1/trade/order/query/` |
| 退款单列表查询 | `life.capacity.catering_after_sale_order` | `GET /goodlife/v1/akte/after_sale/order/query/` |
| 查询门店信息 | `life.capacity.shop` | `GET /goodlife/v1/shop/poi/query/` |
| 验券历史查询 | `life.capacity.billing` | `GET /goodlife/v1/fulfilment/certificate/verify_record/query/` |
| 商品查询与发布 | `life.capacity.goods.query` / `.found` | 通用目录，路径不变 |

### 两个实现细节

- **金额字段读法不同**。订单查询返回中，`amount_info` 仅餐饮行业使用；到综及其他行业必须读 `sub_order_amount_infos`（其中 `sub_order_type`：1 配送费 / 2 打包费 / 3 服务费 / 100 商品费用）。沿用餐饮的解析逻辑会拿到空值。
- **到综没有评价接口**。整个 `comprehensive/` 目录下没有评价查询，评价接口（`life.capacity.catering.comment`）只存在于餐饮目录。到综商户的评价数据在抖音侧暂时取不到，需要在产品上对用户说明。

### 授权参数

授权地址不变，仍是 `https://auth.dylk.com/auth-isv/`，把 `solution_key` 由 `1` 改为 `16`，`permission_keys` 建议至少包含：

| 值 | 能力 | 值 | 能力 |
|---|---|---|---|
| `1` | 门店管理（必传） | `143` | 商品发布 |
| `16` | 商户授权（必传） | `144` | 商品查询 |
| `139` | 团购核销 | `145` | KA 核销对账 |
| `140` | 团购核销对账 | `155` | 团购退款 |
| `141` | 订单查询 | | |

注意同一能力在不同 solution 下的枚举值不同：「团购核销」在 `solution_key=1` 下是 `12`，在 `16` 下是 `139`，不能沿用。

---

## 3. 需求二 · 美团「服务零售」业务线

美团把到店综合官方改名为**「服务零售」**，所有非餐到店类目（含医药健康）统一收敛在两条业务线下，行业差异只体现在解决方案文档和 scope，不体现在 businessId。

**当前**：贵方美团数据源提供三种业务类型，外卖（非接单）`16`、团购 `1`、到店广告 `22`，授权走 `open-erp.meituan.com/storemap`，该入口官方**只支持 businessId 1 / 2 / 3 / 16**。

**希望增加**：
- `businessId=58` 服务零售：核销、评价、经营数据
- `businessId=59` 服务零售（客户）：商品、订单、**财务结算**

两者是独立授权，需分别获取 token。

**依据**：美团《门店映射》官方文档原文「businessId：1 团购、2 外卖、3 闪惠、16 外卖（非接单）」——只有这四个值。这解释了我们遇到的现象：汽车服务商户的门店在美团后台真实存在，但用 `businessId=1` 走 `storemap`，而 `1` 就是「到店餐饮团购」，该入口只在到餐范围内检索门店，因此返回空列表。

### 授权模型必须更换

这是本条需求里工作量最大的一处，服务零售**不能复用现有的 storemap 实现**：

| 环节 | 现有（storemap） | 服务零售（general/auth） |
|---|---|---|
| 授权入口 | `open-erp.meituan.com/storemap` | `open-erp.meituan.com/general/auth` |
| 支持的 businessId | 1 / 2 / 3 / 16 | 58 / 59 及 15、18、22、27、31、33、46、55、57、71、91 |
| 商户登录账号 | 美团商家账号 | **经营宝账号** |
| 令牌 | 门店映射，无 token 流程 | OAuth 2.0：`POST api-open-cater.meituan.com/oauth/token`，code 有效期 10 分钟 |
| 续期 | 不适用 | `/oauth/refresh`，token 30 天、refreshToken 35 天 |
| 解绑 | 不适用 | `open-erp.meituan.com/general/unauth` |
| 门店标识 | `ePoiId` | 回传 `opBizCode` 与 `opBizName`，用 `state` 做映射 |

另需注意：一个门店或客户**只能授权给一个 developerId**，与现有团购授权的独占性一致。

### 我们最需要的接口

docKey 拼到 `https://developer.meituan.com/docs/api/` 之后即为文档页。优先级从上往下递减。

| 用途 | biz | docKey |
|---|---|---|
| **验券记录** | 58 | `ddzh-tuangou-receipt-querylistbydate` |
| 查询已验券信息 | 58 | `ddzh-tuangou-receipt-getconsumed` |
| **查询账期收入明细** | 59 | `ddzhkh-finance-income-detail` |
| 账期信息 | 59 | `ddzhkh-finance-query-payplan` |
| 账期调整明细 | 59 | `ddzhkh-finance-deduct-detail` |
| 订单批量查询 | 59 | `ddzhkh-dingdan-queryOrder` |
| 订单券码分摊金额 | 59 | `ddzhkh-dingdan-receipt-paymentshares` |
| 单一门店评论数据 | 58 | `ddzh-ugc-queryshopreview` |
| 门店星级和单项分 | 58 | `ddzh-ugc-querystar` |
| 门店 ID 映射 | 59 | `ddzhkh-auth-token-queryPoiMapping` |
| 团购退款查询 | 58 | `ddzh-tuangou-query-refund-info` |
| 经营数据（消费/流量） | 58 | `ddzh-merchantdata-consumption`、`ddzh-merchantdata-poitraffic` |

服务零售下共有 333 个接口（`58` 有 155 个，`59` 有 178 个）。若要分期实现，建议先做 `58` 的核销与 `59` 的财务两组。

### scope 对照

- `businessId=58`：`tuangou` 团购、`ugc` 评价、`yuding` 预订、`merchantdata` 经营数据、`customercenter` 客资、`manager` 核销管理、`technician` 手艺人
- `businessId=59`：`shangpin` 商品管理、`dingdan` 订单信息、`finance` 财务、`merchantreceipt` 商家券、`member` 会员

---

## 4. 另外两个小问题

### 美团到店广告的 cpc 分日报告没有对应数据表

贵方美团数据源的帮助文档在「到店广告」业务类型下写明可获取「cpc门店分日报告」，但我们在数据表清单里没有找到对应的表。美团侧该接口是存在的，docKey 为 `ad-report-getDailyDataByShopOffline`（`businessId=22`）。同组还有小时报告、账户分日报告和今日实时数据。

### 来客退款单「只能查 90 天」的限制来源存疑

贵方文档在退款单列表查询上标注了 90 天限制。我们通查了抖音生活服务开放平台的全部接口文档，明确写出天数上限的只有两个：评价查询（最近 90 天）和线索查询（跨度 365 天）。退款单列表查询、订单查询、账单对账、验券历史均未声明可回溯天数。如果这个限制是贵方实现上的约定而非上游限制，希望能放宽；如果确实来自上游，也请告知依据，我们好对客户说明。

---

## 5. 参考文档

本文所有结论均来自下列官方文档，未采用二手来源。

### 抖音生活服务开放平台

- [能力授权&门店绑定SDK（solution_key 与 permission_keys 全量枚举）](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/auth_with_bind)
- [到综团购对接方案介绍（能力与接口映射表）](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/group-buying-integration)
- [综合账单详情查询 composite_query](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/ka-reconciliation/composite_query)
- [订单查询（到综复用此接口）](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/order.query/query)
- [解决方案场景化拆分公告](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/solution/solution-split-notice)
- [商家对服务商授权](https://developer.open-douyin.com/docs/resource/zh-CN/local-life/connect/partner/saas-auth)

### 美团技术服务合作中心

文档全站公开，无需登录。

- [第三方业务授权（OAuth 2.0 主文档）](https://developer.meituan.com/docs/biz/comm-dev-isv-auth)
- [门店映射 storemap 官方说明（含仅支持 1/2/3/16 的表述）](https://developer.meituan.com/docs/biz/biz-commv1-0204)
- [接口调用约定（含完整业务 ID 列表）](https://developer.meituan.com/docs/biz/comm-dev-isv-api-rule)
- [scope 参数说明（58 与 59 的 scope 划分）](https://developer.meituan.com/docs/biz/biz_2023243_3ad0b0e7-01d4-40a8-8a3f-2e6f6ae32f23)
- [服务零售及医药健康业务授权流程](https://developer.meituan.com/docs/biz/biz_ddzh_6600d4b2-d8ba-4410-aea4-9d380e43e645)
- [跨行业通用团购核销](https://developer.meituan.com/docs/biz/biz_ddzh_40e4098e-8b2b-4d6c-a916-47ea00e06248)
- [财务数据直连](https://developer.meituan.com/docs/biz/biz_ddzh_03c683bb-0954-40db-b063-ea113edcb629)

---

如需我们配合，可以提供：非餐饮商户的真实授权测试账号、已踩到的报错截图与复现步骤、以及我们整理的抖音全量接口索引（407 个接口页，含 Scope、HTTP 路径与文档链接）。

关于排期，我们希望了解这两条业务线是否已在计划内。如果短期内无法排期，也请告知，我们好安排非餐饮商户的替代方案。
