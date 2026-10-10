# OpenBox × 高德：接口测试规范

版本：1.0 ｜ 编制日期：2026-09-22 ｜ 适用接口：`/v1`

本文定义“测什么、预期是什么、如何判定通过”。具体执行命令见《OpenBox-Gaode-接口测试操作手册》。这是测试基线，不代替业务接口文档或新的发布说明。

## 1. 测试基线与范围

| 项目 | 基线 |
|---|---|
| 测试地址 | https://gaode.bossipai.com.cn/v1 |
| 浏览器控制台 | https://gaode.bossipai.com.cn/gaode-console.html |
| 最近已验后端 | 20260922-gaode-d92cd11 |
| 文档/控制台对应提交 | 8f3e54a |
| 最近验收日期 | 2026-09-22；后续版本必须重新记录实际版本 |
| 默认档位 | medium；high/medium 为 1080p，low 为 768p 预览档 |
| low 配置证据 | 本轮实测 MiniMax-H3 / 768p；具体模型属于部署配置，不能只凭模型口头回复判断 |

范围：鉴权、跨域、素材、会话创建与历史、发消息与幂等、确认卡、轮询与终态、中止、成片下载、积分与限额。low 本轮已验证创建和落库映射，未做 low 付费成片；medium 的真实成片已在第二轮验证。

不把“单元测试通过”“上传返回 201”“模型声称成功”当成全部流程通过。没有实际执行的边界标为未测。

## 2. 接口清单

以下路径均相对于 BASE（已含 `/v1`），不要重复添加 `/v1`。

| 方法与路径 | 用途 | 正常响应 |
|---|---|---|
| POST /files | 每次上传一个素材，多素材分别调用 | 201，返回 fil_… |
| POST /sessions | 创建会话 | 201，返回 ses_… |
| GET /sessions | 当前身份在该工作区的顶层会话列表 | 200，data/has_more/next_cursor |
| GET /sessions/{sid} | 状态与累计积分 | 200 |
| POST /sessions/{sid}/messages | 提交需求，可带多个文件 ID | 202，user_message_id/assistant_message_id |
| GET /sessions/{sid}/messages | 公开会话历史、回复和确认卡 | 200，data/has_more |
| POST /sessions/{sid}/questions/{qid} | 按题目顺序答卡 | 200，ok=true |
| POST /sessions/{sid}/questions/{qid}/reject | 拒卡 | 200，ok=true |
| POST /sessions/{sid}/abort | 中止会话执行 | 200，aborted=true/false |
| GET /files/{fid}/content | 刷新文件下载地址 | 302，Location 为签名 URL |

会话列表按当前用户和工作区过滤，不是严格按某一把 Key 的创建记录过滤。列表使用 limit=1..100（默认 20）和 cursor；消息使用 after（公开消息 ID）及 limit=1..200。两种分页游标不可混用。

## 3. 前置条件与测试数据

- 已取得测试 Key，具有所测端点需要的作用域；Key 由对接负责人单独交付，本文及报告不包含真实 Key。
- 记录环境、版本、测试人、时区、task_id、测试前积分；每个新需求使用唯一 client_message_id。
- 准备有效、可解码的图片/视频/音频。套件里的 `materials/red/same-name.png` 和 `materials/blue/same-name.png` 是内容不同、文件名和字节数相同的有效 PNG。
- 另准备空文件、不支持格式、超长文本作反例。假 PNG 仅可用于损坏素材测试，不能用其模型解码失败判断正常多素材回归失败。
- 一般契约测试不调用媒体生成；文本对话也可能消耗积分。真实生成在展示模型、分辨率、时长和报价后由测试负责人确认；只生成获准次数，不自动付费重试。
- 60 次/分钟、5 个并发会话、600 秒卡片期限是当前集成基线；执行压力边界前核对实际配置、占用和测试窗口。

## 4. 用例与验收标准

优先级：P0 为交付关键路径；P1 为契约与交互；P2 为边界。结果只能写 PASS / FAIL / BLOCKED / SKIP；代码审查记录单列，不替代运行结果。

| ID | 级别 | 操作/输入 | 预期与判定 |
|---|---|---|---|
| A01 | P0 | 无 Key、坏 Key 请求会话 | 401 UNAUTHORIZED，统一 error 对象 |
| A02 | P1 | 正确 Key 请求不存在会话/路由 | 404 NOT_FOUND；正文 request_id 与 X-Request-Id 可关联 |
| A03 | P1 | 跨域 OPTIONS，Origin 为外部站点及 null | 允许 Authorization/Content-Type；浏览器实际请求可读响应 |
| A04 | P1 | 缺少作用域/其他工作区资源 | 按契约拒绝，不能泄漏正文；没有第二套授权身份则 SKIP |
| F01 | P0 | 上传有效素材并下载比对 | 201；大小正确；下载哈希等于源文件 |
| F02 | P0 | 两个素材分别上传，同一 messages.attachments 提交 | 202；历史有两个 input，模型正常识别两个 |
| F03 | P0 | 同名、同大小、不同内容的有效 PNG | 两个独立 fil ID，两个 input，不按名称/大小误合并 |
| F04 | P1 | 浏览器一次多选两个文件并发送 | 两次上传成功；两个可勾选项；发送正文携带两个 ID |
| F05 | P1 | 重复 attachment ID；21 个 ID | 400 INVALID_REQUEST；不能开始执行 |
| F06 | P2 | 空 PNG、不支持格式 | 空文件 400；不支持格式 415；看实际 MIME 与扩展名组合 |
| F07 | P2 | 文件大小 200×1024×1024 及多 1 字节 | 上限内可上传，上限外 413；需隔离测试流量 |
| S01 | P0 | 缺省、high、medium、low 建会话 | 201，idle，初始积分 0；缺省 medium，metadata 保留 |
| S02 | P1 | 核对档位落库及实际作业参数 | high/medium 1080p；low 768p，禁止误标为 1080p |
| S03 | P1 | 非法档位、metadata 17 个键/非字符串值 | 400；metadata 键长≤64、值长≤512 |
| H01 | P0 | 列表 limit=2 连续翻页 | 时间倒序，无重无漏；按 has_more/next_cursor 结束 |
| H02 | P1 | 非法 cursor、limit=0/101 | 400 INVALID_REQUEST |
| H03 | P0 | 浏览器选择历史或输入 ID 打开 | 展示该会话多轮文本、卡片、文件；处理中自动轮询 |
| H04 | P1 | 重复打开同一会话/切换后再打开 | 不空白、不混入其他会话，不发出新生成请求 |
| H05 | P2 | 超过 200 条公开消息的会话 | 完整分页且最新轮可见；当前控制台未提供完整分页，必须单列限制 |
| M01 | P0 | 发送文本与附件 | 202，保存原 user/assistant ID；轮询后部件逐渐更新 |
| M02 | P0 | 处理中同键同正文重发 | 202，两个 ID 不变，作业/计费不重复 |
| M03 | P0 | 处理中/结束后，同键不同正文 | 409 DUPLICATE_CLIENT_MESSAGE_ID |
| M04 | P1 | 处理中用新键发另一需求 | 409 SESSION_BUSY |
| M05 | P1 | 文本 UTF-8 大于 20×1024 字节 | 413 PAYLOAD_TOO_LARGE |
| M06 | P1 | 修改控制台输入后点幂等重发 | 重放原成功 payload，202，原 ID 一致 |
| Q01 | P0 | 单选、多选、自由输入答卡 | 按题目顺序传二维数组；200；模型准确复述 |
| Q02 | P1 | custom=false 填选项外答案 | 400；卡片仍可正常作答 |
| Q03 | P0 | 已答/已拒卡再次提交，包括同样答案 | 409 INTERACTION_RESOLVED，无重复执行 |
| Q04 | P0 | 留卡满 600 秒，不主动拒绝/中止 | status=timeout；回到 idle/结束态；再次答卡 409 |
| Q05 | P0 | 有素材历史时填写三类草稿，等待至少 20 次轮询 | 单选、多选、自由输入均不丢失，焦点不被签名链接刷新反复打断 |
| Q06 | P1 | 答卡状态变化触发真正重绘 | 已填值正确保留；已处理卡不可再编辑/提交 |
| P01 | P0 | 媒体生成超过模型单轮等待上限 | 公开 busy/finish=null 保持；无需人工发“继续”自动恢复交付 |
| P02 | P0 | 成片完成 | 同一公开回复出现可下载 final；本轮最终 finish=stop；不重复分享同一产物 |
| P03 | P1 | 多轮对话轮询 | 按 assistant_message_id 判断目标轮；不拿历史轮 stop 当成本轮完成 |
| B01 | P0 | 发需求后立即中止 | aborted=true；随后终态 aborted，不误标 stop |
| B02 | P1 | 空闲会话中止 | aborted=false，不开启新运行 |
| D01 | P0 | GET content 刷新后下载 | 302→新签名 URL GET 200；文件完整可解码，尺寸/时长符合批准参数 |
| D02 | P1 | 再次刷新签名 | 地址可更新且可下载；不使用 HEAD 验证 GET 签名 |
| C01 | P0 | 一次获准视频生成前后对账 | 仅一次生成收费，参数/金额与确认报价一致；聊天费用另列 |
| C02 | P2 | 专用低余额账户发需求 | 402 INSUFFICIENT_CREDITS，无付费作业；勿修改共享测试账户余额 |
| L01 | P2 | 隔离窗口触发频率上限 | 429 RATE_LIMITED；按 Retry-After 恢复，无忙循环 |
| L02 | P2 | 专用 Key 已占满并发后再发新任务 | 429 CONCURRENT_LIMIT_EXCEEDED；不干扰其他测试 |

## 5. 状态与成片判定

1. 202 仅表示已接收。`finish=null` 表示目标轮仍处理中；文字说“完成/后台运行”不能代替结构化状态。
2. question.status=pending 时允许答卡。answered/rejected/timeout 均不能重复处理。
3. 生成测试通过需要：有效 final 文件、下载成功、媒体规格正确、目标轮正常结束和费用一致。普通问答 stop 没有 final 属正常；视频生成 stop 没有 final 不算成片验收通过。
4. 不把任意 intermediate 文件由客户端擅自升级为 final；如只有 intermediate，记录服务端响应和失败证据。
5. error/aborted 不算成功；中止不等于上游任务必然取消或自动退款，需另查作业与账单。
6. 签名 URL 24 小时有效；媒体宽高/时长字段首期可能为 null，需从下载文件验证。接口例子里的非空值不代表当前已有探测能力。

## 6. 最近实测状态及未关闭项

| 项目 | 截至 2026-09-22 的证据 |
|---|---|
| low、同名多素材、草稿保留 | 第三轮通过；使用有效 PNG，无解码错误 |
| 异步生成、自动 final、下载 | 第二轮通过：Wan 3.0、1080×1920、5 秒，视频费 6 积分，一次生成 |
| 卡片完整 10 分钟超时 | 第二轮通过，timeout/idle/再次答卡 409 |
| 同会话重复打开 | 第三轮 FAIL：messages 被清空，但 dataset.sig 没重置，历史区空白；后续修复需重测 H04 |
| low 真正成片、跨租户、200MiB、402、并发压力 | 本轮未执行；不得填写 PASS |
| 控制台长历史 | 仅取前 200 条消息、最近会话最多 50 条，不能承诺任意长度“完整历史” |

本版结论基于已完成实测与当前代码，没有在编写文档时重新执行所有线上测试。

## 7. 测试报告模板与发布门槛

每次报告填写：版本、环境、测试人、起止时间/时区、用例 ID、PASS/FAIL/BLOCKED/SKIP、预期、实际、request_id、session_id、message_id、question_id、file/job ID、积分前后值、证据路径、缺陷链接。

建议发布门槛：P0 全通过；P1 失败有明确处理结论；所有 SKIP 写明原因与负责人；付费路径至少一条真实端到端证据。已有 H04 失败不得被“41 项单测通过”覆盖。

证据脱敏：移除 Authorization、Key、Cookie、签名下载 URL 和资产下载 token。保留不含凭据的 ID、状态码、时间和哈希。测试后保留会话供复查，不自动删除云端资源。
