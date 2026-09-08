# 云老板团购接入 —— 搁置记录与调查结果（2026-09-08）

> 状态：**搁置**。本文是下次续做的唯一入口：搁置原因、已查清的事实、已谈定的商务、未决问题、重启时的方案与第一步。
> 原先写在 BossIP 仓库的 `docs/yunlaoban-groupbuy-integration.md` 已并入本文并删除（BossIP 产品容器 09-07 已停，授权中心在 openbox）。
> 密钥（clientId 的 secret、后台密码）不在任何文档里；重启时由用户放进环境变量。

---

## 1. 为什么搁置

1. **云老板没有"发布商品券"接口。** 用户的目标是"绑定门店 → 在授权中心用表单发布商品券，模型可引导"；而云老板 OpenAPI 只有：授权链接、门店建档/绑 ID/列表、商户余额、查询门店已上架团购商品、验券、核销、撤销、核销记录。能"绑门店、看券、核销券"，不能"发新券"。
2. 云老板向抖音申请的授权范围里其实含"商品发布"权限（授权链接 `permission_keys=1,2,3,6,10,11,12,14,15,16`），只是没开放成接口。要全自动发布，需要云老板再开放抖音"商品创建/上下架"接口（美团同理）——这是一条待谈的商务，用户决定先放一放。
3. 其他事项（视频发布、桌面舰队）优先级更高。

## 2. 已查清的事实

### 2.1 云老板是什么
智慧门店 SaaS + 美团/抖音团购接口聚合服务。BossIP/openbox 作为它的"商户"用 `clientId + secret` 请求头调用 `https://openapi.yunlaoban.vip`，云老板再以抖音本地生活服务商身份代调抖音/美团。它不是账务系统：没有订单全量、结算、提现、分账接口（详见 2026-08-21 的《云老板平台调查报告》，在 `~/Documents/Codex/2026-08-20/https-tadmin-yunlaoban-vip-https-tadmin/`）。

### 2.2 接口清单（2026-09 版文档）

| 能力 | 接口 | 状态 |
|---|---|---|
| 商户信息 / 接口余额 | `POST /api/isp/merchant/info` `{}`，`apiBalance` 单位分 | 已验证，余额 0 |
| 门店列表 | `POST /api/isp/shop/list` `{shopId?}` | 已验证 |
| 批量新增门店 | `POST /api/isp/shop/create` `{shopList:[{bizId, shopName, contacts, phone}]}` → `result[{bizId, shopId, shopName}]` | 文档有，未调过 |
| 绑定团购门店 ID | `POST /api/isp/shop/update` `{shopId, dyShopId, ksShopId}` | 上次报"参数错误"，疑参数名不对，按此三字段重试 |
| 平台授权链接 | `POST /api/isp/groupBuy/getScopeUrl` `{shopId, platform}`（1 美团 / 2 抖音）→ `result` 是 `https://auth.dylk.com/auth-isv/?...solution_key=4...` | 已验证 |
| 查门店团购商品 | `POST /api/isp/groupBuy/queryshopdeal`；抖音 `{shopId, platform:2, params:{account_id, goods_creator_type:1}}`；美团 `{shopId, platform:1, offset:1, limit:50, source:1}` | 抖音已验证（泽岚鲜果 2 个在线商品）；美团返回 `[]` 待查 |
| 验券准备 | `POST /api/isp/groupBuy/prepare` `{shopId, platform, code}` | 只读，未用真券测 |
| 核销 | `POST /api/isp/groupBuy/consume` / `consumeWithResult` `{shopId, platform, code, ticketInfo, num?}` | 会改券状态，未测 |
| 撤销核销 | `POST /api/isp/groupBuy/revoke` `{shopId, platform, code, ticketInfo}` | 未测 |
| 核销记录 | `POST /api/isp/groupBuy/getRecords` `{shopId, platform, offset, limit, source, consumeStatus(0 已核销/1 已撤销/2 退款), startDate, endDate}` | 已验证，0 条 |

**没有的接口**：商品创建/上下架、订单列表、退款、结算/提现/分账、授权事件回调查询。

### 2.3 返回结构与坑
- 外层 `{code:"SUCCESS", message, msg, traceId, result}`；`result` 常是 **JSON 字符串，要二次解析**。
- 外层 SUCCESS ≠ 抖音成功：还要看内层 `data.error_code / data.description / extra.logid`（曾出现外层成功、内层 `2119005 应用未获商家授权`）。
- 所有门店 / 账户 / 商品 / 订单 ID 超 JS 安全整数，**必须按字符串处理**（解析前给 16 位以上数字加引号）。
- `account_id` 必须是抖音来客商户账户 ID（来客 PC 右上角 / App"我的"），不是普通抖音号 ID。
- 金额单位分；`ticketInfo`、券码、手机号、secret 不进日志。
- 鉴权是长期静态 `clientId + secret`，无签名/时间戳；后台曾用默认弱密码——重启前轮换。

### 2.4 门店开通链路与"审核"的真相
文档"接入准备"五步：提供申请信息 → 服务商分配账号并取 clientId/secret/门店 ID → **支付费用（具体详谈）** → **把门店名称、门店 ID 交服务商审核** → 审核通过后填抖音 ID（`shop/update` 或后台）。随后店主用 `getScopeUrl` 链接在抖音来客扫码"授权绑定"。

商户后台前端代码（`tadmin.yunlaoban.vip`，bundle 存在上面那个目录的 `work/`）里**没有任何"审核"功能**；启用/停用包年（`/bas/merchant/changeGroupBuyingYear`，"启用后接口免费使用"）、扫码/手动充值、接口单价、门店有效期全是 `isPlatformAdmin` 才能操作。即：**审核 = 云老板人工放行 + 收费，没有 API，也没有商户自助入口。** 可自动化的只有建档、绑 ID、出授权链接、状态跟踪；店主扫码授权是抖音规则，绕不开。

后台还有两条备选路：邀请注册（`/merchant-register` + 邀请码，生成独立商户与密钥，不推荐）；门店团购配置的"商家自研"tab（用商家自己的抖音服务商应用，代码里"商家自研模式无需授权"，需 openbox 自有抖音本地生活服务商资质，中期选项）。

### 2.5 商务结果（2026-09-08，用户已谈定）
- **云老板已为我们开通自动审批**：新建门店填好信息即开通，人工审核步骤消失。
- **审批对象是门店**：我们一个云老板商户、一套密钥，`shop/create` 建门店即开通。
- **产品约束**：每个客户账号（workspace）只能一个抖音门店 + 一个美团门店。云老板一个 `shopId` 同时承载两个平台（`platform=1/2`），所以落库是**每个 workspace 恰好一条门店**，两个平台的授权状态分开推进。

### 2.6 当前配置（无密钥）

```
YUNLAOBAN_BASE_URL=https://openapi.yunlaoban.vip
YUNLAOBAN_CLIENT_ID=b8ab85da15aa4455bac0986e8c6b03b0
YUNLAOBAN_CLIENT_SECRET=<用户自填>
YUNLAOBAN_SHOP_ID=16201667554403          # 泽岚鲜果，门店有效期 2027-08-20
DOUYIN_ACCOUNT_ID=7670896791667230747     # 抖音来客账户 ID
DOUYIN_POI_ID=7670910264128047145         # 抖音门店 / POI
```
抖音已授权；美团已授权但 `queryshopdeal` 返回 `[]`；`apiBalance=0`。

### 2.7 资料怎么再拿到
- 接口文档是有道云笔记 `06293d4c21e2dbd5a1ac5878f85f5146`（`https://share.note.youdao.com/s/bWvzNrTV` 也指向它）。分享页正文在 iframe 里且旧接口只回占位；在该页面的浏览器上下文里 `fetch('https://share.note.youdao.com/yws/api/note/<id>?sev=j1&editorType=1&editorVersion=new-json-editor&sec=v1')` 能拿到 JSON 正文，递归收集字符串即可得全文。
- 商户后台前端 bundle 与截图：`~/Documents/Codex/2026-08-20/https-tadmin-yunlaoban-vip-https-tadmin/work/`。

## 3. 未决问题（重启时先问云老板）
1. 能否开放抖音"商品创建 / 上下架"接口（他们已持有商品发布权限）；美团同理。**没有这个，"发布商品券"只能半自动。**
2. `solution_key=4`（抖音已标"即将下线"）→ `16` 的迁移时间表。
3. 自动审批之后按什么计费；`apiBalance=0` 时核销会不会被拒。
4. `shop/update` 的正确参数（按 `{shopId, dyShopId, ksShopId}` 重试一次）。
5. 美团 `queryshopdeal` 为空：是美团侧没上架团购还是授权门店对不上。
6. 后台"抖音店铺"页为空但接口能查到商品，是否影响后续。

## 4. 重启时的方案（放 openbox 授权中心，与抖音视频发布并排）

- **授权中心第二张卡"团购门店（抖音来客 / 美团）"**：绑定向导只填门店名、联系人、电话 → `shop/create` 建档即开通 → 绑抖音 POI（`shop/update`）→ 抖音 / 美团各一个"授权"按钮（`getScopeUrl` 出链接 + 二维码，店主在抖音来客 / 美团开店宝扫码）→ `queryshopdeal` 查到商品即"已开通"。卡上显示两个平台状态、有效期、已上架商品列表。每 workspace 一店。
- **"发布商品券"表单**：券名、售价、原价、套餐内容、使用规则、有效期。云老板给了发布接口就直发；没给就生成"上架资料"并引导店主去来客创建，后台定时 `queryshopdeal` 核对到同名同价商品即标"已上架"。两条路都预留。
- **模型侧**：工具 `groupbuy_store`（status / bind / auth_link / deals / draft_deal / publish_or_pack / check_published）+ 技能 `groupbuy-deal`：查状态 → 未绑就引导绑定与扫码 → 按用户描述拟券出确认卡 → 发布或出上架资料 → 核对。**核销不给模型**（店员收银动作，另走表单，默认总闸关）。
- **代码**：后端复用 `backend/platforms/` 提供者层，新增 `platforms/yunlaoban/`（应用级密钥走环境变量，不是 OAuth）；新表 `groupbuy_stores`（每 workspace 一条，双平台状态）、`groupbuy_deals`（本地券草稿与上架核对）、`groupbuy_verifications`（核销审计，券码只存哈希，`ticketInfo` 只进短 TTL 缓存）；客户端做双层 JSON、长数字转字符串、只对网络错误/5xx 重试且核销撤销永不重试、pino/日志脱敏；核销超时不重试而是 `getRecords` 回查。
- **分期**：P0 客户端 + 只读接口 + mock 测试（2 天）；P1 门店卡与绑定向导（1.5 天，含建测试门店验证"建档即开通"0.5 天）；P2 商品券表单（视云老板答复 1–2 天）；P3 工具与技能（1 天）；核销与真券联调单独排期，用户现场确认后才开总闸。

## 5. 重启第一步清单
1. 问清 §3 的 1–3 条（尤其商品发布接口）。
2. 用户把 `YUNLAOBAN_CLIENT_SECRET` 放进 gw2 `config/backend.env`；轮换后台密码与 secret。
3. 经用户同意后建一个"测试门店"验证建档即开通，并重试 `shop/update`。
4. 从 §4 的 P0 开始，先做只读，不碰 consume / revoke。
