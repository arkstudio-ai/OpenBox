# 高德联调实测验收（2026-09-22）

结论：**尚不能按“全流程验收通过”交付**。公网 API、鉴权、跨域、素材传输和基本交互可用；实际联调发现幂等、确认卡期限、中止终态、长视频任务等待及交付文档存在差异。真实视频生成和下载已成功，但自动等待与 final 交付契约未通过。

## 环境与边界

- 入口：`https://gaode.bossipai.com.cn/v1`；线上控制台 `/gaode-console.html`。
- 实测工作区：`/Users/wxy/openbox-harness-api`，`feat/harness-api-v1`，HEAD `d2178d8`；关联 PR #58。
- 高德 ECS：`i-uf6fm76cksm8z1cd7bqs`，后端 `openbox-backend:20260922-gaode-bf982a6`，前端 `openbox-frontend-v2:20260922-gaode-8cf388f`，均 healthy。
- 线上 HTML 与工作区逐字节一致，SHA-256 `482c9b1242f0d1ca19309863ef70ddcbd00feed3d20d099c40c017b963c509be`。
- 核对的线上 `api/v1/messages.py`、`api/v1/questions.py`、`tool/question_tool.py`、`question/question.py` 哈希均与工作区一致。
- 未部署、未重启、未修改云端配置、未接触 gw2。只新建验收会话/素材并调用接口；未删除会话、云资源或用户文件。
- 真实视频仅获准生成一次：Wan 3.0、1080p、9:16、10 秒、无字幕、报价 12 积分。文本策划/对话有独立的少量积分消耗。
- 下列证据 JSON 已移除 API Key 与签名下载 URL；凭据不放入此目录。

## 实测矩阵

| 项目 | 结果 | 证据/说明 |
|---|---|---|
| HTTPS 控制台、真实浏览器页面 | PASS | Chrome 正常打开；页面内容与本地文件相同 |
| 空/坏 Key | PASS | HTTP 401 `UNAUTHORIZED`，正文 request_id 与响应头一致 |
| 正确 Key | PASS | 浏览器连通性探针 404 `NOT_FOUND`，建会话 201 |
| 任意来源 CORS | PASS | `Origin: https://partner.example` 与 `Origin: null` 的 OPTIONS 200；ACAO `*`；暴露 X-Request-Id 等响应头 |
| 真正跨域浏览器调用 | PASS | 本机 `http://127.0.0.1:8986` 托管原 HTML，调用公网高德接口；鉴权、建会话、消息、答卡均成功 |
| API Key 持久化 | PASS（代码核对） | HTML 只把 base 写入 localStorage，Key 保留在 password 输入框；未引入第三方脚本 |
| PNG 上传与下载 | PASS（API） | 上传 201；GET content 302；签名 URL 200；下载字节 SHA-256 与上传源相同 |
| 浏览器文件上传 | BLOCKED（测试工具权限） | ChatGPT Chrome 扩展未允许 file URL 访问，文件选择器 setFiles 被拒绝；不能据此判产品上传失败 |
| 不支持格式/空文件 | PASS | TXT 415 `UNSUPPORTED_MEDIA_TYPE`；空 PNG 400 `INVALID_REQUEST` |
| 建会话/metadata/默认 quality | PASS | idle、credits_used=0；默认 medium；task_id 保留 |
| low/metadata 超限或非字符串 | PASS | 均 400 `INVALID_REQUEST` |
| 文本超 20KB | PASS | 413 `PAYLOAD_TOO_LARGE` |
| 发需求、公开 ID、轮询 | PASS | 202 返回 user/assistant ID；after=user_message_id 包含 user 与 assistant；中间可见 finish=null |
| 忙碌时另发新需求 | PASS | 409 `SESSION_BUSY` |
| 处理中同键同内容重发 | **FAIL** | 返回 409 `SESSION_BUSY`，文档要求 202 原 ID |
| 处理中同键不同内容 | **FAIL（错误码差异）** | 返回 409 `SESSION_BUSY`，没有识别成 `DUPLICATE_CLIENT_MESSAGE_ID` |
| 已结束同键同内容重发 | PASS | 202 且两个 ID 完全相同，无重跑 |
| 已结束同键不同内容 | PASS | 409 `DUPLICATE_CLIENT_MESSAGE_ID` |
| 单选确认卡 | PASS | 线上控制台讲稿/字幕卡提交 200，轮询变 answered |
| 多选与自由输入确认卡 | PASS | API 与跨域浏览器均验证；回答甲/丙及自由文本被正确复述 |
| 拒绝卡片、继续轮询 | PASS | 200，status=rejected，最后回复取消说明，finish=stop |
| 相同答案重复提交/重复拒绝 | **FAIL（文档不一致）** | 均 200，文档承诺已处理统一 409 |
| 对已答卡改答案/再拒绝 | PASS | 409 `INTERACTION_RESOLVED` |
| 10 分钟确认卡超时 | **FAIL（未配置期限）** | 实测多个卡片 expires_at=null；数据库亦 null；工具调用未传 expires_at |
| custom=false 不接受自定义答案 | NOT VERIFIED | 即使测试需求明确禁止，实际工具 schema 无 custom 字段，生成卡片均 custom=true，无法覆盖该分支 |
| 空闲中止 | PASS | 200、aborted=false |
| 活跃中止 | PARTIAL | aborted=true、会话变 idle；但早期中止轮询 assistant.finish=stop，不是 aborted |
| 积分累计 | PASS（读数） | GET 会话返回非零累计值；与 usage_events 汇总继续核对 |
| 下载地址文档命令 | **FAIL** | 第 9 节 `curl -I` 发 HEAD，线上 405；GET 才是 302 |
| 未知路由统一错误体 | **FAIL（边缘分支）** | GET /v1/not-a-route 是 404 `{"detail":"Not Found"}`，非统一 error 对象 |
| JSON 格式错误、limit=0、未知 cursor | PASS | 400/400/404，统一错误体 |
| 超过 200MB 上传 | SKIP | 未实际传输 200MB 边界文件 |
| 402 积分不足 | SKIP | 未改变测试账户积分或套餐 |
| 5 并发会话 | 配置核对，压力边界未测 | Key policy max_concurrent_sessions=5；未为压测额外创建 6 个执行任务 |
| 60/minute 限流 | PASS | 同一窗口前 60 次成功，第 61 次 429 RATE_LIMITED；Limit=60、Remaining=0、Retry-After=30 |
| 官方脚本 preflight | PASS | 4/4，退出 0 |
| 对应单元测试 | PASS | test_v1_routes / test_v1_public / test_api_key_auth / test_gaode_flow_script 合计 38 passed |
| 官方脚本完整生成 | 未单独运行 | 避免另开第二次付费生成；真实成片通过浏览器执行。脚本主流程在首个 finish 非 null 时退出，受同一长任务问题影响 |

## 需修复问题与复现证据

### 1. 长视频任务没有完成，外部轮询却已经结束（阻断自动成片验收）

- 浏览器会话：`ses_7YBWW0R2FK3G33C33HR5E12D01`。
- 真实生成轮 user：`msg_01M33ZJMBNFWPBENS8MGEX1317`，assistant：`msg_01M33ZJMBNFWPBENS8MGEX1318`。
- 视频任务：`video_01M33ZMP40GDM7SRZDXV12YNWH`；2026-09-22 15:17:47 创建；模型 `wan3.0-video`，1080p，10 秒，attempt=1。
- 大约 15:20，assistant 已 finish=stop，公开 files 为空。回复称任务仍 in_progress，要求用户“回复任意消息”再查。
- 15:21:38 服务端只读查询仍见同一视频任务 in_progress。
- 控制台 `gaode-console.html:166` 遇任何非 null finish 就停轮询；脚本同样退出，因此高德照文档执行无法自动等到 final 文件。
- 需要把公共轮次完成状态与视频最终交付关联，或明确提供持久任务轮询/回调契约，并实现持续交付。单纯改控制台“继续轮询”未必够：恢复模块的资产附件也可能依赖下一次工具调用。

### 2. 幂等判定在忙碌判定之后

- `flow-results.json`：同内容重发 request_id `req_01M33Z8PSPF703E4Q8XQGRG077` → 409 SESSION_BUSY。
- `backend/api/v1/messages.py:128` 在 `accept_inbox_item()` 之前拒绝 active session。
- 应先查同键请求并返回原 receipt，再对真正新消息执行 busy、余额与并发校验。

### 3. 卡片“10 分钟”尚未接入

- `runtime.out`：Key policy interaction_timeout_s=600；实际四张卡 expires_at 均 null。
- `backend/tool/question_tool.py:52` 调用 ask 未传期限；同一工具 schema 也不暴露 custom 字段。
- 浏览器真实呈现“超时 null”。没有声称已经等满 10 分钟，但数据库无期限与调用路径足以证明当前卡片未配置该超时。

### 4. 重复答卡响应与文档不同

- `answer-results.json`：第一次和第二次相同答卡均 200，重复拒绝亦 200。
- 改答案或在答卡后拒绝，才返回 409（`post-results.json`）。
- `question/question.py:309` 明确对相同决定做幂等成功处理。需决定保留幂等语义并修正文档，还是在 `/v1` 层实现当前契约。

### 5. 早期中止被当成正常完成

- `post-results.json`：`ses_7YBWW0FBXQMZG3ZV450PQ3ZQ7B` 发消息后立即 abort → 200/aborted=true；3 秒后 idle。
- 同一轮 assistant.parts=[]、finish=stop。
- `api/v1/public.py:225` 只认持久消息 finish；没有终态行且会话不活跃时 fallback 为 stop，需要考虑中止记录。

### 6. 交付文档/错误体小项

- 下载命令应改为 `curl -sS -D - -o /dev/null -H "Authorization: Bearer $KEY" "$BASE/files/$FIL/content"`，避免 HEAD。
- 未知路由的 Starlette 404 没有走目前 FastAPI HTTPException handler；OPTIONS 响应也没有 X-Request-Id。文档的“每个响应/所有错误”需实现或限定范围。
- 方案首段口头说 medium=720p，但随后的报价卡和数据库已纠正为 1080p；不能把首段文本当作真实提交参数。本次真实 job 已核对 1080p。

## 证据索引

证据位于同目录 `gaode-acceptance-20260922/`：

- `http-results.json`：鉴权、CORS、上传、下载、参数/错误体。
- `flow-results.json`：发消息、忙碌、运行中重发/冲突。
- `answer-results.json`：答卡、重复答卡、拒绝与重复拒绝。
- `post-results.json` / `settled-poll.json`：终态、结束后幂等、中止、积分。
- `runtime.out`：线上只读数据库/代码哈希证据；云助手调用 `t-sh06xta17cwf2f4`。
- `official-preflight.json`：官方脚本 4 项通过。
- `errors-extra.json`：未知路由、无效 JSON、分页参数错误。


## 控制台补充验证

- 跨域页面正常按每 2.5 秒轮询，成功渲染多选（甲/丙）与自由输入，答卡后恢复并显示模型复述。
- 不修改输入直接点“幂等重发”：202、原 user/assistant ID 一致。
- 修改输入框后点“幂等重发”：409 DUPLICATE_CLIENT_MESSAGE_ID（`req_01M33ZZTZXEVF0525NT8Q94YX9`）。按钮实际重新读取输入框，覆盖 state.lastBody，不能重放原请求；应冻结上次成功提交的 payload。此项为控制台实现问题，服务端此处按幂等规则拒绝是正确的。
- 限流实测第 61 个请求：`req_01M3400ZHE7EGHRXQVVDC07A9Q`，429 RATE_LIMITED，Retry-After=30。对应 `rate-results.json`。

## 真实视频与费用最终结果

- 仅一次生成，job `video_01M33ZMP40GDM7SRZDXV12YNWH`、attempt=1，未重试/改模型/重新生成。
- 任务 15:17:47 创建，15:28:17 本地完成，约 **10 分 30 秒**；上游 completed、progress=100，路由指纹相符。
- 原文件 `fil_01M33ZMP40GDM7SRZDXV12YNWJ`；下载刷新 302，OSS 下载 200。
- 视频大小 **36,812,935 字节**；SHA-256 `e34584ec3776dd70687c7a90b7e662601a7c0ce539c6e0681f551b7a73689d70`。
- MP4 元数据：**1080×1920、10.031 秒、AVC 视频与 AAC 音轨**。Chrome 实际播放出竖屏人物/西湖画面，抽查画面未见字幕；未逐字验听口播。
- 本地产物：`/Users/wxy/Documents/Codex/gaode-acceptance-20260922/gaode-acceptance-10s.mp4`。
- 重新获取下载地址的两次 GET 均为 302；签名随刷新变化，Expires 距当前约 86400 秒；签名 URL GET 200 且 Content-Length 与下载文件一致（GET 签名不能改用 HEAD，改方法会被 OSS 拒绝）。
- usage_events：video_generate 仅 **1 笔 charged，12.000000000000 积分**；主浏览器会话聊天/建议另用 3.008049819674 积分，累计 **15.008049819674**。其他确认卡测试会话分别 0.180022274834 和 0.064019059605；本次测试已记录的总用量约 **15.2521 积分**。
- 证据：`video-status.out`、`provider-status.out`、`video-download.json`、`video-metadata.json`、`refresh-check.json`。

### 7. 取回视频后仍没有 final 文件（阻断）

- 后台完成时，原轮次及第一次补查轮次仍无 file 部件；`video-completed-before-manual-fetch.json` 记录了该状态。
- 15:29 再发送仅取回已有产物的消息后，浏览器出现两个视频附件，角色都为 **intermediate**；同一轮 finish=stop。
- 文件 ID 分别为 `fil_01M33ZMP40GDM7SRZDXV12YNWJ` 和 `fil_01M3409XC29DSQRJ4G2H9DKAE9`，文件名和大小相同；未做第二个文件的全量哈希比较。
- 公开 assistant `msg_01M3409HWFVQ7NPEXCV110TCRR` 的文字声称“成片已交付”，但协议中的 `file.role=final` 始终缺失，现有脚本 final_file_delivered 必然为 false。见 `ui-final-snapshot.json`。
- 路径原因：`tool/video_production.py:883` 把 segment 的附件记为 intermediate；`share_file.py:79` 的 result 也经 `api/v1/public.py` 默认映射成 intermediate。需明确最终交付动作及角色，而非一律把所有中间镜头改成 final。

## 交付建议

先修复异步生成等待/自动挂载 final、运行中幂等及确认卡期限，再做一次完整回归；同步纠正重复答卡语义、HEAD 下载命令、中止终态与控制台重放正文。PR #58 描述本来就将交互策略 C/高德预置 D 列为另行交付，不能仅因 A/B/E 或单测通过就宣布整套联调验收完成。

本报告只记录实测与建议，未修改产品实现、未提交 commit、未发布 PR 评论。
