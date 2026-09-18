# 九数云取数接入 —— 执行单（抖音来客 / 美团团购交易数据）

> 2026-09-18。自包含执行单。背景与调研结论见 §1，字段清单见 `docs/external/jiushuyun-laike-meituan-fields.md`。
> 决策已定（09-16）：九数云是交易类数据（来客团购订单/账单/评价/退款、美团团购结算/验券/点评）的正式取数路线，与云桌面浏览器路线并行、互为补充；浏览器路线继续负责经营分析类指标（曝光、访问、转化、投放）。自建服务商资质、商家自研应用两条路已否，不再讨论。
>
> **执行者须知**：新增 `backend/platforms/jiushuyun/**`、`backend/db/models/jsy_*.py`、一条 alembic 迁移、`backend/api/store_data.py`、`frontend-v2/src/features/auth-center/` 下两张卡；
> 只在 `core/config.py`、`platforms/tasks.py`、`db/models/__init__.py`、`main.py`、locale 文件做加法。不动 `platforms/douyin/**` 的 OAuth 链路，不动视频技能，不动移动端。
> 九数云 accessKey 只进环境变量，任何情况下不入库、不入日志、不入前端。

---

## 1. 背景与已确认事实

| 项 | 结论 | 来源 |
|---|---|---|
| 九数云能给什么 | 抖音来客 12 张表 175 字段（订单+4 子表、餐饮账单 53 字段、评价、退款、门店）；美团 18 张表 255 字段（团购结算明细/已验券码/点评 + 外卖订单/评价/评分/店铺分） | 销售看板 `work.jiushuyun.com/decision/shared/0q3rfhff753fp`，09-18 全量抓取 |
| 不能给什么 | 两家的经营分析指标（曝光/访问/转化/收藏/客群/带货归因）；团购商品主数据；美团退款、打款、广告费；饿了么已下架；帮助文档里的美团 cpc 报表看板中不存在 | 同上 |
| 上游 | 来客走抖音生活服务开放平台，帆软是服务商（"帆软软件有限公司 / E数通"），业务范围"到店餐饮解决方案"；美团走 openapi.meituan.com | 帮助文档 hc.jiushuyun.com/doc/26677、/doc/26702 |
| 计费 | 数据源基础订阅（30 天试用）+ 美团每张表按接口调用扣云币（1 云币 = 1 元，管理类 API 0.3 元/百次，失败也扣）；来客免调用费 | /doc/25940、/doc/26408、/doc/26584 |
| 数据出口 | 企业版开放平台：`GET /api/v1/corp/get/apitoken` → `GET /api/v1/corp/oauth/token`（1h 有效，10s 限一次）→ `POST /api/v1/folders`、`POST /api/v1/{folderId}/tables`、`POST /api/v1/table/{name}/id`、`POST /api/v2/export`（整表 CSV/Excel，只支持分析表）。备用：仪表板查询接口每页 500 行 | help.fanruan.com/jiushuyun/doc-view-140/149、hc /doc/25739 |
| 限制 | 美团首次只回溯 5 天；一个美团店铺只能绑一条连接，团购授权独占，会解除门店已绑的其他系统；来客退款单只能查 90 天；九数云定时同步最高每小时 | 帮助文档 |
| 企业版 | 已开通（09-18） | 用户 |

---

## 2. 目标与完成定义

**一句话**：商家把来客/美团授权给我们的九数云企业账号后，openbox 每小时把他的团购交易数据拉进自己的库，授权中心能看到状态，agent 能查账。

**完成定义**：
1. 一个商家授权后 2 小时内，openbox 库里有他当天的来客订单、账单、评价、退款和美团结算明细、已验券码、点评，金额与来客/经营宝后台对账单一致。
2. 授权中心出现"抖音来客数据""美团团购数据"两张卡：待授权 / 已同步（上次同步时间）/ 停更 / 已停用。
3. 定时任务每小时拉一次，停更超 3 小时或美团因无云币失败时写通知。
4. agent 有 `store_ledger` 工具，能回答核销数、结算金额、退款、评价等问题。
5. `docs/API_INTERFACES.md` 补条目。

---

## 3. 第一步：九数云侧准备（手工，D0）

| # | 动作 | 验收 |
|---|---|---|
| 1.1 | （**09-18 已完成**，密钥已交用户写入 gw2 `.env`）企业超管账号登录 work.jiushuyun.com，浏览器 Application → Cookies → `fine_auth_token`，加 `Bearer ` 前缀调 `GET /decision/api/v1/corp/get/apitoken`，拿 `accessKeyId` / `accessKeySecret` | 两个值存入 gw2 `.env`：`JSY_ACCESS_KEY_ID`、`JSY_ACCESS_KEY_SECRET`、`JSY_BASE_URL=https://work.jiushuyun.com/decision` |
| 1.2 | **先验证旧文档接口存活**（**09-18 已验证**：超管 cookie 换 accessKey 成功；`oauth/token` 200 返回 220 字符临时 token；`/api/v1/folders`、`/api/v1/{folderId}/tables`、`/api/v1/table/{name}/id` 均 200；`/api/v2/export` 存在，假 id 返回 `errorCode 61310042 "Api data source can not find table"`，真实导出待首张分析表建好后再测；仪表板查询接口也在线） ：`GET /api/v1/corp/oauth/token?accessKeyId=&accessKeySecret=` → 用 token 调 `POST /api/v1/folders`、`POST /api/v1/{folderId}/tables`、`POST /api/v2/export {"tableId": ..}` | 三个都 200 才继续；`/api/v2/export` 若 404，改走仪表板查询接口（§5.3 备用方案） |
| 1.3 | ~~数据连接市场开通"抖音来客""美团"试用（30 天）~~ **09-18 已确认不需要**：版本信息页显示两个数据源已随企业版购买，有效期 2026/09/18~2027/09/18 | 已完成 |
| 1.4 | ~~开云币支付并充值~~ **09-18 实际情况**：本企业按"增值数据源同步点数"计费（¥100 = 1 万点，1 万点起购，含税 ¥113），版本信息页没有云币开关。已购 1 万点（订单 20260918202803838474，¥113，09-18 20:28 支付） | 版本信息页点数总量 = 10000 |
| 1.5 | 新建项目 `openbox-ingest`，只放导出用分析表（**09-18 已建**，folderId `agqliy7uwfzy5hoqmja7ydj43m`） | 已完成 |
| 1.6 | 定命名：连接 `lk_{workspace_id}` / `mt_{workspace_id}`；分析表 `exp_{lk|mt}_{table}_{workspace_id}`，`table` 取 §6 落地表名 | 写入本单 §6 |

同时问销售（不阻塞）：数据源基础价按企业还是按连接/门店计；企业版连接数、表数、同步行数上限；`/api/v2/export` 是否长期维护；来客账单表回溯范围；交易类 API 单价是否与管理类相同。

## 4. 第二步：首个商家跑通授权（手工，D1–D2）

### 4.1 来客
1. 商家在来客后台：店铺管理 → 服务应用授权 → 服务商代理 → 新增授权。所属服务商"帆软软件有限公司"，授权应用"E数通"，授权业务"到店餐饮解决方案"，门店范围全部门店。
2. 我们在九数云：数据连接市场 → 抖音来客 → 新增链接，名称 `lk_{workspace_id}` → 点击授权 → 跳来客登录。
   - 登录动作在**商家的云桌面**里做：把授权页 URL 发到他的云桌面浏览器，他自己登。**先试一次授权链接能否单独打开**；不能就只能运营远程陪同，记进 §8 风险。
3. 授权完成 → 左侧出现 12 张表 → 点"同步数据源"跑一次全量。
4. 对照字段清单核对：`餐饮团购-订单查询`、`餐饮团购-餐饮账单详情查询`、`餐饮团购-评价查询`、`退款单列表查询`、`门店管理-查询门店信息` 有数、字段对得上。
5. 批量设置定时同步：每小时。

### 4.2 美团
1. 动手前确认门店**没有绑任何收银/POS/其他 SaaS 的团购授权**（授权独占）。
2. 九数云：新增"美团"数据连接，名称 `mt_{workspace_id}`，业务类型"团购" → 点击授权 → 商家在云桌面里用美团账号密码登录并同意。
3. 同步一次，核对 `查询团购订单结算明细`、`已验券码查询`、`查询美团平台指定门店点评内容`。
4. 看"任务记录"里每张表扣的云币，记进本单 §8 作为单价依据。
5. 定时同步：每小时。

### 4.3 建导出用分析表
每张主表建一张透传分析表放进 `openbox-ingest`：来源表 → 筛选"游标字段 ≥ 今天-7 天" → 保存。游标字段：

| 表 | 游标 |
|---|---|
| 餐饮团购-订单查询 | 最新修改时间 |
| 餐饮团购-餐饮账单详情查询 | 结算时间 |
| 餐饮团购-评价查询 | 评价时间 |
| 退款单列表查询 | 创单时间 |
| 门店管理-查询门店信息 | 无（整表，量小） |
| 查询团购订单结算明细 | 验券时间 |
| 已验券码查询 | 券码使用时间 |
| 查询美团平台指定门店点评内容 | 无（整表） |

子表（商品/履约/子单金额/联系人/评价图片/售后子单）先不单独建，第一版用主表 + 商品/履约两张子表，其余按需加。

---

## 5. 第三步：openbox 后端接入（D3–D6）

### 5.1 配置
`core/config.py` 加三项，映射环境变量 `JSY_BASE_URL`、`JSY_ACCESS_KEY_ID`、`JSY_ACCESS_KEY_SECRET`，写法照 `douyin_client_key` 那组。

### 5.2 客户端 `backend/platforms/jiushuyun/`
- `client.py`
  - `get_token()`：`GET {base}/api/v1/corp/oauth/token`，进程内缓存，55 分钟过期；调用间隔 ≥ 10 秒（asyncio.Lock + 上次时间戳）；连续失败 3 次退避 10 分钟并记 warning（九数云会限制错误过多的 key）。
  - `list_folders()`、`list_tables(folder_id)`、`table_id(name)`、`export_csv(table_id) -> bytes`。所有请求头 `Authorization: {token}`。
- `parse.py`：CSV → 行字典。规则：UTF-8、全字段带双引号、金额字段单位为分（转 Decimal 元入库或原样存分，**统一存分**）、时间字段为九数云导出的格式化文本，按表映射到 UTC。
- `sync.py`：`sync_connection(conn)`：按 §6 每张落地表找分析表 id（名字 → id，缓存到连接行）、导出、解析、按主键 upsert、更新 `last_sync_at` / 行数 / 错误；单表失败不影响其他表。

### 5.3 备用出口（仅当 §3 1.2 判定导出接口不可用）
改用仪表板查询接口：在九数云建一个仪表板，每张分析表放一个明细表组件；`client.py` 增加 `dashboard_meta(id)`、`component_data(component_id, page)`，每页 500 行循环。其余不变。

### 5.4 落地表与迁移
新增 `db/models/jsy_connection.py` 与 `db/models/jsy_store_data.py`（一文件多表），`db/models/__init__.py` 导出，一条 alembic 迁移。表定义见 §6。

### 5.5 定时任务
`platforms/tasks.py` 里新增 `internal_tasks.register("jsy_store_pull", 60*60, pull_all)`，写法同 `platform_token_keepalive`。`pull_all()`：遍历 `status='active'` 的连接，逐个 `sync_connection`，每个连接写一条 `jsy_sync_run`。错开九数云同步：任务首次注册时对齐到整点 +30 分。

### 5.6 告警
`pull_all()` 末尾：
- `last_sync_at` 早于 3 小时前的活跃连接 → `add_notification(kind="jsy_stale")`，每连接每天最多一条。
- 美团表错误文本含"云币"/"余额" → `add_notification(kind="jsy_no_credit")`，发给 workspace 管理员和平台管理员。

### 5.7 API `backend/api/store_data.py`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/store-data/connections` | 当前 workspace 的两张卡数据 |
| POST | `/api/admin/store-data/connections` | 运营登记连接：`workspace_id`、`platform(lk|mt)`、`connect_name`；后端立即调九数云校验分析表存在，写入 id 缓存并触发一次同步 |
| POST | `/api/admin/store-data/connections/{id}/deactivate` | 商家退出 |
| POST | `/api/admin/store-data/connections/{id}/sync` | 手动触发 |
| GET | `/api/store-data/summary?from=&to=` | 按日汇总：核销数、GMV、应结算、实结算、退款、评价均分，分平台 |

登记为什么走 admin：授权动作发生在九数云控制台，openbox 感知不到，只能由运营在授权完成后登记。

### 5.8 agent 工具 `store_ledger`
只读，参数 `platform`、`from`、`to`、`group_by(day|store|sku)`、`metric`。读落地表返回汇总与明细，最多 200 行。注册到 `tool/registry.py`，曝光规则与其他只读工具一致。

### 5.9 前端
授权中心加两张卡：标题、状态、上次同步时间、门店数、"数据来自九数云，由运营协助授权"说明；无按钮，授权流程由运营侧完成。locale 两份各加 6 条。

---

## 6. 落地表

命名 `jsy_` 前缀，全部带 `workspace_id`、`connection_id`、`raw_json`（原行）、`synced_at`。主键取自字段清单。

| 表 | 主键 | 来源分析表 | 关键列 |
|---|---|---|---|
| `jsy_connection` | id | — | workspace_id, platform, connect_name, folder_id, table_ids(json), status(active/stale/inactive), last_sync_at, last_error |
| `jsy_sync_run` | id | — | connection_id, started_at, finished_at, rows_by_table(json), error |
| `jsy_lk_order` | order_id | 餐饮团购-订单查询 | order_type, order_status, create/pay/update_time, original_amount, discount_amount, pay_amount, poi_id, poi_name, merchant_account_id, open_id |
| `jsy_lk_order_goods` | (order_id, sku_id) | 订单查询_商品信息 | sku_name, count, third_sku_id |
| `jsy_lk_order_certificate` | (order_id, certificate_id) | 订单查询_履约信息 | order_item_id, sku_id, item_status, item_update_time, refund_amount, refund_time |
| `jsy_lk_bill` | ledger_id | 餐饮账单详情查询 | settle_time, settle_type, biz_type, poi_id, verify_id, code, sku_id, group_name, amount_original, amount_pay, amount_ledger_total, amount_goods, 各补贴/佣金列（按清单全收） |
| `jsy_lk_comment` | rate_id | 评价查询 | poi_id, create_time, score, text, product_id, product_name |
| `jsy_lk_refund` | after_sale_id | 退款单列表查询 | order_id, refund_type, create/audit/complete_time, total/refund/real_refund/fee 各金额 |
| `jsy_lk_shop` | poi_id | 门店管理-查询门店信息 | poi_name, address, lat, lng, account_poi_id, root_account_id |
| `jsy_mt_coupon_trade` | (order_id, coupon_code) | 查询团购订单结算明细 | poi_id, poi_name, deal_id, use_time, deal_value, coupon_buy_price, buy_price, biz_cost, due |
| `jsy_mt_coupon_verified` | (coupon_code, coupon_use_time, verify_acct) | 已验券码查询 | poi_id, deal_id, deal_title, coupon_buy_price, status_desc, is_voucher |
| `jsy_mt_review` | feedback_id | 查询美团平台指定门店点评内容 | poi_id, star, scores, content, shop_reply |

---

## 7. 第四步：运营与风控

- 九数云超管账号单独建，只给这条链路用，密码进密码库。
- 商家退出：运营在九数云删连接 → 调 deactivate；落地表保留，不删。
- 数据回溯：美团接入日前 5 天起，来客账单以首次全量同步为准；对商家不承诺接入日之前的账目。
- 每周看一次云币余额与"任务记录"，把单商家月消耗记进本单 §8。
- 授权中心页面文案明确"交易数据来自九数云同步，每小时更新"。

---

## 8. 风险与待确认

| 风险 | 处理 |
|---|---|
| `/api/v2/export` 只在旧帮助中心有文档，新帮助中心未列 | §3 1.2 第一天验证；不通走 §5.3 |
| 授权链接可能不能脱离九数云控制台单独打开 | §4.1 第一个商家试；不能则运营远程陪同，接入成本按每商家 30 分钟估 |
| 美团团购授权独占，会踢掉门店已绑系统 | 接入前逐店确认；有冲突的店只接来客 |
| 美团每表每次同步扣云币，单价只公开了管理类 0.3 元/百次 | 首商家跑一个月记实际消耗；余额告警 |
| 企业版连接数/行数上限未知 | 问销售；先按 50 个商家规划 |
| 九数云 Beta 数据源可能改表结构 | `sync.py` 对缺列容错、多列入 raw_json；每次同步校验主键列存在 |
| 商家在来客侧撤销服务商授权，九数云不会主动通知 | 3 小时停更告警兜底 |

---

## 9. 排期与验收

| 天 | 内容 | 验收 |
|---|---|---|
| D0 | §3 全部 | 三个读取接口 200；云币到账 |
| D1–D2 | §4 首商家 | 九数云里两条连接有数，分析表 8 张建好 |
| D3–D5 | §5.1–5.6 后端 | 单测：token 缓存/节流、CSV 解析（分与时间）、upsert 幂等；本地跑一次 `pull_all` 落地成功 |
| D6 | §5.7–5.9 API/工具/前端 | 授权中心两张卡显示正确；agent 能答"昨天来客核销了多少" |
| D7 | 发 gw2，首商家验收 | 与来客后台对账单核对当日应结算金额一致 |

---

## 10. 关系说明

- 与 `docs/A5_AUTHORIZATION_CENTER.md`：复用授权中心页面和 `add_notification`，不复用 OAuth 链路（九数云那边没有我们可接的 OAuth）。
- 与浏览器路线（云桌面读来客/经营宝页面）：本单只覆盖交易类；经营分析指标继续走浏览器，两边在 `store-data/summary` 和 agent 工具层合流。
- 与 `docs/BILLING_PLAN.md`：九数云订阅与云币是平台侧成本，首期不向商家分摊，成本数据记进 §8 后再定价。
