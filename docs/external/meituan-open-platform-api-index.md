# 美团开放平台 · 文档站与接口索引（含到店综合/服务零售）

> 2026-09-21 整理。来源：`developer.meituan.com` 自身的文档树 JSON 接口（免登录开放）
> - `https://developer.meituan.com/api/v1/doc/menu/apis` — 全部 API 树，49 个业务组，**每个节点自带 businessId**
> - `https://developer.meituan.com/api/v1/doc/menu/tree?type=1` — API 文档导航树（1371 篇）
> - `https://developer.meituan.com/api/v1/doc/menu/tree?type=4` — 业务文档树（2182 节点）
>
> **文档全部公开，不需要登录。** 只有控制台和权限申请要登录。
>
> ⚠️ 该站是 SPA，**任何路径都返回 HTTP 200**，状态码不能证明页面存在。本文所有 docKey 均从平台自己的 JSON 树校验得出。
>
> URL 规则：API 文档 `https://developer.meituan.com/docs/api/{docKey}`，业务文档 `https://developer.meituan.com/docs/biz/{docKey}`

---

## 0. 最重要的一条：storemap 不是到综的授权入口

这条直接解释了 2026-09-20 车曼（汽车服务类）授权失败的原因。

美团有**两套互不相通的授权模型**：

### 模型一 · 门店映射 UISDK（我们之前用的）
`https://open-erp.meituan.com/storemap?developerId=&businessId=&ePoiId=&timestamp=&sign=&state=`

官方文档原文：「businessId：**1团购、2外卖、3闪惠、16外卖(非接单)**」——**只有这 4 个值**。

→ 车曼用 `businessId=1` 走 storemap，1 就是**到店餐饮团购**，只在到餐范围找门店，所以显示「账号内没有美团门店」。**到综商户在这条路上永远查不到门店。**

### 模型二 · 第三方业务授权 OAuth 2.0（到综必须走这条）
- 授权页：`https://open-erp.meituan.com/general/auth?developerId=&businessId=&timestamp=&charset=UTF-8&sign=&state=&scope=`
- 换 token：`POST https://api-open-cater.meituan.com/oauth/token`（`grantType=authorization_code`，code 有效期 10 分钟）
- 刷新：`POST https://api-open-cater.meituan.com/oauth/refresh`（token 30 天过期，refreshToken 35 天）
- 解绑：`https://open-erp.meituan.com/general/unauth`
- 适用 businessId：**58、59**、15、18、22、27、31、33、46、55、57、71、91
- 商户侧：服务零售(58) 授权「服务零售门店」，服务零售-客户(59) 授权「服务零售客户」，**均登录经营宝账号**
- **1 个门店/客户只能授权 1 个 developerId**
- 58/59 回传 `opBizCode`（门店唯一标识）和 `opBizName`（门店名），用 `state` 做我方门店 ID 映射
- 主文档：`https://developer.meituan.com/docs/biz/comm-dev-isv-auth`

**九数云的美团数据源只提供外卖(16)/团购(1)/到店广告(22)三档，全是 storemap 模型，所以它接不了到综。这是九数云的实现范围问题，不是美团的能力问题。**

---

## 1. 文档站入口清单

| 站点 | URL | 管什么 | 需登录 |
|---|---|---|---|
| **美团技术服务合作中心**（主站，唯一权威） | `https://developer.meituan.com/` · 文档中心 `/docs` | **所有到店/外卖/配送/到综业务线总入口**，49 业务组、1789 个 API 节点 | 否 |
| 到餐开放平台 | 无独立站，即主站「到店餐饮」板块；网关 `https://api-open-cater.meituan.com` | 到餐团购核销、闪惠、预订、排队、在线点 | 否 |
| **到店综合（到综）** | 无独立站，即主站「**服务零售**」板块 `/docs/biz/daozong` | 非餐到店全行业 + 医药健康 | 否 |
| 外卖开放平台 | `https://open.waimai.meituan.com/openapi_docs/` → 302 到主站 | 已并入主站 | 否 |
| 配送开放平台（独立） | `https://peisong.meituan.com/open/doc` · `/open/guide` | 独立网关 `https://peisongopen.meituan.com/api`，appkey+secret **SHA1** 签名 | 否，但页面自称「已不再维护」 |
| 企业版技术文档 | `https://h5.dianping.com/app/bep-docs/sky-doc/` | 企业因公消费/差旅报销，网关 `api-sqt.meituan.com`。**与商家侧无关，无券核销** | 否 |
| 企业用餐开放平台 | `https://eps.meituan.com/open/` | 企业团餐/补贴 | 否 |
| 大众点评开放平台 / 北极星 | `https://open.dianping.com/` | **已停用**，2025-07-02 起迁主站，旧文档 URL 全部 500 | — |
| 美团账号 OAuth | `https://open.meituan.com/docs/` | 第三方接「美团账号登录」，**不是**商家 API | JS 渲染 |
| POI 数据开放 | `https://poiopen.dianping.com/` | POI 基础检索，无券/订单/结算 | JS 渲染 |
| 闪购/医药/买菜 | `open-shangou.meituan.com` → 301 → `https://tscc.meituan.com/` | **硬登录墙** | 是 |
| 美团联盟（CPS） | `https://union.meituan.com/` · 接口文档 `https://page.meituan.net/html/1701831807616_1db0df/index.html` | 分销带货，网关 `media.meituan.com/cps_open/` | 否 |
| 开店宝 / 经营宝 | `https://e.meituan.com/` | **商家控制台，不是开放平台**；它的作用是「商家授权时登录的账号类型」 | 是 |

**结论**：到餐、到综、外卖、到店广告不是四个平台，是主站下的四个业务组。真正独立的只有配送、企业版、联盟、闪购医药。

---

## 2. businessId 对照表

来源双重确认：① 官方《接口调用约定【必读】》`/docs/biz/comm-dev-isv-api-rule` 的业务ID列表；② 平台 `/api/v1/doc/menu/apis` 树上每个节点自带的 businessId。

| biz | 业务 | API数 |
|---|---|---|
| 1 | 到店餐饮团购 | 55 |
| 2 | 外卖 | 203 |
| 3 | 闪惠 | 8 |
| 5 | 平台基础业务 | — |
| 7 | 到店餐饮预订 | 21 |
| 15 | 到餐品牌会员卡 | 4 |
| **16** | **外卖非接单** | 236 |
| 17 | 茶饮版 | 45 |
| 18 | 餐饮系统-智能版 | 555 |
| 19 | 配送-合同版 | 24 |
| **22** | **到店广告** | 42 |
| 24 | AI服务-图文处理 | 13 |
| 26 | 小黄卡 | 6 |
| 27 | 快驴 | 7 |
| 31 | 配送-零售版 | 17 |
| 33 | 顾客点餐自助核销 | 4 |
| 37 | 酒旅经营宝 | 22 |
| 43 | 到店券合作 | 8 |
| 46 | 团购配送 | 27 |
| 49 | 餐饮排队 | 18 |
| 50 | 美团平台直播 | 15 |
| 51 | 到店餐饮在线点 | 52 |
| 52 | 美团收单 | 43 |
| 53 | 到餐评价 | 2 |
| 55 | 大众点评免费试 | 6 |
| 57 | 酒店管理系统 | 100 |
| **58** | **服务零售 / 到店综合** | **155** |
| **59** | **服务零售(客户) / 到综客户** | **178** |
| 61 | 站外分销 | 4 |
| 66 | 门票直连 | 9 |
| 67 | 度假 | 8 |
| 71 | 门店基础信息直连 | 4 |
| 79 | 餐饮直连解决方案 | 11 |
| 86 | 餐饮客户解决方案 | 6 |
| 87 | 企业版 | 2 |
| 92 | 美团租车 | 9 |
| 94 | 自动物流车 | 13 |
| 1000 | 公共服务 | 19 |

（其余：28 AI对话机器人、29 语音原子能力、32 到餐小程序、36 智能外呼、39 自动车配送、42 NLP、54 斑码内容工具、60 充电宝、70 蘑菇订单、73 美团快点、77 客满满、78 境外酒店、91 游戏实物履约）

### 丽人 / 汽车服务 / 休闲娱乐 / 亲子 / 运动健身 是哪个 businessId？

**它们没有各自的 businessId，全部归入 58（服务零售），客户维度是 59。**

美团把整个非餐到店（含医药健康）收敛成一条业务线，行业差异体现在「行业解决方案」文档和 scope，不体现在 businessId。官方原文（《跨行业通用团购核销》，更新于 2025-12-09）：

> 「行业无特殊业务对接均可直接使用此团购直连模板」

只有医疗健康、教育、洗浴、乐生活、文印、茶馆、体检等少数行业有专门对接流程文档，businessId 仍是 58。

### scope 标识对照
文档：`/docs/biz/biz_2023243_3ad0b0e7-01d4-40a8-8a3f-2e6f6ae32f23`

- **biz=58**：`tuangou`(团购) · `ugc`(评价) · `yuding`(预订) · `merchantdata`(经营数据) · `customercenter`(客资) · `manager`(核销管理) · `technician`(手艺人) · `thirdresource` · `registration`(医院挂号)
- **biz=59**：`shangpin`(商品管理) · `dingdan`(订单信息) · `generalreserve` · `member` · `merchantreceipt`(商家券) · **`finance`(财务)** · `dzminiprogram` · `clubactivity` · `tijianzixun`

---

## 3. 服务零售（到店综合）接口清单 · businessId 58 / 59

**到综 API 共 333 个**（58 有 155 个，59 有 178 个）。

| 分组 | biz | 代表接口 docKey |
|---|---|---|
| **团购核销** | 58 | `ddzh-tuangou-receipt-prepare`(输码验券校验) · `ddzh-tuangou-receipt-consume`(验券) · `ddzh-tuangou-receipt-reverseconsume`(撤销) · **`ddzh-tuangou-receipt-getconsumed`(查询已验券信息)** · **`ddzh-tuangou-receipt-querylistbydate`(验券记录)** · `ddzh-tuangou-receipt-batchconsume`(次卡批量验券) · `ddzh-tuangou-receipt-querybymobile`(手机号查可用券) |
| **团购退款** | 58 | `ddzh-tuangou-pre-refund-valid` · `ddzh-tuangou-apply-refund` · `ddzh-tuangou-query-refund-info` · `ddzh-tuangou-refund-audit` |
| **财务/结算** | 59 | **`ddzhkh-finance-income-detail`(查询账期收入明细)** · `ddzhkh-finance-query-payplan`(账期信息) · `ddzhkh-finance-deduct-detail`(账期调整明细) · `ddzhkh-finance-order-detail`(查询券详情) · `ddzhkh-finance-payplan-type` · 滚动账期 `ddzhkh-finance-dnIncome-detail` / `ddzhkh-finance-dnDeduct-detail` |
| **订单** | 59 | `ddzhkh-dingdan-queryOrder`(订单批量查询) · `ddzhkh-dingdan-generalconnect-query-info`(订单及券码状态查询·新) · `ddzhkh-dingdan-receipt-paymentshares`(**订单券码分摊金额**) · `ddzhkh-dingdan-reserveorderquery` |
| **门店点评** | 58 | `ddzh-ugc-querystar`(门店星级和单项分) · `ddzh-ugc-queryshopreview`(单一门店评论数据) |
| **团单商品管理** | 59 | `ddzhkh-shangpin-dealgroup-create/submit/query/online/offline/updateprice/updatestock/updateshopids/drawback/relate-rooms` · 商品 `ddzhkh-shangpin-product-*` · 新版通用 `ddzhkh-shangpin-united-query-product` / `-update-shops` / `-update-stocks` / `-update-prices` · `ddzhkh-shangpin-image-upload` |
| **门店/授权** | 59/58 | `ddzhkh-auth-token-queryPoiMapping`(门店ID映射) · `ddzhkh-auth-token-pageQueryPoiList`(适用门店查询) · `ddzh-common-authorization-grantingOpCustomer-query` |
| **经营数据** | 58 | `ddzh-merchantdata-consumption`(消费数据) · `ddzh-merchantdata-dealgroups`(团单消费详情) · `ddzh-merchantdata-poitraffic`(门店流量) · `ddzh-merchantdata-book`(预约数) |
| 商家券 | 59 | `ddzhkh-merchantreceipt-verify-verifyreceipt` · `ddzhkh-merchantreceipt-query-verifyreceipthistory` · `ddzhkh-merchantreceipt-reverseconsume` |
| 其他 | 58 | 店铺优惠码 `ddzh-poiqrcode-querydzcoupon` · 手艺人 `ddzh-shouyiren-*`(10) · 客资 `ddzh-customercenter-addfollowup` · 预订 `ddzh-yuding-*`(40) |
| 北极星迁移 | 58 | `ddzh-common-datamigration-migrateSession` · `ddzh-common-transfer-openShopUuidToOpPoiId` 等 15 个 ID 互转接口 |

### 到综业务文档（`/docs/biz/`）
- 团购核销 `biz_ddzh_47f98a84-8178-4e2f-8562-26a2f25dddeb`
- 财务 `biz_ddzhkh_1f2a5555-e135-4748-8562-b4c15f573b04`
- 订单服务 `biz_ddzhkh_7273339f-cb56-4a99-8487-5519d2a5dc1d`
- 评价 `biz_ddzh_0d4c7ad5-3564-4c71-a349-c9cb69c4e387`
- 商品服务 `biz_ddzhkh_8e72569b-f550-460e-9b81-84cb5da324dc`
- 经营数据 `biz_ddzh_2603bdaf-2bce-434c-b8c1-be0c662eada9`

### 行业解决方案（`/docs/biz/`）
- **跨行业通用团购核销** `biz_ddzh_40e4098e-8b2b-4d6c-a916-47ea00e06248`
- **财务数据直连** `biz_ddzh_03c683bb-0954-40db-b063-ea113edcb629`
- 团购商品管理 `biz_ddzh_dcf3b2b5-76d1-45f5-b844-6e8df732f287`
- 医疗健康 `biz_ddzh_f70c257d-...` · 教育 `biz_ddzh_751def99-...` · 洗浴 `biz_ddzh_9098a03f-...` · 乐生活 `biz_ddzh_75946cbd-...`

---

## 4. 到店餐饮团购接口（businessId=1，网关 `api-open-cater.meituan.com`）

我们已在九数云用到的两个接口的官方文档页：

| 接口路径 | docKey | 标题 |
|---|---|---|
| `/tuangou/coupon/queryTradeDetail` | `tuangou-coupon-queryTradeDetail` | 9.3.查询团购订单结算明细 |
| `/tuangou/coupon/queryById` | `tuangou-coupon-queryById` | 5.2.已验券码查询 |

同组其余：
- **核销**：`tuangou-coupon-prepare`(验券准备) · `tuangou-ng-coupon-msprepare`(新) · `tuangou-coupon-consume`(执行验券) · `tuangou-ng-coupon-msconsume`(新) · `tuangou-coupon-cancel`(撤销验券)
- **核销记录**：`tuangou-coupon-queryListByDate`(门店验券历史) · `tuangou-coupon-queryLocalListByDate`(门店本地验券历史)
- **结算**：`tuangou-ng-coupon-externalProfitDetailRequire`(结算扩展明细) · `tuangou-ng-coupon-getCouponPriceInfo`(团购券交易快照)
- **团单/门店**：`tuangou-coupon-querySetMealList`(门店套餐映射) · `tuangou-ng-coupon-querySetMealListV1`(套餐状态) · `tuangou-coupon-queryDealAttr`(团购项目限制条件)
- 业务说明：`/docs/biz/biz-tuangou-root`

---

## 5. 外卖（businessId=2 接单版 / 16 非接单版）

九数云用到的 5 个接口的官方文档：

| 接口路径 | docKey | 标题 |
|---|---|---|
| `/wmoper/ng/order/queryOrders` | `wmoper-ng-order-queryOrders` | 10.2.1.查询订单列表 |
| `/wmoper/ng/order/queryDetail` | `wmoper-ng-order-queryDetail` | 10.2.2.查询订单详情(展示费率字段) |
| `/wmoper/ng/comment/queryCommentList` | `wmoper-ng-comment-queryCommentList` | 10.6.1.查询门店评价信息 |
| `/wmoper/ng/comment/queryScore` | `wmoper-ng-comment-queryScore` | 10.6.3.查询门店评分 |
| `/wmoper/ng/poi/getPoiScoreDetail` | `wmoper-ng-poi-getPoiScoreDetail` | 10.8.12.查询店铺分 |

⚠️ `wmoper-order-queryOrderDetail`（无 `-ng-`）在文档树里标记为**废弃**，与九数云字段清单里「已停用」分组吻合。
⚠️ `queryOrders` 文档正文注明**仅能查近 5 天数据**，建议调用频率不超过每分钟一次——这正是九数云「首次只回溯 5 天」的来源。

其他：`wmoper-ng-poi-mget` · `wmoper-ng-poi-detail` · `wmoper-ng-comment-addReply` · `wmoper-ng-comment-complain-report`

---

## 6. 到店广告（businessId=22，42 个接口）

- 报表：`ad-report-getDailyDataByShopOffline`(**cpc门店分日报告**) · `ad-report-getHourlyDataByShopOffline` · `ad-report-getDailyDataByAccountOffline` · `ad-report-getCpcRtDataByShop`(今日实时) · `ad-report-getDailyCpmData` / `getDailyHourlyCpmData`
- 管理：`ad-launch-queryCpcLaunchIds` · `ad-launch-batchCreateCpcLaunchs` · `ad-launch-batchUpdateCpcLaunchStatus` · `ad-launch-batchEditCpcPlanBudget` · `ad-launch-batchEditCpcBidPrice`
- 业务文档：`/docs/biz/biz-ad-root` · `/docs/biz/biz-ad-report` · `/docs/biz/biz-ad-manage`

> 注：`ad-report-getDailyDataByShopOffline` 就是九数云文档提到但看板里没有对应表的「cpc门店分日报告」。

---

## 7. 其他可能有用的业务线

**餐饮直连 / 餐饮客户方案**（结算流水的另一条路）：
`dishconnect-tuangou-coupon-queryLocalListByDate`(门店本地验券历史) · `dishconnect-tuangou-coupon-queryProfitDetail`(券码结算扩展明细) · **`dishconnect-settle-queryByPage`(分页查询账单流水)** · `solution2-csaccount-scrollQueryTradeProfitDetailOpPoiId`(游标查询交易结算流水)

**配送**：主站内 biz=19/31 — `peisong-order-createByShop` · `peisong-order-queryStatus` · `peisong-order-cancel` · `peisong-shop-query` · `peisong-shop-area-query` · `peisong-order-rider-location`

---

## 8. ISV 接入流程文档

| 主题 | URL |
|---|---|
| **第三方业务授权（OAuth 2.0 主文档）** | `/docs/biz/comm-dev-isv-auth` |
| **门店映射 storemap 官方说明** | `/docs/biz/biz-commv1-0204` |
| storemap 详细参数（含 businessId 取值） | `/docs/biz/biz_waimaing_df60ceb0-f17d-403e-8a13-884b2ee14555` |
| 接口调用约定【必读】+ 业务ID列表 | `/docs/biz/comm-dev-isv-api-rule` |
| 签名规则 | `/docs/biz/comm-dev-isv-sign-rule` |
| scope 参数说明 | `/docs/biz/biz_2023243_3ad0b0e7-01d4-40a8-8a3f-2e6f6ae32f23` |
| 服务商入驻流程 | `/docs/biz/biz_2023243_b9c16a0b-93ba-49af-a570-39eb4861b92c` |
| 服务商合作对接流程 | `/docs/biz/biz_xn202005112_36af9674-1474-4093-8036-5efc2690e232` |
| 公共请求/响应参数 | `/docs/biz/biz-comm-sys-param-req` · `/docs/biz/biz-comm-sys-param-resp` |
| 扫码授权说明 | `/docs/biz/biz_2023243_0b27b26a-0026-40fa-90f1-c56e8f3167d0` |
| **服务零售授权流程** | `/docs/biz/biz_ddzh_6600d4b2-d8ba-4410-aea4-9d380e43e645` |
| 单业务授权 UISDK / 接口对接 | `/docs/biz/biz_ddzh_d8897594-c83b-4d4c-b2e5-8cd6758bc37a` · `/docs/biz/biz_ddzh_e9b421d2-ead0-4e83-ae22-4164f4b8e91b` |
| 多业务授权 接口 / 工具 | `/docs/biz/biz_ddzh_e88a15f3-b330-4814-bf30-893c88341700` · `/docs/biz/biz_ddzh_7bc37b75-13a8-411a-919a-f2928e7a5e81` |
| 北极星迁移指引 / 接口对照表 | `/docs/biz/biz_ddzh_d4a588f8-98c9-4986-9b2f-102543f0e6a8` · `/docs/biz/biz_ddzh_46594b73-697b-4b24-85dc-b353b0a53c12` |
| FAQ：对接服务零售的授权流程 | `/docs/biz/biz_xn202005112_f22e4b81-098f-4bc3-80c5-8ade4a5cb412` |
| 到综授权常见问题 | `/docs/biz/biz_ddzh_76eb0347-c407-4174-b9a6-e28c0c379fd1` |

---

## 9. 非餐（到综）商户到底能不能拿券核销和结算数据

**能，有直接证据。**

- **券核销**（biz=58，scope `tuangou`）：`ddzh-tuangou-receipt-prepare` → `ddzh-tuangou-receipt-consume` → `ddzh-tuangou-receipt-reverseconsume`；取数用 `ddzh-tuangou-receipt-getconsumed`（查询已验券信息）和 `ddzh-tuangou-receipt-querylistbydate`（验券记录）。官方《跨行业通用团购核销》文档的对接接口表直接写明「团购对账 → 查询已验券信息 / 验券记录」。
- **结算**（biz=59，scope `finance`，7 个接口）：`ddzhkh-finance-income-detail`（查询账期收入明细）、`ddzhkh-finance-query-payplan`、`ddzhkh-finance-deduct-detail`、`ddzhkh-finance-order-detail`、滚动账期版两个。另有《财务数据直连》行业方案和订单侧 `ddzhkh-dingdan-receipt-paymentshares`（订单券码分摊金额）。
- **门店点评**：`ddzh-ugc-querystar`、`ddzh-ugc-queryshopreview`。

**要做的改动**：授权从 `storemap?businessId=1` 换成 `general/auth?businessId=58`（核销/评价/经营数据）和 `businessId=59`（商品/订单/财务），并在控制台申请对应 businessId 的业务权限和 scope。**58 和 59 是两个独立授权，要分别拿 token。**

---

## 10. 未找到 / 卡点

| # | 条目 | 原因 |
|---|---|---|
| 1 | 闪购/买菜/医药独立文档 | `tscc.meituan.com` 硬登录墙，robots.txt 和 sitemap.xml 都返回「用户未登录」。注：医药健康的商家侧能力其实在主站 biz=58 下 |
| 2 | 开店宝/经营宝数据导出 API | **不存在**。经营宝是商家授权时登录的账号类型，不是开放平台 |
| 3 | 外卖结算/账单明细 API | **确认空白**。biz=2 和 16 下没有结算接口。结算只存在于团购、团购配送、餐饮客户方案、美团收单、智能版POS。要外卖结算只能走 POS 线 |
| 4 | 外卖券核销 | 不存在。外卖只有发券，无核销 |
| 5 | 丽人/汽车/休娱/亲子/运动各自的 businessId | **确认不存在**，统一 58/59 |
| 6 | 点评北极星旧文档正文 | 全部 HTTP 500，需点评管家登录态。2025-07-02 起已迁主站 |
| 7 | `peisong.meituan.com/open/docNew` | JS 空壳 |
| 8 | `platform.meituan.com/apidoc/description` | 纯 JS，无法判定业务线 |
| 9 | `poiopen.dianping.com` 完整接口清单 | TOC 为 JS 渲染 |
| 10 | `openapi.meituan.com` / `open.meituan.com` | 均 JS 渲染；`openapi.meituan.com` 是网关域，非文档站 |

**来源声明**：本文**没有任何 URL 或接口路径来自二手来源**。所有 docKey 均从 `developer.meituan.com` 自己的 JSON 文档树校验；所有接口路径与授权参数均引自官方页面正文。

---

## 11. 与九数云的关系

| 我们要的 | 九数云能给 | 美团直连能给 |
|---|---|---|
| 餐饮商户团购核销/结算/点评 | ✅ 已跑通（团购三张表） | ✅ biz=1 |
| **非餐商户（到综）核销/结算/点评** | ❌ **不支持**，只有 storemap 模型的 1/16/22 三档 | ✅ **biz=58/59，接口齐全** |
| 外卖订单/评价 | ✅ | ✅ biz=16 |
| 到店广告 cpc 报表 | ❌ 看板无对应表 | ✅ `ad-report-getDailyDataByShopOffline` |

给九数云工单的说法：**请支持美团「服务零售」(businessId 58/59) 业务线**，它走的是 `general/auth` OAuth 授权而非 storemap，接口齐全（核销 `ddzh-tuangou-receipt-querylistbydate`、财务 `ddzhkh-finance-income-detail`、评价 `ddzh-ugc-queryshopreview`），目前贵方只覆盖了 storemap 模型的三档。

---

## 相关文档
- 抖音侧到综分析：`douyin-local-life-comprehensive-api.md`
- 抖音全量接口索引：`douyin-local-life-full-api-index.md`
- 九数云字段清单：`jiushuyun-laike-meituan-fields.md`
- 接入执行单：`../JIUSHUYUN_INGEST.md`
