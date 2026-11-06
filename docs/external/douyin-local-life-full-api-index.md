# 抖音生活服务开放平台 · 全量接口索引

> 2026-09-21 自动抓取整理。来源：developer.open-douyin.com 的 local-life 文档树
> （sitemap 四分片取全量 URL → 迭代展开侧边栏服务端渲染节点 → 逐页取 loader 接口的 openApiMeta）。
> 覆盖 **407 个接口页 + 185 篇对接文档**，815 个节点 0 失败。
>
> 每行格式：`中文接口名` | `Scope（API名）` | `HTTP 方法 + 路径` | 文档 URL
>
> 阅读须知：
> - SPI 没有 Scope，只有事件名；Webhook 的 Scope 是主 Scope 加 `.webhook` 后缀。
> - 65 个页面是方案介绍/枚举说明/SPI 纯说明，本就没有结构化元数据，标「未获取」，URL 照列。
> - **同名接口在不同行业目录下是不同 HTTP 路径但同 Scope**。例如 `life.capacity.order.query`
>   有通用 `/trade/order/query/`、餐饮 `/akte/order/query/` 等多条；`life.capacity.billing.detail`
>   有餐饮 `catering_query`、到综 `composite_query`、通用 `ledger/detailed_query` 等。
>   **选路径要看行业，不能只看 Scope。**
> - 「历史版本文档（不推荐）」81 篇单独成组，未混入主清单。
>
> 行业速查：通用接口 14 组 71 个 / 餐饮 4 方案 120 个 / 到综 6 方案 75 个 /
> 酒旅 6 方案 136 个 / 大交通 1 方案 5 个。合计 31 方案 407 接口页。
>
> 到综（非餐饮）的分析与落地建议见同目录 `douyin-local-life-comprehensive-api.md`。


### 生服免授权通用能力
- `开放平台解密` | `life.capacity.open.common_biz.crypto.decrypt.batch` | POST `/goodlife/v1/open/common_biz/crypto/decrypt/batch/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/common-capaticy/decryption-open-platform
- `开放平台解密脱敏` | `life.capacity.open.common_biz.crypto.decrypt_mask.batch` | POST `/goodlife/v1/open/common_biz/crypto/decrypt_mask/batch/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/common-capaticy/open-platform-decryption-desensitization
- `开放平台加密` | `life.capacity.open.common_biz.crypto.encrypt.batch` | POST `/goodlife/v1/open/common_biz/crypto/encrypt/batch/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/common-capaticy/open-platform-encryption

### 职人信息
- `查询商家总户下所有职人绑定信息列表` | `life.capacity.craftsman_openapi.merchat.craftsman.bind_info.all` | GET `/goodlife/v2/craftsman_openapi/merchat/craftsman/bind_info/all/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/employee-info/query-info-list
- `查询商家某职人绑定信息列表` | `life.capacity.craftsman_openapi.merchat.craftsman.bind_info.single` | GET `/goodlife/v2/craftsman_openapi/merchat/craftsman/bind_info/single/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/employee-info/query-merchant-info

### 商品发布
- `同步库存` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/stock/sync/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/batch.save
- `创建适用人群` | `life.capacity.shop` | POST `/goodlife/v1/poi/crowd/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/crowdsave
- `免审修改商品接口` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/product/free_audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/free.audit
- `编辑商品门店（目前仅支持餐饮类目商品编辑）` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/open/common/product/poi/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/goods-poi-operate
- `创建/更新多SKU商品的SKU列表` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/sku/batch_save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/goods.batch.save
- `上下架商品` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/operate
- `商品审核结果通知Webhook` | `life.capacity.goods.found.webhook` | Webhook `life_product_common_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/product-review-notice
- `商品状态变更通知Webhook` | `life.capacity.goods.found.webhook` | Webhook `life_product_status_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/product-status-notification
- `创建/更新商品接口` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/product/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/goods/save

### 团购退款
- `售后单详情查询` | `life.capacity.catering_after_sale_order` | GET `/goodlife/v1/akte/after_sale/order_detail/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/groupon-refund/after-sale-order-detail
- `发起退款` | `life.capacity.groupon.order.refund.apply` | POST `/goodlife/v1/groupon/order/refund/apply/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/groupon-refund/order-refund-apply
- `退款单列表查询` | `life.capacity.catering_after_sale_order` | GET `/goodlife/v1/akte/after_sale/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/groupon-refund/refund-list-query
- `抖音码核销后退款审批` | `life.capacity.catering_after_sale_order` | POST `/goodlife/v1/akte/after_sale/order/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/groupon-refund/system-code-refund-audit
- `抖音码退款申请通知Webhook` | `life.capacity.catering_after_sale_order.webhook` | Webhook `life_hermes_akte_after_sale_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/groupon-refund/system-code-refund-notice

### KA核销对账
- `账单详细查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/ledger/detailed_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/ka-verification/detailedquery

### 团购对账
- `验券历史查询` | `life.capacity.billing` | GET `/goodlife/v1/fulfilment/certificate/verify_record/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.billing/certificate.verifyrecord.query
- `分账明细查询` | `life.capacity.billing` | GET `/goodlife/v1/settle/ledger/query_record_by_cert/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.billing/ledger.query-record-by-cert
- `账单查询` | `life.capacity.billing` | GET `/goodlife/v1/settle/ledger/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.billing/merchantquery
- `离线分账单下载url` | `life.capacity.billing` | GET `/goodlife/v1/settle/bill/query_leger_url/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.billing/query-leger-url

### 团购核销
- `撤销核销` | `life.capacity.fulfilment` | POST `/goodlife/v1/fulfilment/certificate/cancel/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/certificate.cancel
- `券状态查询` | `life.capacity.fulfilment` | GET `/goodlife/v1/fulfilment/certificate/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/certificate.get
- `验券准备` | `life.capacity.fulfilment` | GET `/goodlife/v1/fulfilment/certificate/prepare/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/certificate.prepare
- `券状态批量查询` | `life.capacity.fulfilment` | GET `/goodlife/v1/fulfilment/certificate/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/certificate.query
- `验券` | `life.capacity.fulfilment` | POST `/goodlife/v1/fulfilment/certificate/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/certificate.verify
- `跨订单验券` | `life.capacity.fulfilment` | POST `/goodlife/v1/fulfilment/certificate/batch_verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.fulfilment/cross-order-verification

### 门店相关接口
- `能力授权&门店绑定SDK` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/auth_with_bind
- `提交门店资质亮照/修改任务` | `life.capacity.poi.claim` | POST `/goodlife/v1/poi/poi/claim/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/poi.claim
- `提交门店基础信息更新任务` | `life.capacity.poi.update` | POST `/goodlife/v1/poi/poi/update/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/poi.update

#### 门店管理
- `查询门店信息` | `life.capacity.shop` | GET `/goodlife/v1/shop/poi/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-management/shop.query
- `查询门店资质信息` | `life.capacity.shop` | GET `/goodlife/v1/poi/cert/info/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-management/store-qualification-info
- `保存人群使用条件` | `life.capacity.shop` | POST `/goodlife/v1/poi/crowd/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-management/user-condition-save

#### 门店匹配
- `查询门店匹配关系` | `life.capacity.poi.match` | GET `/goodlife/v1/poi/match/relation/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-matching/match.relationquery
- `查询门店匹配任务结果` | `life.capacity.poi.match` | GET `/goodlife/v1/poi/match/task/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-matching/match.taskquery
- `提交门店匹配任务` | `life.capacity.poi.match` | POST `/goodlife/v1/poi/match/task/submit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/store-matching/match.tasksubmit
- `查询门店任务结果` | `life.capacity.poi.task.query` | GET `/goodlife/v1/poi/task/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/life.capacity.shop/task.query

### 会员接入
- `会员信息变更（抖音->商家）SPI` | `未获取` | SPI `merchant.member.info.update` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/member/member-info-update
- `会员退会（解绑）SPI` | `未获取` | SPI `merchant.member.leave` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/member/member-leave
- `会员通手机号格式说明&国家区号拆分能力` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/member/member-mobilephone-description
- `会员入会SPI` | `未获取` | SPI `merchant.member.join` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/member/member.join.new
- `会员数据更新` | `life.capacity.member` | POST `/goodlife/v1/member/user/update/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/member/update.info

### 招商入驻
- `招商入驻-获取h5入驻链接` | `life.capacity.entrance.h5.link.get` | POST `/goodlife/v1/entrance/h5/link/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/merchant-settlement/merchant-h5-link
- `招商入驻-前置资质上传` | `life.capacity.entrance.file.upload` | POST `/goodlife/v1/entrance/file/upload/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/merchant-settlement/merchant-qualification-upload

### 订单查询
- `订单查询` | `life.capacity.order.query` | GET `/goodlife/v1/trade/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/order.query/query
- `券消息通知Webhook` | `life.capacity.order.query.webhook` | Webhook `life_trade_certificate_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/order.query/trade.certificate.notify
- `订单消息通知Webhook` | `life.capacity.order.query.webhook` | Webhook `life_trade_order_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/order.query/trade.order.notify

### 商品查询
- `批量查询sku` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/sku/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/batch-query-sku
- `查询商品品类` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/category/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/category.get
- `查询商品草稿数据` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/product/draft/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/draft.get
- `分时代金券金额查询` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/product/coupon_price/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/gold-coupon-query
- `商品审核结果同步Webhook` | `life.capacity.goods.found.webhook` | Webhook `life_product_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/goods.audit
- `查询商品线上数据` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/product/online/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/online.get
- `查询商品线上数据列表` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/product/online/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/online.query
- `资质搜索` | `life.qual.search` | POST `/goodlife/v1/account/qual/search/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/qualification-search
- `查询商品草稿数据列表` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/product/draft/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/query
- `查询商品模板接口` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/template/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/template.get

### 服务费返还对账
- `获取服务费返还账单接口` | `life.capacity.settle.rebate.query_bill` | GET `/goodlife/v1/settle/rebate/query_bill/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/rebate-reconciliation/rebate-query-bill
- `获取服务费返还明细下载链接` | `life.capacity.settle.rebate.query_record_url` | GET `/goodlife/v1/settle/rebate/query_record_url/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/rebate-reconciliation/rebate-query-record-url

### 三方码
- `发券回调` | `life.capacity.tripartite.code` | POST `/goodlife/v1/fulfilment/create/callback/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/callback
- `发券SPI` | `未获取` | SPI `fulfilment.order.tripartite_code` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/create
- `订单延期API` | `life.capacity.order.delay` | POST `/goodlife/v1/order/delay/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/order-delay-api
- `订单延期SPI` | `未获取` | SPI `life.trade.order_delay` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/order-delay-spi
- `预下单SPI` | `未获取` | SPI `trade.order.pre_create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/precreateorder
- `退款申请SPI` | `未获取` | SPI `fulfilment.order.refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/refund.apply
- `审核回调` | `life.capacity.tripartite.code` | POST `/goodlife/v1/fulfilment/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/refund.audit
- `退款信息同步SPI` | `未获取` | SPI `fulfilment.order.refund_notice` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/refund.notice
- `订单状态查询SPI` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/tripartite.code/status_query
### 到店餐饮团购解决方案

#### 餐饮门店管理（连锁门店）
- `批量认领餐饮门店` | `life.capacity.catering.merchant` | POST `/goodlife/v1/akte/merchant/claim/submit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/chain-restaurant-mgmt/bulk-restaurants-claim
- `餐饮门店认领结果查询` | `life.capacity.catering.merchant` | GET `/goodlife/v1/akte/merchant/claim/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/chain-restaurant-mgmt/restaurant-claim-result
- `门店认领任务完成通知Webhook` | `life.capacity.catering.merchant.webhook` | Webhook `life_hermes_akte_store_claim_notification` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/chain-restaurant-mgmt/store-claim-notification

#### 券资产转移
- `异步领券结果回调` | `life.capacity.asset.transfer` | POST `/goodlife/v1/fulfilment/transfer/callback/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/coupon-transfer/async-coupon-callback
- `通知送礼领券SPI` | `未获取` | SPI `cert.asset.receive_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/coupon-transfer/gift-coupon-notification
- `通知服务商加锁SPI` | `未获取` | SPI `cert.asset.lock` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/coupon-transfer/service-provider-lock
- `通知服务商解锁SPI` | `未获取` | SPI `cert.asset.unlock` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/coupon-transfer/unlock-notification-service

#### 餐饮团购退款
- `抖音码核销后退款审批` | `life.capacity.catering_after_sale_order` | POST `/goodlife/v1/akte/after_sale/order/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-refund/audit
- `抖音码退款申请通知Webhook` | `life.capacity.catering_after_sale_order.webhook` | Webhook `life_hermes_akte_after_sale_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-refund/audit_notification
- `售后单详情查询` | `life.capacity.catering_after_sale_order` | GET `/goodlife/v1/akte/after_sale/order_detail/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-refund/order_detail
- `退款单列表查询` | `life.capacity.catering_after_sale_order` | GET `/goodlife/v1/akte/after_sale/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-refund/refund-list-query

#### 餐饮订座
- `餐饮订座取消预约SPI` | `未获取` | SPI `booking.catering.cancel_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-cancel
- `查询是否可订SPI` | `未获取` | SPI `catering.order.can_buy` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-check
- `批量配置订座` | `life.capacity.catering.booking` | POST `/goodlife/v1/akte/booking/config/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-config
- `订座配置任务完成通知Webhook` | `life.capacity.catering.booking.webhook` | Webhook `life_hermes_akte_booking_config_result` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-config-notify
- `查询订座配置任务结果` | `life.capacity.catering.booking` | GET `/goodlife/v1/akte/booking/config/task/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-config-query
- `下预订单SPI` | `未获取` | SPI `booking.catering.push_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-order
- `手动接单` | `life.capacity.catering.booking` | POST `/goodlife/v1/akte/booking/order/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-order-audit
- `用户到店确认` | `life.capacity.catering.booking` | POST `/goodlife/v1/akte/booking/order/fulfillment/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-order-fulfil
- `预订单状态变更通知Webhook` | `life.capacity.catering.booking.webhook` | Webhook `life_hermes_akte_booking_status_notification` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-order-notify
- `库存变更通知` | `life.capacity.catering.booking` | POST `/goodlife/v1/akte/booking/stock_update/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-stock-notify
- `查询桌台实时库存SPI` | `未获取` | SPI `catering.booking.pull_stock` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/booking-stock-query
- `商户订座业务变更通知Webhook` | `life.capacity.catering.booking.webhook` | Webhook `life_hermes_akte_merchant_config_update` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/merchant-booking-change-notify
- `门店订座配置变更Webhook` | `life.capacity.catering.booking.webhook` | Webhook `life_hermes_akte_store_config_update` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/dining-reservation/shop-booking-config-notify

#### 餐饮评价
- `评价查询` | `life.capacity.catering.comment` | GET `/goodlife/v1/akte/comment/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/food-review/query_comment
- `查询评分` | `life.capacity.catering.comment` | GET `/goodlife/v1/akte/comment/score/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/food-review/query_score
- `回复评价` | `life.capacity.catering.comment` | POST `/goodlife/v1/akte/comment/reply/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/food-review/reply_comment

#### 商家账单明细（餐饮行业）
- `餐饮账单详情查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/catering_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/merchant-bill-detail/ledger_for_catering
- `餐饮提现记录查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/withdraw/catering_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/merchant-bill-detail/withdraw_catering_query

#### 订单查询
- `餐饮团购-订单查询` | `life.capacity.order.query` | GET `/goodlife/v1/akte/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/order-query/order_query

#### POI信息报错（连锁门店）
- `门店报错任务完成通知Webhook` | `life.capacity.poi.basic.webhook` | Webhook `life_poi_report_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-info-error/store-error-task-notice
- `查询单个门店报错任务结果` | `life.capacity.poi.basic` | POST `/goodlife/v1/poi/report/task/view/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-info-error/store-error-task-result
- `提交单个门店信息报错任务` | `life.capacity.poi.basic` | POST `/goodlife/v1/poi/report/task/create/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-info-error/store-info-error-task

#### POI匹配/申诉（连锁门店）
- `提交单个门店匹配/匹配申诉任务` | `life.capacity.poi.basic` | POST `/goodlife/v1/poi/match/task/create/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-match-appeal/store-match-appeal
- `门店匹配/匹配申诉任务完成通知Webhook` | `life.capacity.poi.basic.webhook` | Webhook `life_poi_match_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-match-appeal/store-match-notification
- `查询单个门店匹配/匹配申诉任务结果` | `life.capacity.poi.basic` | POST `/goodlife/v1/poi/match/task/view/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/poi-match-appeal/store-match-result

#### 门店申诉（连锁门店）
- `门店冒领申诉工单状态变更通知webhook` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/store-appeal/store-appeal-status-webhook
- `查询门店冒领申诉工单` | `life.capacity.poi.appeal` | POST `/goodlife/v1/poi/claim/appeal/ticket/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/store-appeal/store-complaint-query
- `提交门店冒领申诉工单` | `life.capacity.poi.appeal` | POST `/goodlife/v1/poi/claim/appeal/ticket/submit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/store-appeal/submit-appeal-order

#### 三方码发布
- `退款审核回调` | `life.capacity.tripartite.code` | POST `/goodlife/v1/fulfilment/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-group-solution/third-party-code-release/refund-audit-callback

### 餐饮买单解决方案

#### 餐饮买单交易能力
- `聚合码/点餐买单校验` | `life.capacity.paybill.trade.check` | POST `/goodlife/v1/paybill/trade/check/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-payment-solution/dining-payment-ability/aggqr-orderpay-verify
- `商家申请退款` | `life.capacity.paybill.order.refund.apply` | POST `/goodlife/v1/paybill/order/refund/apply/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-payment-solution/dining-payment-ability/merchant-refund-apply
- `买单配置开通通知Webhook` | `life.capacity.pay_bill.config.webhook` | Webhook `life_pay_bill_enable_config` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-payment-solution/dining-payment-ability/payment-config-webhook
- `买单退款申请Webhook` | `life.capacity.pay_bill.refund.audit.webhook` | Webhook `life_pay_bill_refund_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-payment-solution/dining-payment-ability/payment-refund-webhook
- `退款成功通知Webhook` | `life.capacity.pay_bill.refund.complete.webhook` | Webhook `life_pay_bill_refund_complete` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/dining-payment-solution/dining-payment-ability/refund-success-webhook

### 即送（原随心团）解决方案

#### 次卡三方码交易能力
- `通知商家即送订单创建SPI` | `未获取` | SPI `delivery_takeout.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/delivery-timecard-thirdparty-transaction/notify-merchant-order-spi

#### 订单接单
- `确认订单接口` | `life.capacity.order_confirm` | POST `/goodlife/v1/trade/buy/merchant_confirm_order/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-acceptance/confirm-order-api
- `商家确认出餐完成` | `life.capacity.order_confirm` | POST `/goodlife/v1/hermes/preparation/complete/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-acceptance/merchant-meal-complete
- `商家拒绝接单` | `life.capacity.order_confirm` | POST `/goodlife/v1/after_sale/order/merchant_reject/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-acceptance/merchant-order-refusal

#### 订单配送
- `订单配送提醒通知Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_takeout_delivery_notice` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-delivery/merchant-delivery-notice-webhook
- `自配送-回传配送信息接口` | `life.capacity.order_delivery` | POST `/goodlife/v1/fulfilment/distribution/order/sync_status/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-delivery/self-delivery-interface

#### 订单推送
- `账单确认Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_bill_ledger_create` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/bill-confirmation-webhook
- `配送状态变更消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_delivery_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/delivery-status-webhook
- `订单完成消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_takeout_order_finish` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-complete-webhook
- `订单修改消息通知Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_takeout_order_modify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-information-modification-message
- `订单支付成功Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_order_pay_success` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-payment-webhook
- `订单退款信息同步Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_refund_complete` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-refund-sync
- `订单已同意/已拒绝退款消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_refund_audit` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-refund-webhook
- `订单已拒单消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_order_merchant_refuse` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-rejection-webhook
- `订单已接单消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_order_merchant_receive` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/order-webhook
- `订单已部分退消息Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_trade_part_refund_complete` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/partial-refund-webhook
- `订单未支付关单通知Webhook` | `life.capacity.order.notice.webhook` | Webhook `life_takeout_order_pay_close` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-push/unpaid-order-notice

#### 订单查询
- `订单查询` | `life.capacity.order.query` | GET `/goodlife/v1/hermes/trade/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-query/order-inquiry

#### 订单退款
- `商家取消订单接口` | `life.capacity.order_refund` | POST `/goodlife/v1/after_sale/order/apply_refund/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-refund/merchant-cancel-order
- `商家同意/拒绝退款接口` | `life.capacity.order_refund` | POST `/goodlife/v1/after_sale/audit/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-refund/merchant-refund-interface
- `用户申请退款SPI` | `未获取` | SPI `trade.takeout.refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/order-refund/refund-spi

#### 结算对账
- `账单查询` | `life.capacity.order_billing` | GET `/goodlife/v1/settle/ledger/query_by_order/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/settlement-reconciliation/bill-query-interface
- `订单详细查询接口` | `life.capacity.order_billing_detail` | GET `/goodlife/v1/settle/ledger/detailed_query_by_order/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/settlement-reconciliation/order-detail-query

#### 门店配送设置
- `配送信息保存接口` | `life.capacity.poi.deliver_config` | POST `/goodlife/v1/hermes/delivery/delivery_info/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/delivery-info-api
- `配送信息查询` | `life.capacity.poi.deliver_config` | GET `/goodlife/v1/hermes/delivery/delivery_info/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/delivery-info-query
- `接单状态变更` | `life.capacity.poi.deliver_config` | POST `/goodlife/v1/hermes/delivery/accept_status/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/order-status-change
- `门店配送信息变更消息Webhook` | `life.capacity.poi.deliver_config.webhook` | Webhook `life_shop_deliver_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/store-delivery-update
- `门店接单状态变更消息Webhook` | `life.capacity.poi.deliver_config.webhook` | Webhook `life_shop_order_receive_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/store-order-status
- `门店设置暂停营业时间段` | `life.capacity.poi.deliver_config` | POST `/goodlife/v1/hermes/delivery/store_suspend_time/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/store-delivery-setting/store-pause-times

#### 三方码交易能力
- `通知商家即送订单创建SPI` | `未获取` | SPI `delivery_takeout.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/group-buy-solution/trading-capacity/notify-merchant-spi

### 餐饮在线点单解决方案

#### 生活服务用户信息转换
- `抖音来客open_id获取OpenAPI` | `life.capacity.open.common_biz.dylk_open_id.get` | GET `/goodlife/v1/open/common_biz/dylk_open_id/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/life-service-user-convert/douyin-openid-get

#### 商家账单明细
- `「OpenAPI」餐饮账单详情查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/catering_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/merchant-bill-details/openapi-restaurant-bill

#### 在线点单交易能力
- `「OpenAp」点单订单查询` | `life.capacity.retail.order.query` | GET `/goodlife/v1/retail/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/catering-retail-order-query
- `预计收入可用通知Webhook` | `life.capacity.trade.estimated.order.income.webhook` | Webhook `life_trade_estimated_order_income` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/estimated-order-income
- `通知外部商家创单SPI` | `未获取` | SPI `retail.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/external-merchant-order
- `查询门店取餐状态SPI` | `未获取` | SPI `life.retail.poi.query_pick_up_status` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/meal-retail-poi-pickupstatus
- `通知外部商家履约备餐状态变更Webhook` | `life.trade.meal_retail_webhook_order_status.webhook` | Webhook `retail_life_meal_order_status_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/merchant-status-change
- `通知外部商家待接单SPI` | `未获取` | SPI `retail.order.confirm_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/notify-merchants-pending
- `通知外部商家支付成功SPI` | `未获取` | SPI `retail.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/notify-payment-success
- `「OpenAPI」商家回传备餐/出餐/取餐信息` | `life.trade.meal_retail_openapi_sync_status` | POST `/goodlife/v1/retail/order/sync/status/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/openapi-meal-info
- `「OpenAPI」商家发起退款` | `life.trade.meal_retail_openapi_apply_refund` | POST `/goodlife/v1/retail/order/refund/apply/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/openapi-merchant-refund
- `「OpenAPI」商家接单/拒单` | `life.trade.meal_retail_openapi_confirm_order` | POST `/goodlife/v1/retail/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/openapi-order-acceptance
- `通知外部商家订单已关闭SPI` | `未获取` | SPI `retail.order.cancel_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/order-closed-notice
- `「OpenAPI」点单退款审核回调` | `life.capacity.retail.order.refund.audit` | POST `/goodlife/v1/retail/order/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/order-refund-callback
- `查询外部商家订单SPI` | `未获取` | SPI `retail.order.query_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/query-external-orders
- `「SPI」退款结果同步外部商家` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/refund-sync-merchant
- `点单查询电话` | `life.capacity.retail.order.query.contact` | POST `/goodlife/v1/retail/order/query/contact/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/retail-order-query-contact
- `查询订单取餐状态SPI` | `未获取` | SPI `life.retail.order.query_pick_up_status` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/retail-order-query-pickupstatus
- `用户申请取消点单SPI` | `未获取` | SPI `retail.order.audit_refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/online-order-transaction/user-cancel-order

#### 点单相关商品查询
- `加料品查询` | `life.capacity.ordering.goods.query` | GET `/goodlife/v1/goods/foodorder/affiliated/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/additive-query
- `查询团购绑定点单信息` | `life.capacity.ordering.goods.query` | GET `/goodlife/v1/goods/foodorder/couponproduct/bind/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/group-order-info
- `点单品查询` | `life.capacity.ordering.goods.query` | GET `/goodlife/v1/goods/foodorder/product/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/product-inquiry
- `查询标品模版` | `life.capacity.goods.spu.template.get` | GET `/goodlife/v2/goods/spu/template/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/product-templement
- `查询标品类目` | `life.capacity.goods.spu.category.get` | GET `/goodlife/v2/goods/spu/category/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/query-category
- `查询在线加料品列表` | `life.capacity.ordering.goods.query` | POST `/goodlife/v1/goods/foodorder/affiliated/list/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/query-online-affiliated-list
- `查询在线点单品列表` | `life.capacity.ordering.goods.query` | POST `/goodlife/v1/goods/foodorder/dish/list/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/query-online-dish-list
- `查询商品类目` | `life.capacity.goods.query` | GET `/goodlife/v1/goods/category/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/query-product-categories
- `标品查询` | `life.capacity.goods.spu.get` | GET `/goodlife/v2/goods/spu/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/spu-query
- `商品门店维度信息查询` | `life.capacity.ordering.goods.query` | GET `/goodlife/v1/goods/foodorder/poiproduct/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/store-product-info
- `加料品门店沽清查询` | `life.capacity.ordering.goods.query` | GET `/goodlife/v1/goods/foodorder/affiliated/poi_sell_out/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-query/store-sold-out-query

#### 点单相关商品发布
- `团购绑定点单标品` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/couponproduct/bind/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/group-purchase-standard
- `加料品上下架` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/affiliated/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/ingredient-management
- `加料品创建/更新` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/affiliated/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/ingredients-create-update
- `点单品上下架` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/item-listing
- `点单品创建/更新` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/product/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/order-item-crud
- `点单品更新门店` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/product/poi/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/ordering-poi-operate
- `点单商品模型介绍` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/product-model-intro
- `标品创建` | `life.capacity.goods.spu.save` | POST `/goodlife/v2/goods/spu/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/spu-create
- `标品上下架` | `life.capacity.goods.spu.operate` | POST `/goodlife/v2/goods/spu/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/spu-online-offline
- `标品审核状态通知SPI` | `未获取` | SPI `goods.spu.status_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/standard-product-audit-status
- `标品删除` | `life.capacity.goods.spu.delete` | POST `/goodlife/v2/goods/spu/delete/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/standard-product-delete
- `点单品门店上下架` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/poiproduct/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/store-product-listing
- `加料品门店沽清` | `life.capacity.ordering.goods.found` | POST `/goodlife/v1/goods/foodorder/affiliated/poi_sell_out/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/order-product-release/store-sold-out

#### 门店接单信息设置
- `点单的接单信息查询` | `life.capacity.poi.order.server` | POST `/goodlife/v1/poi/order/serve/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/store-order-settings/order-info-query
- `点单的接单信息设置` | `life.capacity.poi.order.server` | POST `/goodlife/v1/poi/order/serve/submit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/catering/online-ordering-solution/store-order-settings/order-info-settings
### 到综团购预约解决方案

#### 综合预约订单查询
- `综合预约品订单查询` | `life.capacity.comprehensive_reserve_query_order` | POST `/goodlife/v1/comprehensive/trade/query/order/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/comprehensive-order-query/integrated-order-query

#### 综合预约三方订单查询
- `查询外部订单` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/comprehensive-thirtyparty-order-query/query-external-order

#### 综合预约订单支付通知
- `支付结果通知` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/integrated-booking-payment/payment-notification

#### 综合预约订单确认接拒单
- `综合预约品确认接拒单` | `life.capacity.comprehensive_reserve_confirm_order` | POST `/goodlife/v1/comprehensive/trade/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/order-confirmation/appointment-confirmation

#### 综合预约订单创建
- `创建预约单SPI` | `未获取` | SPI `comprehensive_reserve.order.create` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/order-creation/create-appointment

#### 商品预约类价量态操作
- `创建or更新到综接待单元库存sku` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/comprehensive/reception/stock/sku/upsert/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/create-update-inventory-sku
- `库存拉取SPI` | `未获取` | SPI `comprehensive_spot.reception.pull_stock` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/inventory-pulling
- `到综接待单元库存更新通知` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/comprehensive/reception/trigger/stock/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/inventory-update-notice
- `查询ktv套餐` | `life.capacity.goods_booking_ari_operate` | GET `/goodlife/v1/goods/open/comprehensive/booking/package/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/package-get
- `创建/更新/下架套餐/更新套餐&价目表管理渠道` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/open/comprehensive/booking/package/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/package-operate
- `查询价目表` | `life.capacity.goods_booking_ari_operate` | GET `/goodlife/v1/goods/open/comprehensive/booking/price_list/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/price-list-get
- `保存价目表(全量覆盖)` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/open/comprehensive/booking/price_list/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/price-list-save
- `到综接待单元保存实时库存` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/comprehensive/reception/save/stock/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/real-time-inventory-save
- `创建/更新时段库存（全量覆盖）` | `life.capacity.goods_booking_ari_operate` | POST `/goodlife/v1/goods/open/comprehensive/booking/room/time_stock/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/time-stock-save
- `查询时段库存` | `life.capacity.goods_booking_ari_operate` | GET `/goodlife/v1/goods/open/comprehensive/booking/room/time_stock/get/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/group-buy-reservation/product-reservation-ops/time-stofck-get

### 到综团购解决方案
- `到综团购对接方案介绍` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/group-buying-integration

#### KA核销对账(综合定制接口）
- `综合账单详情查询（连锁）` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/composite_query_new/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/ka-reconciliation/bill-composite-query-new
- `综合账单详情查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/composite_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/ka-reconciliation/composite_query
- `综合提现记录（连锁升级）` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/withdraw/composite_query_new/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/ka-reconciliation/withdraw-composite-query-new
- `综合提现记录查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/withdraw/composite_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/in-store-industry/ka-reconciliation/withdraw_composite_query

### 线索管理解决方案

#### 线索查询
- `线索查询` | `life.crm.clue.query` | GET `/goodlife/v1/open_api/crm/clue/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/lead-mgmt/lead-inquiry/clue-query

### 零售即送解决方案

#### 门店SKU库存管理
- `配送商品门店库存查询` | `life.capacity.goods.retailorder.poistock.detail` | POST `/goodlife/v1/goods/retailorder/poistock/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/poi-sku-stock-management/retail-poi-stock-detail
- `配送商品门店库存管理接口` | `life.capacity.goods.retailorder.poistock.save` | POST `/goodlife/v1/goods/retailorder/poistock/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/poi-sku-stock-management/retail-poi-stock-save

#### 零售即送配送能力管理
- `用户取消取件SPI` | `未获取` | SPI `life.comprehensive.cancel_pickup` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-delivery-pickup/cancel-pickup
- `待验收退款SPI` | `未获取` | SPI `life.comprehensive.pending_accept_refund` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-delivery-pickup/pending-accept-refund
- `同步取件信息SPI` | `未获取` | SPI `life.comprehensive.sync_pickup_info` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-delivery-pickup/sync-pickup-info

#### 零售点单品创建
- `配送商品创建编辑` | `life.capacity.goods.retailorder.product.save` | POST `/goodlife/v1/goods/retailorder/product/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-store-sku-create/delivery-product-create-edit
- `查询配送商品线上数据` | `life.capacity.goods.retailorder.product.detail` | GET `/goodlife/v1/goods/retailorder/product/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-store-sku-create/retail-order-product-detail
- `配送商品列表查询` | `life.capacity.goods.retailorder.list` | POST `/goodlife/v1/goods/retailorder/list/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-store-sku-create/retail-order-product-list
- `上下架配送商品` | `life.capacity.goods.retailorder.product.operate` | POST `/goodlife/v1/goods/retailorder/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-store-sku-create/retail-order-product-operate
- `配送商品更新门店` | `life.capacity.goods.retailorder.product.poi.operate` | POST `/goodlife/v1/goods/retailorder/product/poi/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/retail-instant-delivery-solution/retail-store-sku-create/retail-product-poi-operate

### 综合到店提货解决方案

#### 提货券抖音码交易能力
- `券状态批量查询` | `life.capacity.compre_retail_certificate_query` | GET `/goodlife/v1/compre_retail/fulfilment/certificate/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/coupon-code-trading/batch-coupon-status
- `提货券门店变更通知Webhook` | `life.capacity.retail.order.poi.chagne.webhook` | Webhook `life_retail_comp_poi_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/coupon-code-trading/pick-up-poi-change
- `撤销核销接口` | `life.capacity.compre_retail_certificate_cancel` | POST `/goodlife/v1/compre_retail/fulfilment/certificate/cancel/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/coupon-code-trading/revoke-write-off-api
- `验券接口` | `life.capacity.compre_retail_certificate_verify` | POST `/goodlife/v1/compre_retail/fulfilment/certificate/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/coupon-code-trading/voucher-interface
- `验券准备接口` | `life.capacity.compre_retail_certificate_prepare` | GET `/goodlife/v1/compre_retail/fulfilment/certificate/prepare/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/coupon-code-trading/voucher-verification-api

#### 商品查询
- `查询门店库存` | `life.capacity.goods.query` | POST `/goodlife/v1/goods/pickupcoupon/poistock/detail/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/product-inquiry/store-inventory-query

#### 商品发布
- `创建提货券商品` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/product-release/create-pickup-coupon
- `保存门店库存` | `life.capacity.goods.found` | POST `/goodlife/v1/goods/pickupcoupon/poistock/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/product-release/store-inventory-save

#### 提货券三方码交易能力
- `发券回调接口` | `life.capacity.compre_retail_gen_code_callback` | POST `/goodlife/v1/compre_retail/fulfilment/create/callback/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/coupon-callback-api
- `发券SPI` | `未获取` | SPI `comprehensive_retail.order.gen_third_code` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/coupon-distribution
- `券状态批量查询` | `life.capacity.compre_retail_certificate_query` | GET `/goodlife/v1/compre_retail/fulfilment/certificate/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/coupon-status-query
- `验券接口` | `life.capacity.compre_retail_certificate_verify` | POST `/goodlife/v1/compre_retail/fulfilment/certificate/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/coupon-verification
- `通知外部商家创单SPI` | `未获取` | SPI `comprehensive_retail.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/merchant-order-creation
- `商家退款申请SPI` | `未获取` | SPI `comprehensive_retail.order.refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/merchant-refund
- `通知外部商家取消订单SPI` | `未获取` | SPI `comprehensive_retail.order.cancel_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/notify-cancel-order
- `通知外部商家支付成功SPI` | `未获取` | SPI `comprehensive_retail.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/notify-payment-success
- `提货券门店变更通知Webhook` | `life.capacity.retail.order.poi.chagne.webhook` | Webhook `life_retail_comp_poi_change` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/pick-up-poi-change
- `退款审核回调接口` | `life.capacity.comprehensive_retail_refund_audit` | POST `/goodlife/v1/compre_retail/order/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/refund-callback-api
- `退款结果同步外部商家SPI` | `未获取` | SPI `comprehensive_retail.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/refund-results-sync
- `撤销核销` | `life.capacity.compre_retail_certificate_cancel` | POST `/goodlife/v1/compre_retail/fulfilment/certificate/cancel/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/store-pickup/voucher-code-trading/revoke-write-off

### 丽人/汽车/家居家电交易上翻解决方案

#### 国补报销
- `国补报销单审核结果查询` | `life.capacity.homed.trade.govsubsidy.auditquery` | GET `/goodlife/v1/homed/trade/govsubsidy/auditquery/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/gov-subsidy-ability/gov-subsidy-audit-query
- `SN绑定` | `life.capacity.homed.trade.govsubsidy.snbind` | POST `/goodlife/v1/homed/trade/govsubsidy/snbind/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/gov-subsidy-ability/gov-subsidy-snbind
- `国补报销单新增或修改` | `life.capacity.homed.trade.govsubsidy.upsert` | POST `/goodlife/v1/homed/trade/govsubsidy/upsert/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/gov-subsidy-ability/gov-subsidy-upsert
- `发票PDF链接转图片链接工具` | `life.capacity.homed.trade.pdf.toimage` | POST `/goodlife/v1/homed/trade/pdf/toimage/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/gov-subsidy-ability/pdf-to-image
- `SN绑定结果消息通知Webhook` | `life.capacity.homed.trade.govsubsidy.snresult` | Webhook `life_capacity_homed_trade_govsubsidy_snresult` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/gov-subsidy-ability/sn-binding-result-webhook

#### 上翻激励归因
- `线索归因查询` | `life.capacity.homed.trade.cluesource.query` | POST `/goodlife/v1/homed/trade/cluesource/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/incentive-attribution-ability/clue-source-query
- `订单归因查询` | `life.capacity.homed.trade.ordersource.query` | GET `/goodlife/v1/homed/trade/ordersource/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/incentive-attribution-ability/order-source-query

#### 买单收银
- `发起收银` | `life.capacity.homed.trade.payment.order.create` | POST `/goodlife/v1/homed/trade/payment/order/create/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/create-payment-order
- `售后退款审核结果消息通知Webhook` | `life.capacity.homed.trade.after.audit.notify` | Webhook `life_capacity_homed_trade_after_audit_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-aftersale-audit-notify
- `商家发起退款` | `life.capacity.homed.trade.payment.after.sale.merchant_apply` | POST `/goodlife/v1/homed/trade/payment/after/sale/merchant_apply/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-aftersale-merchant-apply
- `商家审核用户退款` | `life.capacity.homed.trade.payment.after.sale.merchant_audit` | POST `/goodlife/v1/homed/trade/payment/after/sale/merchant_audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-aftersale-merchant-audit
- `售后订单详情查询` | `life.capacity.homed.trade.aftersale.order.query` | GET `/goodlife/v1/homed/trade/aftersale/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-aftersale-order-query
- `售后订单列表查询` | `life.capacity.homed.trade.aftersale.order.querylist` | POST `/goodlife/v1/homed/trade/aftersale/order/querylist/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-aftersale-order-querylist
- `用户申请退款消息通知Webhook` | `life.capacity.homed.trade.apply.after.notify` | Webhook `life_capacity_homed_trade_apply_after_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-apply-aftersale-notify
- `取消订单` | `life.capacity.homed.trade.payment.order.cancel` | POST `/goodlife/v1/homed/trade/payment/order/cancel/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-cancel-order
- `订单取消消息通知Webhook` | `life.capacity.homed.trade.order.cancel.notify.webhook` | Webhook `life_capacity_homed_trade_order_cancel_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-order-cancel-notify
- `订单支付成功消息通知Webhook` | `life.capacity.homed.trade.payment.order.pay.notify.webhook` | Webhook `life_capacity_homed_trade_order_pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-order-pay-notify
- `买单订单列表（含加密实名信息）查询` | `life.capacity.homed.trade.payment.order.user_info.query` | POST `/goodlife/v1/homed/trade/payment/order/user_info/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-order-userinfo-query
- `买单订单列表查询` | `life.capacity.homed.trade.payment.order.query` | POST `/goodlife/v1/homed/trade/payment/order/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-payment-order-query
- `买单订单详情查询` | `life.capacity.homed.trade.payment.order.detail.query` | POST `/goodlife/v1/homed/trade/payment/order/detail/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-payment-orderdetail-query
- `买单门店列表查询` | `life.capacity.homed.poi.payment.query` | POST `/goodlife/v1/homed/poi/payment/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-poi-payment-query
- `退款成功消息通知Webhook` | `life.capacity.homed.trade.after.sale.notify` | Webhook `life_capacity_homed_trade_after_sale_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-trade-aftersale-notify
- `【SPI】生成订单并冻结优惠` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-trade-order-createandfreeze
- `【SPI】撤销订单并解冻优惠` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/comprehensive/transaction-upscaling-solution/payment-ability/homed-trade-order-repealandunfreeze
### 酒店行业日历房解决方案

#### 代理商-酒店静态信息自助获取
- `自助匹配抖音酒店信息（仅限代理商）` | `life.capacity.trip_hotel_scroll` | POST `/goodlife/v1/trip/hotel/poi/scroll/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/agent-hotel-info/tiktok-hotel-info

#### 日历房交易正向
- `确认接单接口` | `life.capacity.trip_trade_order` | POST `/goodlife/v1/trip/trade/hotel/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-house-trade/confirm-order-api
- `酒店创建订单` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-house-trade/hotel-booking
- `酒店修改订单SPI` | `未获取` | SPI `hotel_spot.order.modify_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-house-trade/hotel-modify-order
- `酒店支付结果通知SPI` | `未获取` | SPI `hotel_spot.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-house-trade/hotel-payment-notice
- `酒店日历房可订检查SPI` | `未获取` | SPI `hotel_spot.order.can_buy` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-house-trade/hotel-room-availability

#### 日历房上下架能力
- `商品上下架&删除接口` | `life.capacity.trip_product_operate` | POST `/goodlife/v1/trip/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-listing/product-status-interface

#### 日历房交易逆向
- `酒店取消订单SPI` | `未获取` | SPI `hotel_spot.order.cancel_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-trading-reverse/hotel-order-cancel
- `订单退款结果通知SPI` | `未获取` | SPI `hotel_spot.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-trading-reverse/hotel-refund-result
- `售后审核结果返回` | `life.capacity.trip_trade_after_sale` | POST `/goodlife/v1/trip/trade/hotel/cancel/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/calendar-trading-reverse/order-cancellation-api

#### 入住/离店状态同步能力
- `订单入住审核接口` | `life.capacity.trip_notify_booking` | POST `/goodlife/v1/trip/trade/hotel/booking/audit/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/check-in-out-sync/order-check-in-audit
- `酒店日历房常见枚举值列表` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/enum-list
- `酒店账单详情查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/detail/trip/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-bill-details-enquiry

#### 酒店静态信息匹配/创建/更新能力
- `酒店静态信息接口` | `life.capacity.trip_hotel_push` | POST `/goodlife/v1/trip/hotel/info/match/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-info-mgmt/hotel-info-api
- `酒店静态信息处理结果推送Webhook` | `life.capacity.trip_hotel_push.webhook` | Webhook `life_hotel_audit_result` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-info-mgmt/hotel-info-push
- `酒店静态信息处理状态查询` | `life.capacity.trip_hotel_push` | POST `/goodlife/v1/trip/hotel/status/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-info-mgmt/hotel-info-status

#### 酒店静态信息自助获取能力
- `自助匹配酒店信息查询接口` | `life.capacity.trip_hotel_pull` | POST `/goodlife/v1/trip/hotel/poi/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-info-retrieval/hotel-info-api
- `预估结算价查询` | `life.capacity.settle.bill.detail.ledger_income_by_order` | GET `/goodlife/v1/settle/bill/detail/ledger_income_by_order/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-ledger-income-enquiry

#### 酒店会员管理
- `酒店注销会员` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/logout/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/hotel-member-cancellation
- `会员信息变更接口通知` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/change/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/member-info-update
- `酒旅会员-抖音请求三方系统发送用户退会通知SPI` | `未获取` | SPI `merchant.hotel_member.leave.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/membership-withdrawal-notice
- `酒旅会员-抖音请求三方系统发送用户查询通知SPI` | `未获取` | SPI `merchant.hotel_member.query.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/tiktok-membership-query
- `抖音请求三方系统发送用户入会通知SPI` | `未获取` | SPI `merchant.hotel_member.join.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/tiktok-user-notification
- `更新酒店会员数据` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/update/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/hotel-membership/update-hotel-member-data

#### 售卖房型创建/更新能力
- `售卖房型静态信息接口` | `life.capacity.trip_sale_room_pull` | POST `/goodlife/v1/trip/hotel/rateplan/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/house-type-update/static-room-info-api

#### 房价/房态/房量更新
- `房价推送接口` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/price/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/housing-updates/house-price-interface
- `日历房价库变更增量通知接口` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/ari/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/housing-updates/price-change-notification
- `房量房态推送接口` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/stock/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/housing-updates/room-status-api
- `价量态接口异步失败通知Webhook` | `life.capacity.trip_hotel_ari_pull.webhook` | Webhook `life_hotel_async_ari_result` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/housing-updates/webhook-async-failure

#### 营销价量态拉取
- `营销价库拉取接口（价格模式SPI）` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-data-pull/marketing-price-spi

#### 营销库存推送
- `营销库存推送接口` | `life.capacity.trip_hotel_promotion_inventory` | POST `/goodlife/v1/trip/promotion/inventory/push/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-inventory/marketing-inventory-api

#### 营销规则价格模式推送
- `营销规则接口（价格模式）` | `life.capacity.trip_hotel_price_rule_promotion_push` | POST `/goodlife/v1/trip/promotion/price/rule/push/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-price-push/marketing-rule-api

#### 营销查询
- `营销查询接口` | `life.capacity.trip_hotel_promotion_query` | POST `/goodlife/v1/trip/promotion_info/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-query/marketing-query-api

#### 营销退出报名
- `营销退出报名接口` | `life.capacity.trip_hotel_promotion_exit` | POST `/goodlife/v1/trip/promotion/exit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-quit-sign-up/marketing-withdrawal-api

#### 营销规则价格推送
- `营销价格推送接口（价格模式）` | `life.capacity.trip_hotel_promotion_price` | POST `/goodlife/v1/trip/promotion/price/push/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-rule-price-push/marketing-price-api

#### 营销规则推送
- `营销规则接口` | `life.capacity.trip_hotel_promotion_push` | POST `/goodlife/v1/trip/promotion/push/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/marketing-rule-push/marketing-rules-api

#### 日历房线上开票
- `酒店日历房线上开票SPI` | `未获取` | SPI `hotel_spot.invoice.invoice_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/online-ticketing/hotel-room-billing
- `开票结果通知` | `life.capacity.trip_hotel_invoice` | POST `/goodlife/v1/trip/hotel/invoice/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/online-ticketing/invoice-results-notice

#### 物理房型上下架能力
- `物理房型上下架&删除接口` | `life.capacity.trip_physical_room_operate` | POST `/goodlife/v1/trip/physical_room/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/physical-room-capability/room-type-interface

#### 主动拉取价量态
- `价量态拉取接口SPI` | `未获取` | SPI `hotel_spot.trip.hotel_ari_cron_pull` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/price-volume-pull/price-volume-interface

#### 物理房型静态信息自助获取能力
- `自助匹配物理房型信息查询接口` | `life.capacity.trip_physical_room_pull` | POST `/goodlife/v1/trip/physical_room/search/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/room-info-access/room-info-query-api
- `物理房型静态信息查询` | `life.capacity.trip_physical_room_push` | POST `/goodlife/v1/trip/physical_room/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/room-info-access/room-static-info-query

#### 物理房型静态信息匹配/创建/更新
- `日历房套餐审核结果推送Webhook` | `life.capacity.trip_sale_room_pull.webhook` | Webhook `life_hotel_group_audit_result` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/room-info-ops/calendar-room-package-audit
- `物理房型信息处理结果推送Webhook` | `life.capacity.trip_physical_room_push.webhook` | Webhook `life_hotel_physical_room_audit_result` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/room-info-ops/room-info-processing
- `物理房型静态信息接口` | `life.capacity.trip_physical_room_push` | POST `/goodlife/v1/trip/physical_room/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/calendarroom/room-info-ops/room-static-info-api
- `酒店通用错误码` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/general-error-codes

### 酒店行业新预售券解决方案

#### 住宿预售券交易正向
- `可订检查SPI` | `未获取` | SPI `hotel_spot.order.can_buy` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/bookable-check
- `创建预约` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/create-booking-order
- `创建预售订单SPI` | `未获取` | SPI `hotel_spot.order.create_presale_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/create-pre-sale-order
- `酒店预约修改订单` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/hotel-modify-order
- `正向交易流程` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/hoteltradedesc
- `确认接单接口` | `life.capacity.trip_trade_order` | POST `/goodlife/v1/trip/trade/hotel/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/order-confirmation-api
- `支付结果通知SPI` | `未获取` | SPI `hotel_spot.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/accommodation-voucher-trade/paynotice
- `酒店账单详情查询` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/bill/detail/trip/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-bill-details-enquiry

#### 酒店静态信息自助获取
- `自助匹配酒店信息查询接口` | `life.capacity.trip_hotel_pull` | POST `/goodlife/v1/trip/hotel/poi/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-info-fetch/hotel-info-query

#### 酒店静态信息匹配/创建/更新
- `酒店静态信息处理状态查询` | `life.capacity.trip_hotel_push` | POST `/goodlife/v1/trip/hotel/status/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-info-mgmt/hotel-info-status-query
- `酒店静态信息处理结果推送（webhook）` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-info-mgmt/hotel-info-webhook
- `酒店静态信息接口` | `life.capacity.trip_hotel_push` | POST `/goodlife/v1/trip/hotel/info/match/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-info-mgmt/hotel-static-info

#### 酒店会员管理
- `酒店注销会员` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/logout/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/hotel-member-cancellation
- `会员信息变更通知` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/change/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/member-info-change
- `酒旅会员-抖音请求三方系统发送用户入会通知SPI` | `未获取` | SPI `merchant.hotel_member.join.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/send-membership-notice
- `抖音请求三方系统发送用户查询通知` | `未获取` | `merchant.hotel_member.query.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/tiktok-member-query
- `抖音请求三方系统发送用户退会通知SPI` | `未获取` | SPI `merchant.hotel_member.leave.new` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/travel-membership-notice
- `更新酒店会员数据` | `life.capacity.hotel.member` | POST `/goodlife/v1/member/hotel/user/update/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-membership/update-hotel-data

#### 酒旅商品上架和下架信息推送
- `商品上下架&删除` | `life.capacity.trip_product_operate` | POST `/goodlife/v1/trip/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-travel-info-push/product-shelf-delete

#### 住宿预售券创建和更新
- `创建/更新预售券` | `life.capacity.trip_presale_push` | POST `/goodlife/v1/trip/hotel/savepresale/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-voucher-mgmt/create-update-coupon
- `创建/更新预定商品` | `life.capacity.trip_presale_push` | POST `/goodlife/v1/trip/hotel/presale/rateplan/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-voucher-mgmt/create-update-products
- `预售券审核结果通知` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-voucher-mgmt/presale-ticket-review
- `酒店行业新预售券常见枚举值` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/hotel-vouchers

#### 房价/房态/房量更新
- `房价推送接口` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/price/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/housing-update/house-price-api
- `价量态变更通知接口（OpenAPI）` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/ari/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/housing-update/presale-price-notification
- `房量房态推送接口` | `life.capacity.trip_hotel_ari_pull` | POST `/goodlife/v1/trip/hotel/stock/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/housing-update/room-status-push-api

#### 预售券线上开票
- `开票结果通知` | `life.capacity.trip_hotel_invoice` | POST `/goodlife/v1/trip/hotel/invoice/notify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/presale-ticketing-online/invoice-result-notice
- `线上开票` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/presale-ticketing-online/online-billing

#### 主动拉取价量态
- `价量态拉取接口` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/pull-price-volume/price-volume-interface

#### 住宿预售券交易逆向
- `售后审核结果返回` | `life.capacity.trip_trade_after_sale` | POST `/goodlife/v1/trip/trade/hotel/cancel/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/reverse-hotel-voucher/callback-cancellation
- `酒店取消订单SPI` | `未获取` | SPI `hotel_spot.order.cancel_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/reverse-hotel-voucher/cancel-hotel-order
- `订单取消退款通知SPI` | `未获取` | SPI `hotel_spot.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/reverse-hotel-voucher/refund-notification

#### 物理房型静态信息自助获取
- `自助匹配物理房型信息查询接口` | `life.capacity.trip_physical_room_pull` | POST `/goodlife/v1/trip/physical_room/search/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/room-info-fetch/self-match-room-query

#### 物理房型静态信息匹配/创建/更新
- `物理房型静态信息查询` | `life.capacity.trip_physical_room_push` | POST `/goodlife/v1/trip/physical_room/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/room-info-ops/room-static-info
- `物理房型静态信息接口` | `life.capacity.trip_physical_room_push` | POST `/goodlife/v1/trip/physical_room/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/room-info-ops/room-type-info-api
- `物理房型信息处理结果推送` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/room-info-ops/room-type-push

#### 物理房型上下架
- `物理房型上下架&删除接口` | `life.capacity.trip_physical_room_operate` | POST `/goodlife/v1/trip/physical_room/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/presale/room-listing-mgmt/room-type-interface

### 景区团购三方码方案

#### 交易履约
- `景区团购发码回调V2` | `life.capacity.scenic_promissory_callback_code` | POST `/goodlife/v1/trip/scenic/groupon/certificate/callback/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-performance/scenic-group-code-v2
- `景区团购发码V2SPI` | `未获取` | SPI `scenic_spot.order.promissory_gen_code` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-performance/scenic-group-v2
- `景区核销` | `life.capacity.trip_certificate_verify` | POST `/goodlife/v1/trip/certificate/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-performance/scenic-spot-verification

#### 交易正向
- `景区团购预下单接口V2SPI` | `未获取` | SPI `scenic_spot.order.promissory_create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-positive/scenic-group-preorder-v2

#### 交易逆向
- `景区退款结果通知` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-reverse/scenic-refund-notice
- `景区退款审核` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-reverse/scenic-refund-review
- `景区回调审核退款` | `life.capacity.trip_order_after_sale_refund_audit` | POST `/goodlife/v1/trip/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-group-code-plan/transaction-reverse/scenic-refund-review-openapi

### 景区行业日历票解决方案

#### 日历票价格库存属性发布
- `批量拉取价格库存SPI` | `未获取` | SPI `scenic.goods.pull_ticket_calendar` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/calendar-price-stock/batch-price-inventory
- `日历票价格库存属性批量发布` | `life.capacity.trip_ticket_calendar_found` | POST `/goodlife/v1/trip/ticket_calendar/batch_save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/calendar-price-stock/calendar-price-stock-release
- `日历票价格库存属性发布` | `life.capacity.trip_ticket_calendar_found` | POST `/goodlife/v1/trip/ticket_calendar/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/calendar-price-stock/calendar-ticket-attributes

#### 景区创建订单
- `创建订单SPI` | `未获取` | SPI `scenic_spot.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/create-order/trip-order-create
- `行政区划列表` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/districts-list

#### 景区取消订单通知
- `通知取消订单SPI` | `未获取` | SPI `scenic_spot.order.cancel_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/order-cancellation-notice/trip-order-cancel-notify

#### 景区确认订单
- `确认订单` | `life.capacity.trip_order_confirm` | POST `/goodlife/v1/trip/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/order-confirmation/confirm-order

#### 景区订单状态查询
- `景区订单状态查询SPI` | `未获取` | SPI `scenic_spot.order.query_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/order-status-query/get-third-order

#### 景区支付通知
- `通知支付结果SPI` | `未获取` | SPI `scenic_spot.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/pay-notice/trip-order-pay-notify

#### 景区退款审核
- `申请退款SPI` | `未获取` | SPI `scenic_spot.order.refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/refund-audit/trip-order-after-sale-refund-apply

#### 景区退款结果通知
- `通知退款结果SPI` | `未获取` | SPI `scenic_spot.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/refund-notification/after-sale-refund-notify

#### 景区预订信息校验
- `预订信息校验SPI` | `未获取` | SPI `scenic_spot.order.can_buy` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/reservation-check/booking-info-check
- `景区日历票商品类目` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/scenic-calendar-ticket

#### 景区回调审核退款
- `回调审核退款` | `life.capacity.trip_order_after_sale_refund_audit` | POST `/goodlife/v1/trip/refund/audit/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/scenic-refund-review/callback-refund-audit

#### 门店管理
- `保存人群适用条件` | `life.capacity.shop` | POST `/goodlife/v1/poi/crowd/save/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/store-mgmt/save-crowd-conditions

#### 三方码发布
- `发券回调接口` | `life.capacity.tripartite.code` | POST `/goodlife/v1/fulfilment/create/callback/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/thirdparty-code-release/coupon-callback-api
- `发放凭证SPI` | `未获取` | SPI `fulfilment.order.tripartite_code` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/thirdparty-code-release/third-party-coupon-spi

#### 景区履约验券
- `履约验券` | `life.capacity.trip_certificate_verify` | POST `/goodlife/v1/trip/certificate/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/scenic-solution/ticket-verification/coupon-verification

### 度假行业解决方案

#### 日历品

##### 旅行社日历品行业交易创建订单
- `旅行社日历品行业交易创建订单SPI` | `未获取` | SPI `travel_spot.order.calendar_create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/create-order/travel-calendar-trade

##### 旅行社日历品行业交易支付结果通知
- `旅行社日历品行业交易支付结果通知` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/payment-result-notice/travel-payment-notice

##### 旅行社日历品行业交易退款成功通知
- `日历品行业交易订单退款结果通知SPI` | `未获取` | SPI `travel_spot.order.calendar_refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/refund-success/calendar-trade-refund-notice

##### 旅行社日历品操作
- `日历商品操作上下架` | `life.capacity.trip_travel_calendar_product_operate` | POST `/goodlife/v1/trip/trade/travelagency/product_operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-agency-ops/calendar-product-management
- `商品审核结果通知（webhook）` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-agency-ops/product-audit-notification

##### 旅行社poi查询
- `查询订单商品快照的poi` | `life.capacity.trip_travel_agency_poi_query` | POST `/goodlife/v1/trip/trade/travelagency/order/poi/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-agency-poi/query-order-snapshot

##### 旅行社日历品行业交易商家接单
- `旅行社日历套餐接单` | `life.capacity.travel.calendar.confirm_order` | POST `/goodlife/v1/trip/trade/travelagency/calendar/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-biz/travel-agency-booking

##### 旅行社日历品-订单取消通知
- `旅行社日历品订单取消SPI` | `未获取` | SPI `travel_spot.order.calendar_cancel_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-cancel/travel-calendar-cancellation

##### 旅行社日历品行业交易可定检查
- `可定检查SPI` | `未获取` | SPI `travel_spot.order.calendar_can_buy` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-check/spi-inspection

##### 旅行社日历品价量态操作
- `日历套餐价库信息拉取更新SPI` | `未获取` | SPI `travel_spot.product.pull_ari` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-ops/calendar-price-update
- `日历品商品价量态信息查询` | `life.capacity.trip_travel_calendar_product_ari` | POST `/goodlife/v1/trip/trade/travelagency/calendar_product/list_calendar_ari/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-ops/calendar-product-query
- `旅行社日历品价量态信息同步` | `life.capacity.trip_travel_calendar_product_ari` | POST `/goodlife/v1/trip/trade/travelagency/calendar_product/save/calendar/ari/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-calendar-ops/travel-agency-info

##### 酒旅-旅行社支付结果通知
- `通知支付结果（SPI）` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/calendar-product/travel-payment-notice/payment-notification

#### KA核销对账
- `按商户查询分账明细` | `life.capacity.billing.detail` | GET `/goodlife/v1/settle/ledger/detailed_query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/ka-verification/merchant-split-details

#### 预售券

##### 旅行社POI查询
- `查询订单商品快照的poi` | `life.capacity.trip_travel_agency_poi_query` | POST `/goodlife/v1/trip/trade/travelagency/order/poi/query/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/poi-query/query-order-snapshot

##### 酒旅旅行社-预售券价量态操作
- `商家获取商品价量态信息` | `life.capacity.trip_travel_presale_ari_operate` | POST `/goodlife/v1/trip/trade/travelagency/presale_coupon/query/ari/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/presale-coupon-ops/merchant-product-info
- `预售券价量态-更新预定商品日历库存` | `life.capacity.trip_travel_presale_ari_operate` | POST `/goodlife/v1/trip/trade/travelagency/presale_coupon/save/book_calendar_stock/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/presale-coupon-ops/ta_presale_coupon_update_book_calendar_stock
- `更新预售券价格库存及其加价规则` | `life.capacity.trip_travel_presale_ari_operate` | POST `/goodlife/v1/trip/trade/travelagency/presale_coupon/save/presale/ari/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/presale-coupon-ops/update-presale-rules

##### 酒旅商品上架和下架信息推送
- `商品审核结果通知Webhook` | `life.capacity.trip_travel_presale_ari_operate.webhook` | Webhook `life_trip_travel_agency_presale_online_offline_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/product-on-off-push/product_status_notify
- `商品上下架` | `life.capacity.trip_product_operate` | POST `/goodlife/v1/trip/product/operate/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/product-on-off-push/ta_presale_coupon_product_operate

##### 旅行社确认
- `旅行社交易确认接单接口` | `life.capacity.trip_travel_confirm` | POST `/goodlife/v1/trip/trade/travelagency/order/confirm/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-agency-confirm/travel-order-confirm-api

##### 酒旅-旅行社订单取消通知
- `预售券订单取消通知SPI` | `未获取` | SPI `travel_spot.order.cancel_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-cancel-notice/ta_presale_coupon_order_cancel

##### 酒旅-旅行社创建订单
- `创建加项订单SPI` | `未获取` | SPI `life.travel.order.create_addition_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-order-creation/create-addition-order
- `预售券创建预约订单SPI` | `未获取` | SPI `travel_spot.order.create_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-order-creation/ta_presale_coupon_create_book_order

##### 酒旅-旅行社支付结果通知
- `预售券支付通知SPI` | `未获取` | SPI `travel_spot.order.pay_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-payment-notice/ta_presale_coupon_pay_notify

##### 酒旅-旅行社创建预售订单
- `预售券创建预售订单SPI` | `未获取` | SPI `travel_spot.order.create_presale_order` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-presale-orders/ta_presale_coupon_create_presale_order

##### 酒旅-旅行社退款结果通知
- `预售券退款通知SPI` | `未获取` | SPI `travel_spot.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/presale_coupon/travel-refund-notice/ta_presale_coupon_refund_notify
- `旅行社直连相关枚举&常见错误码` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/JiuLv/vacation/travel-agency-enums
### 航司解决方案

#### 三方码
- `发券` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/airline/airline-solutions/third-party-code/coupon-issue
- `履约验券` | `life.capacity.trip_traffic_cert_verify` | POST `/goodlife/v1/trip/certificate/traffic/verify/` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/airline/airline-solutions/third-party-code/coupon-verification
- `发码回调` | 未获取 | 未获取 | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/airline/airline-solutions/third-party-code/voucher-callback-api

#### 大交通交易退款
- `申请退款SPI` | `未获取` | SPI `traffic.order.refund_apply` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/airline/airline-solutions/transport-refund/refund-application
- `通知退款结果SPI` | `未获取` | SPI `traffic.order.refund_notify` | https://developer.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/airline/airline-solutions/transport-refund/refund-notification