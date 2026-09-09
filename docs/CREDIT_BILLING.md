# Token 积分计费与支付接入

2026-09-07 更新：付费套餐与无影云自动开通、Free 普通对话、到期停用但保留机器的规则见 [订阅 Sandbox 生命周期](SUBSCRIPTION_SANDBOX.md)。该实现不改变本文的模型积分计量；云电脑不再需要用户先手动开通。

2026-09-07 临时测试价：按用户要求，默认 `billing/plans.json` 与备用 `billing/plans.payment-test.json` 的 Pro / Max 月付、年付均统一为 **¥0.10（10 分，总价）**；Free 仍免费，积分和有效期不变，额外充值规则不变。价格改动只影响新订单，既有订单、付款链接及已购订阅保留原快照。两份配置都已改价，清空 `BILLING_PLANS_FILE` 不会恢复原价；测试结束需明确修改目录并重启后端。下文旧支付测试/恢复价格记录仅为历史记录。此价不改变阿里云基础设施成本，支付成功仍会触发真实无影云开通。

本次改价前的默认价格为 Pro ¥499/月、¥5,988/年，Max ¥1,999/月、¥23,988/年；仅记录以便后续恢复，本次不设置自动恢复时间。离线浏览器计费回归可在 `frontend-v2` 运行 `npx playwright test --config playwright.billing.config.ts`，使用仓库套餐目录和模拟支付接口，不创建真实支付订单。

2026-09-05 实现。额外充值的 **1 元 = 1 积分** 替代旧 BILLING_PLAN / DETAILED_PLAN_M1_M2 中的换算比例；订阅套餐另按套餐价与周期额度计算。账户属于工作空间；个人空间对应个人余额，团队空间共用余额。模型调用始终记到会话保存的 workspace_id，HTTP 请求头不能改变会话的扣费归属。

## 积分订购套餐

商业参数参考邻接 workspace 的 [bossip 套餐目录](../../bossip/apps/center/src/billing/plans.config.yaml)；积分发放参考 [SubscriptionService](../../bossip/apps/center/src/billing/subscription.service.ts)。采用仓库中的默认配置，不读取 bossip 线上数据库覆盖值。OpenBox 的统一配置为 `backend/billing/plans.json`，可通过 `BILLING_PLANS_FILE` 指定完整替代配置并重启。这里只接入价格、积分和账期，不把另一个产品的云桌面、视频或席位权益作为已经实现的套餐承诺。

| 套餐 | 月付 | 年付 | 周期积分 |
| --- | ---: | ---: | --- |
| 免费版 | ¥0 | ¥0 | 每周 10 |
| 专业版 | ¥0.10 | ¥0.10 | 每月 280 |
| 旗舰版 | ¥0.10 | ¥0.10 | 每月 1,680 |

年付一次付款，仍按月发放额度。按北京时间自然周（周一零点）/自然月分桶，在该周期首次访问余额、套餐页或调用模型时发放当期额度，往期未领取额度不补发；已到账积分按参考实现累积，不做月清。`allowance:workspace:plan:period` 唯一账本键和余额行锁共同防止重复/并发领取。付费额度只在有效订阅期间发放；到期恢复免费版，不清除已经入账的余额。

沿用通用一次性支付接口，当前采用手动续订，不自动扣款。订单保存套餐价格、积分和周期的快照，确认付款后才写 `billing_subscriptions` 并发放当期额度。年付不一次发放 12 个月积分。再次购买（含换档）从该空间已付费期限的末尾起生效，避免覆盖剩余权益；界面在下单前说明生效时间并展示待生效套餐。这是通用预付支付下的续期处理，不接入 bossip 的 Stripe 自动续费或基础设施调度。

只有空间 owner/admin 可订购或继续付款。额外充值要求当前存在有效付费套餐，单笔 ¥1–¥100,000，快捷金额 ¥10/50/100/500；允许精确到分，按 1 元 = 1 积分到账，不变更套餐、发放周期或到期时间。新规则不阻止此前已创建的合法订单完成到账。

新增查询：`GET /api/billing/plans` 返回目录；`GET /api/billing/subscription` 返回当前套餐、额度周期、期限、待生效订阅、充值资格和当前成员是否可管理。`POST /api/billing/orders` 支持以下两种请求，均要求固定的重试键：

```json
{"kind":"subscription","plan_id":"pro","cycle":"yearly","provider":"gateway","request_key":"客户端UUID"}
```

```json
{"kind":"topup","amount_fen":1000,"provider":"gateway","request_key":"客户端UUID"}
```

订阅金额由服务端目录计算，拒绝客户端传入金额或积分覆盖值；旧充值请求可省略 `kind`。订单查询增加 `kind`、`amount_fen`、`plan_id`、`cycle`、`starts_at`、`ends_at`，便于区分已付款、当期额度和将来的订阅。迁移 `b9d1f3a5c7e9` 在已有充值订单上补 `kind=topup`，保留所有余额、用量和支付记录。

## 模型计费与通用支付

价格保存于 `backend/billing/rates.json`，支持通过 `BILLING_RATES_FILE` 指定完整替代价目表。单价单位是每百万 token，计算时除以 1,000,000。GPT 的美元官方报价按相同数值人民币计费；人民币报价直接计积分；其他美元报价按已记录汇率转人民币。当前美元兑人民币为 6.7787，取 2026-09-04 [国家外汇管理局中间价](https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do)。汇率不会在请求中联网浮动。

| 模型 | 输入积分 / 百万 token | 缓存读取积分 / 百万 token | 输出积分 / 百万 token |
| --- | ---: | ---: | ---: |
| GPT-5.6 Sol | 4 | 0.4 | 20 |
| GPT-5.6 Terra | 2 | 0.2 | 12 |
| GPT-5.6 Luna | 0.2 | 0.02 | 1.2 |
| GPT-5.5 | 5 | 0.5 | 30 |
| GPT-5.4 | 2.5 | 0.25 | 15 |
| GPT-5.4 Mini | 0.75 | 0.075 | 4.5 |
| Qwen3.8 Max，北京 | 12 | 1.5（隐式）/ 1（显式） | 36 |
| Qwen3.8 Flash，北京 | 0.8 | 0.1 | 2.7 |
| DeepSeek V4 Flash，空闲 / 高峰 | 1.5 / 3 | 0.05 / 0.1 | 4.5 / 9 |
| DeepSeek V4 Pro，空闲 / 高峰 | 4.5 / 9 | 0.15 / 0.3 | 13.5 / 27 |
| Claude Opus 5 / 4.8 / 4.7 / 4.6 | 33.8935 | 3.38935 | 169.4675 |
| Claude Sonnet 5 | 13.5574 | 1.35574 | 67.787 |
| Claude Sonnet 4.6 | 20.3361 | 2.03361 | 101.6805 |
| Claude Fable 5 | 67.787 | 6.7787 | 338.935 |
| Claude Haiku 4.5 | 6.7787 | 0.67787 | 33.8935 |
| Gemini 3.7 Flash，年底前优惠价 | 5.084025 | 0.5084025 | 25.420125 |

核价来源：[OpenAI 价格](https://developers.openai.com/api/docs/pricing)、[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)、[Claude 价格](https://platform.claude.com/docs/en/about-claude/pricing)、[DeepSeek 人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Qwen3.8 Max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Qwen3.8 Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[Gemini 价格](https://ai.google.dev/gemini-api/docs/pricing)。标准同步推理价作为产品计费基准，网关的折扣、套餐和返利不改变该价目表。

缓存读取和写入分开计费。输入 token 是包含缓存的总数，先减去缓存读写部分，再分别按各自单价求和。原生 Anthropic usage 的 input_tokens 不含缓存，先统一成包含缓存的口径；LiteLLM / OpenAI 的输入已包含缓存。推理 token 已在输出用量中，不再重复加算。Claude 的 5 分钟 / 1 小时写入分别按输入价的 1.25 / 2 倍，GPT-5.6 缓存写入按 1.25 倍；Qwen 写入遵循价目表单列价格。未确认的 token 分类不猜测价格。

GPT-5.6 三款及 GPT-5.5、GPT-5.4 的单次输入超过 272,000 时，该次调用全部输入（含缓存）按 2 倍、输出按 1.5 倍计价。DeepSeek 高峰为北京时间周一至周五 09:00–12:00、14:00–18:00，以调用开始时间选择价格。Gemini 优惠到 2026-12-31，过期后先标待定价，更新价目表后才能继续按新价收费。DeepSeek `deepseek-chat` / `deepseek-reasoner` 的官方别名已于 2026-07-24 停用，见[官方更新日志](https://api-docs.deepseek.com/zh-cn/updates/)；自定义网关若继续提供这些名称，先在 aliases 中确认实际映射，不能按免费调用处理。

`usage_events` 保存每次调用的模型、发起用户、会话、用途、用量、积分和价格快照。对话、工具调用后续、压缩（含分段）、标题生成、Bash 判断及定时摘要均经过统一计量。重试产生新调用记录；重复结算同一调用不会重复扣费。取消时若已收到上游 usage，仍结算；上游未返回 usage 的请求标 `unreported`，进程崩溃留下 `pending`，均不伪装为零消耗，需要核对上游记录。

积分金额使用 Decimal / NUMERIC(28,12)，API 返回十进制字符串，前端数字格式化不参与财务计算。每次扣款/充值在同一事务内锁定 `credit_balances`，同时写 `credit_ledger` 和业务记录。余额可从流水求和核对。会话软删除后保留计费记录；分叉复制的消息没有新调用用量，不重复记账。旧 `cost` 浮点字段仅作兼容展示，新客户端使用 `credits`，财务以积分流水为准。

环境变量：

```dotenv
# off: 停止新调用计量；shadow: 记用量与积分、不扣余额；enforce: 真正扣款
BILLING_MODE=shadow
# 可选，未设时读取包内 rates.json
BILLING_RATES_FILE=
# 可选，积分套餐完整替代配置
BILLING_PLANS_FILE=
# 未配置支付渠道时，订购按钮显示“暂未开放”，不能生成虚假付款成功状态
PAYMENT_PROVIDERS_JSON={}
PAYMENT_PUBLIC_BASE_URL=
```

当前本地默认 shadow。enforce 在模型调用开始前检查已验证价格和余额，结束时按实际 token 扣款。**这是调用后结算：在途调用可能使余额变负，之后的新调用会被拦截。** 它不是硬额度预付保留系统，不保证所有在途调用的最大输出都已有资金覆盖。切换模式不追扣历史/shadow 记录；免费/订阅额度按上述套餐规则独立入账。支付渠道配置好后可切换 enforce。

PostgreSQL 部署时先对目标环境执行 `alembic upgrade head`，再启动代码。单用户桌面 SQLite 启动时自动补齐旧订单的 `kind/product` 字段并创建订阅表，保留原订单。需要恢复旧对话统计时，在已明确选择正确数据库的 backend 环境执行 `python -m billing.backfill`。只根据原始 assistant message 用量生成历史估算；不使用会被压缩重置的 session 累计数，不扣余额。旧缓存字段缺少写入区分，历史估算按缓存读取处理，可能与原始供应商账单不同。新增已核实模型后可执行 `python -m billing.backfill --price-unpriced-history`，仅补齐此前待定价的历史估算，不修改已结算记录。本轮不涵盖视频/图片生成单独费用、缓存存储时长或支付渠道手续费。

支付适配有两种方式：实现 `billing.providers.PaymentProvider` 后在应用启动时调用 `register_provider(name, adapter)`；或在任意支付系统外包一层下述 HTTP 合同，直接使用现成的 `HttpPaymentProvider`。核心账本不依赖微信、支付宝、Stripe 等厂商 SDK。

```json
{
  "gateway": {
    "kind": "http",
    "display_name": "支付渠道名称",
    "base_url": "https://payments.example.com/openbox",
    "api_key": "服务间访问密钥",
    "webhook_secret": "至少32个字符的独立随机回调密钥",
    "checkout_hosts": ["checkout.example.com"]
  }
}
```

将该对象放入 `PAYMENT_PROVIDERS_JSON`，设置 `PAYMENT_PUBLIC_BASE_URL=https://你的应用域名`，密钥只保存在后端环境中。支付方最终给用户展示实际应付金额；用量页仅显示积分。

1. 浏览器通过已验证的工作空间调用 `POST /api/billing/orders`，充值时提交 `{ "provider": "gateway", "amount_fen": 1000, "request_key": "客户端生成且重试不变的UUID" }`。金额是人民币分，1000 分对应 10 积分，积分由服务端计算。订阅时改为提交上文的 `kind/plan_id/cycle`。同一空间同一 request_key 的用户、渠道、金额或订阅产品不同返回冲突。
2. OpenBox 先持久化订单，再 `POST {base_url}/checkouts`，JSON 为 `{ "version": 1, "order_id": "pay_…", "amount_fen": 1000, "currency": "CNY", "callback_url": "https://应用域名/api/billing/webhooks/gateway" }`。携带 `Authorization: Bearer {api_key}`、`Idempotency-Key: {order_id}` 和下面的签名头。**适配服务必须按 order_id 幂等创建收款订单**，超时重试也返回原订单。
3. 支付服务返回 `{ "provider_order_id": "厂商订单编号", "checkout_url": "https://checkout.example.com/…" }`。跳转 URL 只允许 HTTPS 和配置白名单。浏览器显示“前往支付”链接，轮询 `GET /api/billing/orders/{id}` 查询状态。
4. 仅支付成功后，支付服务向 callback_url 发送签名 JSON：`{ "order_id": "pay_…", "payment_id": "厂商唯一支付流水号", "amount_fen": 1000, "currency": "CNY", "status": "paid" }`。不可传 workspace_id、用户积分或余额。支付服务必须先核实厂商签名及交易状态。
5. OpenBox 验证签名、金额、币种、渠道、订单及唯一 payment_id，在一个事务中标记 paid，处理充值入账或订阅生效及当期额度。重复通知返回 200 `{ "accepted": true, "duplicate": true }`；同一支付流水不能给另一笔订单充值。仅跳转回来、客户端声称支付成功或创建订单都不会增加已购积分或激活套餐。

HTTP 签名合同：

```text
X-Payment-Timestamp: 当前 Unix 秒（允许正负 300 秒）
X-Payment-Signature: HMAC-SHA256(secret, timestamp + "." + 原始HTTP请求体字节) 的小写十六进制
```

验签在 JSON 解析前进行；重试通知需生成当前时间戳并重新签名，保持原 order_id / payment_id / 金额。回调请求体上限 64 KiB。不接收未签名的通用“加积分”接口，不开放浏览器直接修改余额。退款、订阅续费、撤销/拒付属于后续独立业务，不得伪装成重复 paid 通知。

查询接口 `/api/billing/balance`、`/summary`、`/usage`、`/ledger`、`/providers` 及订单详情都要求有效成员身份。用量和流水支持 `page` / `page_size`（1–100），总数和合计在数据库中计算。

侧栏“订购”进入 `/app/billing`，包含“积分订购”“用量与消耗”“充值记录”三个页签。用量从设置迁至 `/app/billing/usage`，旧 `/app/settings/usage` 自动跳转，侧栏余额也链接至新地址。用量卡片保留可用积分、token 数和消耗积分，计费模式等实现说明保留在本文，不占用页面空间。页面继续按当前工作空间统计；个人空间展示个人的用量与余额。

`GET /api/billing/summary` 和 `/api/billing/usage` 接受相同的可选查询参数：`date_from=2026-09-01`、`date_to=2026-09-05`、`tz=Asia/Shanghai`。日期为 `YYYY-MM-DD`，支持只填开始或结束日期；省略时查询全部记录。时区使用 IANA 名称，接口默认北京时间，页面传浏览器本地时区。结束日期包含当天全部记录，服务端将本地日期边界转换为 UTC，以 `开始日零点 <= created_at < 结束日次日零点` 查询，涵盖夏令时变化。无效日期、倒置范围和未知时区返回 422。筛选同时更新明细、条数和消耗合计，余额仍为当前可用余额；应用筛选或重置均回到第一页。

页面刷新或下单超时后，通过 `GET /api/billing/orders?page=1&page_size=10` 找回订单；仅返回当前用户在当前工作空间发起的订单，不包含支付密钥或厂商支付流水。`POST /api/billing/orders/{id}/checkout` 无需请求体，继续同一订单；仅原付款用户且仍是该空间有效成员时允许访问，否则返回 404。上游仍按原 `order_id` 幂等重试；已有收银链接或已支付时直接返回原状态，不再创建上游订单。即使支付回调早于创建收银台的响应，已支付状态也不会被覆盖。

“订单查询列表”支持分页、订单号、渠道、状态和日期组合筛选，展示订单号、套餐、金额、积分和支付状态。待付款订单可继续付款或取消支付；支付确认后刷新余额。浏览器请求会保留发起时的工作空间，登录续期重试不会随着用户切换空间改变订单归属；登录身份改变时不重放旧请求。

数据库健康检查包含六张计费表及关键字段。部署漏跑计费迁移时，健康检查返回未就绪，避免直到用户发消息或充值时才发现缺表。

验证命令：

```sh
cd backend
uv run --extra test pytest tests/unit/test_billing.py tests/unit/test_subscription_plans.py tests/unit/test_database_readiness.py
# BILLING_TEST_POSTGRES_URL 仅允许 localhost / 127.0.0.1；测试创建并清理独立 schema
uv run --extra test pytest tests/integration/test_billing_postgres.py
cd ../frontend-v2
npm run check
npm run build
npx playwright test e2e/billing.spec.ts --project=main --no-deps
```

2026-09-05 续作验证：计费、模型流处理、压缩、取消与任务相关后端回归通过；PostgreSQL 测试在独立临时 schema 实际运行本次 Alembic 迁移后，验证并发充值、并发扣费及同支付流水跨空间冲突，结束后清理测试 schema。日期测试覆盖本地日界线、单边范围、无结果、错误参数及 23/25 小时的夏令时日期。前端检查与生产构建通过；浏览器测试使用明确的模拟 API 数据验证桌面/635px 窄窗口布局、旧地址跳转、订购导航、日期筛选与分页重置、刷新恢复原订单及到账刷新余额。通用支付合同已验证，实际收款仍需配置外部支付适配服务；默认 `shadow` 只记消耗、不扣余额。

## 支付宝电脑网站支付

原生适配器 `billing/alipay.py` 使用 `alipay.trade.page.pay`，产品码 `FAST_INSTANT_TRADE_PAY`，跳转到支付宝官方收银台。当前支持商家直接接入的 **RSA2 公钥模式**，无需通用 HTTP 中转服务。实现参考[官方页面支付接口](https://aipay.alipay.com/docs/ai-web-app-payment-qianyi/api-list/alipay-trade-page-pay.html)和[支付宝官方 SDK](https://github.com/alipay/alipay-sdk-python-all)。

在后端环境中配置以下对象（JSON 应为一行，私钥文件不要提交到仓库）：

```json
{
  "alipay": {
    "kind": "alipay",
    "display_name": "支付宝",
    "app_id": "支付宝应用APPID",
    "seller_id": "收款商家的2088开头PID",
    "private_key_file": "/run/secrets/alipay-app-private.pem",
    "alipay_public_key_file": "/run/secrets/alipay-public.pem",
    "environment": "production",
    "return_url": "https://应用域名/app/billing/orders"
  }
}
```

- 将该对象写入 `PAYMENT_PROVIDERS_JSON`。`private_key_file` 是应用签名私钥（PKCS8 PEM，建议权限 0600）；`alipay_public_key_file` 是支付宝平台公钥，不能用应用公钥替代。两种密钥均要求 RSA 2048 位以上。密钥管理服务也可提供 `private_key` / `alipay_public_key` 字符串。
- 设置 `PAYMENT_PUBLIC_BASE_URL=https://应用域名`。异步通知地址为 `https://应用域名/api/billing/webhooks/alipay`，必须映射到创建订单所用的同一套后端及数据库。不能将本地测试订单的通知指向尚未部署此接口的生产服务。支付通知地址在每次下单的 `notify_url` 中指定；控制台“应用网关”和“授权回调地址”分别面向平台消息与授权流程，不用于替代支付通知参数。
- `return_url` 可选，默认该公共地址下的 `/app/billing/orders`。跳转只用于返回订单页面，不触发积分发放。通知及跳转地址限 HTTPS、256 字符以内；订单主题默认 `bossip 订购`，可通过 `subject` 修改。
- 沙箱将 `environment` 改为 `sandbox`，并使用对应沙箱 APPID、PID 和密钥。生产、沙箱网关固定在适配器中，不能由浏览器指定。

通知按表单解码后校验 RSA2 签名，再校验 `app_id`、`seller_id`、交易状态、原订单金额。仅 `TRADE_SUCCESS` / `TRADE_FINISHED` 可以入账。等待支付通知只确认接收；交易关闭通知将未支付订单记为已取消并移除支付链接，不激活套餐。已支付订单收到关闭通知不会被改为未支付或通过此路径撤销积分（退款另行处理）。重复字段、金额改动、错误账号或签名全部拒绝。允许支付宝重试历史通知，依赖订单与支付流水唯一约束防止重复发放。事务提交成功后返回纯文本 `success`，与通用 HTTP 适配器的 JSON 应答区分；参见[官方异步通知说明](https://aipay.alipay.com/docs/ai-web-app-payment-qianyi/api-list/async-notify-verify.html)。

选择套餐或额外充值后，先保存订单，再在当前标签直接跳转支付页；浏览器后退返回订单查询列表。支付渠道按钮位于套餐标题旁，默认选中支付宝，只展示实际配置的渠道。旧的内联支付提示及更新链接区域已移除。订单页提供“继续支付”“查询支付状态”“取消支付”。继续支付在重新生成收银链接前先查询支付宝，已支付或已取消时返回状态，不再跳转收银台。

### 支付测试结束后恢复原价

2026-09-06 用户已完成真实付款；阿里云订单页确认专业版月付 0.10 元订单为已支付，套餐已开通、余额为 290 积分。恢复价格时清空本地 `backend/.env` 和阿里云 `config/backend.env` 的 `BILLING_PLANS_FILE`，重启后端，使用默认 `billing/plans.json`：专业版 599 元/月、7188 元/年，旗舰版 2100 元/月、25200 元/年。已有订单金额及订阅权益保持下单快照；`BILLING_MODE=shadow` 继续保留。本操作不修改 AWS 环境。

### 本地 1 毛 / 2 毛测试价（历史联调记录）

按用户 2026-09-05 的测试要求，新增 `billing/plans.payment-test.json`：Pro 月付/年付均为 **10 分**，Max 月付/年付均为 **20 分**，积分与周期权益保持原配置。本地 `backend/.env` 的 `BILLING_PLANS_FILE` 指向此文件，订购页已确认显示 ¥0.1 / ¥0.2。正常商业价仍在 `billing/plans.json`；结束测试后清空 `BILLING_PLANS_FILE` 并重启后端即可恢复。已有订单保留下单时的价格与权益快照。应用没有增加支付宝单笔限额拦截。

原生适配器回归：

```sh
cd backend
uv run --extra test pytest tests/unit/test_alipay.py tests/unit/test_billing.py tests/unit/test_subscription_plans.py
```

这些测试使用临时 RSA 密钥和本地 HTTP 模拟，覆盖测试价签名、篡改通知、重复通知、到账事务、跨付款人隔离和签名查询恢复；不代表真实商家已完成支付。真实联调需先将已开通产品关联应用并完成密钥配置。

2026-09-05 支付宝适配验证：69 项后端测试通过；前端 i18n、类型检查和 205 项单元测试通过，ESLint 保留 23 项已有警告、无错误；生产构建与 5 项浏览器测试通过。新增浏览器测试验证 635px 视口中的 0.1 / 0.2 测试价、同订单更新链接、待支付/查询失败不激活套餐、确认付款后更新积分和权益。

2026-09-05 商户配置联调：应用公钥已由商户绑定，下载的支付宝公钥与控制台一致，私钥和商户配置保存在被 Git 忽略的 `backend/credentials/alipay/` 下。真实生产网关的只读交易查询已通过 RSA2 验签，随机校验订单返回 `ACQ.TRADE_NOT_EXIST`，未创建付款或改动积分。联调发现公共参数全部放入 POST 表单会让网关返回 GBK；现按官方 SDK 将公共参数（含 charset、sign）放在 URL，业务参数放在 UTF-8 表单中，修正后再次通过 69 项后端回归。

从生产后端容器实际查询确认出口 IP 为 `106.15.105.236`，域名解析的 `106.52.167.53` 是前置代理。支付服务端接口的白名单准备配置 `222.93.12.50,106.15.105.236`；当前等待商户在支付宝原标签页完成支付密码验证，尚未确认规则保存成功。线上 `/api/billing/providers` 仍返回 404，支付回调尚未发布。

### 本地实付测试（主动查单确认）

支付宝适配器可显式设置 `"confirmation_mode":"query"`，此时不依赖 `PAYMENT_PUBLIC_BASE_URL`，收银请求省略 `notify_url`，未指定 `return_url` 时也不发送返回地址。付款人完成支付后回到本地订单页，由页面自动查询，也可以点击“查询支付状态”；后端使用商户私钥调用支付宝并验证其签名响应，再通过原有到账事务开通套餐和发放积分。创建订单、未付款、查询失败均不会开通套餐；重复查询不重复发放。默认仍为 `callback` 模式，继续要求有效的公共 HTTPS 回调地址。

本地 `backend/.env` 已启用支付宝 `query` 模式和 0.10 / 0.20 测试套餐，忽略文件 `backend/credentials/alipay/…/provider.local.json` 保存对应配置。应用已重启，订购按钮可用，已创建一笔 0.10 元待付款测试订单。此模式不把本地订单发送到生产数据库，也不需要公开本地服务。付款后须由订单页自动或手动完成一次签名查询才能入账；正式部署时恢复 `callback` 模式并配置同一数据库对应的通知入口。本地 token 消耗仍为 `shadow` 统计模式。

本地查询模式验证：72 项后端测试、205 项前端单元测试、5 项浏览器测试以及生产构建通过。用例覆盖无回调地址下单、未付款不发积分、签名查询结算与重复请求、默认回调模式的地址校验，以及页面付款确认提示。

实际浏览器验证：本地 0.10 元订单已进入支付宝官方收银台，显示 `bossip 订购`、收款商户和 `0.10 元`，扫码待付款。当前登录的支付宝为收款商户，收银台提示付款人与收款人不能相同，应使用其他付款账号测试。本地“已支付，查询结果”已查询这笔真实未付款订单并显示未确认支付，套餐仍为免费版、余额仍为 10 积分。尚未执行真实付款或发放付费套餐额度。


## 2026-09-06 订单恢复与取消

- 页面进入、重新显示或获得焦点时查询当前列表中的待支付订单，页面可见时每 15 秒继续核对，最多同时查询 3 笔。后端调用 `alipay.trade.query` 并验证原始响应签名后更新状态；成功时刷新订单、积分余额和套餐。网络失败仍保留订单状态，允许手动重试。
- `POST /api/billing/orders/{id}/cancel` 仅允许原付款人且仍为该空间 owner/admin 调用。先查询实际状态，待支付才调用 `alipay.trade.close`；关单响应签名及订单号验证通过后显示“已取消”。关单失败或超时会再查询一次，处理付款与关单并发、响应丢失的情况。签名支付凭证优先，已支付订单永不因取消请求或延迟关闭通知倒退。
- 关闭网页本身没有可靠的取消支付通知，也不代表支付宝订单已关闭。未付款返回时保持待支付，用户可点击“取消支付”完成真实关单。支付宝返回交易不存在时，可以取消本地订购记录，并保留对旧收银页面的核对；这不表示支付宝已关闭一笔实际交易。此前签发的 page.pay 链接可能仍能创建交易，因此页面提示关闭旧收银台。
- 同一用户、工作空间、支付渠道、产品及价格快照的待支付订单会被新下单请求复用。每个请求键都保存在 `payment_order_requests`，因此更换页面产生的请求键，在原订单支付或取消后重试仍返回原订单，不会生成另一笔付款。不同产品或新价格不会合并；已完成订单后的明确新购买仍可用于续订。
- 迁移 `c0e2f4a6b8d0` 创建请求映射并回填历史请求键，已应用于本地数据库。历史重复订单保留，可逐笔查询或取消。
- `GET /api/billing/orders` 在当前付款人和工作空间内支持 `status=pending|paid|cancelled`、`provider`、`order_id`（字面部分匹配）、`date_from`、`date_to`、`tz`，过滤后再计数分页。日期包含完整本地结束日。付款渠道元数据增加 `supports_cancel`。

验证：106 项后端单元回归、4 项独立 PostgreSQL 集成测试（含 12 个不同请求键并发去重）、205 项前端单元测试、类型与语言检查、生产构建通过。10 项浏览器模拟测试已分批通过，覆盖直接跳转、返回及焦点自动查单、关单失败、成功取消、支付优先、筛选和窄窗口布局。未执行真实付款，真实商户关单也尚未人工联调；本地仍使用 0.10 / 0.20 测试价格及签名查询确认模式。


## 2026-09-06 真实历史订单取消修复

实际在用户现有浏览器复现旧订单取消返回 502。真实签名响应为 `code=40004, sub_code=ACQ.TRADE_NOT_EXIST`，没有 `trade_no` 或金额；旧实现将它作为关单失败，遗漏了仅生成过收银页面、尚未创建支付宝交易的订单。此前的自动化测试没有覆盖完整的真实响应形状及本地取消流程，不能作为该场景已经可用的证明。

现在用独立的 `TradeNotCreated` 类型表达“尚无交易”，不伪造已关闭交易的支付流水。只有用户明确取消且关单查询经过验签，才将本地订单设为已取消。新增 `cancelled_at`、`cancellation_reason`，日期使用时区类型；迁移 `d1f3a5b7c9e1` 已应用到本地 PostgreSQL。订单接口返回 `reconcile_required`：未创建交易的已取消订单仍参与自动核对，后续若出现待付款交易则继续关单，若收到已付款凭证则按原幂等到账流程处理，绝不丢弃真实付款。网络错误、签名错误、商户或订单不匹配仍拒绝取消。

真实浏览器验证（未支付）：
- 2026-09-05 22:01、22:03 的两笔 0.10 元历史订购，在原订单页点击取消后均显示已取消，取消时间正确，继续支付按钮消失；刷新页面及“已取消”筛选仍显示两笔记录。
- 22:18 的历史订单点击继续支付，进入真实支付宝官方 0.10 元收银台；展开支付宝订单详情，确认商户订单号仍为原订单号。返回 OpenBox 后仍只有原来的三笔订单：两笔已取消、一笔待支付。
- 余额始终为 10 积分。没有输入支付密码、扫码付款或发放付费套餐积分。本轮证明真实未创建交易的旧订单可取消、旧订单可续接收银台；未将其表述为真实付款或已创建交易的成功关单测试。

回归：111 项后端单元测试、5 项真实 PostgreSQL 临时 schema 测试、205 项前端单元测试和本次相关 6 项浏览器模拟测试通过，类型检查、语言检查及生产构建通过。新增用例直接复现无交易号/金额的签名错误响应，覆盖本地取消、刷新/筛选、旧收银页面迟到付款、后续实际关单、关单与付款竞争，以及数据库取消时间的时区。

## 2026-09-06 再次订购去重与跳转弹窗

订购套餐、额外充值、订单列表继续支付现在共用 `CheckoutRedirectProvider`。点击时先同步锁定支付操作并打开“正在跳转支付页面”弹窗，再发送订单请求；使用原生模态对话框阻止鼠标与键盘重复操作。弹窗放在订购路由内、各个分页之外，替换到订单列表时仍保留，直到外部收银台打开。请求失败显示可关闭的提示；浏览器返回恢复缓存页面时清除弹窗和锁，离开页面或切换账号/空间后的旧响应不会继续跳转。

服务端继续按当前空间、付款用户、渠道、套餐/账期和价格快照复用已有待付款订单。页面关闭、刷新、重新进入订购产生新的请求键，也映射到原订单；请求键别名持久化，已支付或已取消后的同一次请求重试不会生成新单。明确取消的订单保留历史记录，之后主动订购才创建新的付款订单。

真实浏览器验证：本轮开始时为 4 条订单（3 条已取消、1 条待支付）。复用用户原有标签，两次点击专业版月付订购、进入真实支付宝 0.10 元收银台、展开订单详情并返回。两次商户订单号均为 `pay_01M1TBZDGBNF3TQJX6WR2GS56F`；结束后页面与数据库仍为 4 条订单，同一待付款订单累计对应 4 个请求键。余额保持 10 积分，没有实际付款。真实弹窗截图保存在 `/tmp/openbox-billing-verification/checkout-progress-live.png`，收银台订单详情保存在 `/tmp/openbox-billing-verification/reused-order-cashier-live.png`。

验证通过：205 项前端单元测试、语言/类型/代码检查、生产构建；5 项隔离 PostgreSQL 集成测试（包含 12 个不同请求键并发只产生一笔订单）；15 项账单浏览器模拟测试分批通过。新增浏览器用例覆盖慢响应期间连续点击、页面离开前仍显示弹窗、退出收银台后再次订购、错误恢复和重试请求键、继续支付及充值入口。浏览器夹具与真实商户验证分别记录，模拟测试不执行真实付款。

## 2026-09-09 媒体计费第一条：视频云端合成（video_compose）

`rates.json` 新增 `media` 段（IMS 云剪辑官方价，按输出分钟、按短边定档、不足 1 分钟按 1 分钟、失败不计费）。
`billing/media.py`：`quote_compose` 提交前报价；`precheck_compose` 在 enforce 下校验会话 workspace 余额；
`settle_compose` 在 IMS 成功后按实际时长写一条 `usage_events`（kind=`video_compose`，幂等键 `compose:<job_id>`），
enforce 走 `post_ledger` 扣积分，shadow 只记录。视频生成与转写仍未落账。详见 `docs/VIDEO_RENDER_ENGINE_SELECTION.md` §5.5。
