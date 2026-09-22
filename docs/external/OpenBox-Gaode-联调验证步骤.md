# OpenBox 视频接口 · 联调验证步骤

| 项 | 内容 |
|---|---|
| 适用方 | 高德 |
| 接口版本 | `/v1`，对应《OpenBox 视频创作开放接口文档 v1.1》 |
| 测试环境 | `https://gaode.bossipai.com.cn/v1` |
| 日期 | 2026-09-22 |

本文按接口文档第 9 节的调用时序，给出每一步的命令、预期响应和常见错误，可以照着逐条核对。

## 0. 准备

```bash
export BASE=https://gaode.bossipai.com.cn/v1
export KEY=obx_sk_...        # OpenBox 签发的测试 Key，只放在服务端
```

- 所有请求带 `Authorization: Bearer $KEY`，每个响应都有 `X-Request-Id`，联调时报问题请附上。
- 限额：60 次/分钟（超限 `429` + `Retry-After`）、同时处理中的会话 5 个（`429 CONCURRENT_LIMIT_EXCEEDED`）、确认卡 10 分钟超时。
- 素材：jpg / png / webp / mp4 / mov / mp3 / wav / m4a，单文件 ≤ 200 MB。
- 两种验证方式任选：下面的 `curl` 逐步核对，或第 11 节的控制台页面 / 第 12 节的脚本。

## 1. 连通性与错误体

```bash
# 坏 Key → 401
curl -sS -D - -o /dev/null -H "Authorization: Bearer obx_sk_invalid" $BASE/sessions/ses_x | grep -E "HTTP|X-Request-Id"
# 正确 Key，不存在的会话 → 404，且错误体是统一格式
curl -sS -H "Authorization: Bearer $KEY" $BASE/sessions/ses_does_not_exist
```

预期：
```json
{"error":{"code":"NOT_FOUND","message":"Session not found","request_id":"req_01…"}}
```

## 2. 上传素材

```bash
curl -sS -H "Authorization: Bearer $KEY" -F "file=@road.mp4" $BASE/files
```

预期 `201`：
```json
{"id":"fil_01…","filename":"road.mp4","mime_type":"video/mp4","size":10485760,"duration_s":null,"created_at":"2026-09-22T08:00:00Z"}
```

- 首期 `duration_s` 固定为 `null`（文档 §11 遗留项）。
- 大文件按实际传输时间计：200 MB 通常 1–3 分钟，服务端等待上限 15 分钟，请把客户端超时设为 15 分钟以上。上传接口没有幂等键，超时后重传会产生一个内容相同的新文件（30 天后自动清理），不影响使用。
- 不支持的格式 → `415 UNSUPPORTED_MEDIA_TYPE`；超过 200 MB → `413 PAYLOAD_TOO_LARGE`。

## 3. 创建会话

```bash
curl -sS -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" $BASE/sessions \
  -d '{"title":"五一杭州自驾攻略口播","quality":"high","metadata":{"task_id":"t_456"}}'
```

预期 `201`，`status` 为 `idle`，`credits_used` 为 `"0"`。

- `quality` 缺省 `medium`；`low` 为 768p 预览档，已开放。
- `metadata` 最多 16 个键，值必须是字符串，超出 → `400 INVALID_REQUEST`。

## 4. 发送需求

```bash
export SES=ses_01…   # 上一步的 id
curl -sS -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" $BASE/sessions/$SES/messages \
  -d '{"text":"帮我做一条 25 秒的五一杭州自驾攻略口播，用我上传的路况视频做素材","attachments":["fil_01…"],"client_message_id":"t_456-1"}'
```

预期 `202`：
```json
{"session_id":"ses_01…","user_message_id":"msg_01…","assistant_message_id":"msg_01…"}
```

验证点：
- 处理中再发一条 → `409 SESSION_BUSY`。
- 用**同一个** `client_message_id` 重发同样内容 → 仍是 `202`，且两个 id 与第一次完全相同，不会重跑。
- 同一个 `client_message_id` 但内容不同 → `409 DUPLICATE_CLIENT_MESSAGE_ID`。
- 文本超过 20 KB → `413`。

## 5. 轮询消息

```bash
export USER_MSG=msg_01…   # 202 里的 user_message_id
curl -sS -H "Authorization: Bearer $KEY" "$BASE/sessions/$SES/messages?after=$USER_MSG"
```

每 2–3 秒一次。响应 `data` 里固定两条：`role=user` 的原文，和 `role=assistant` 的回复（id 就是 202 里的 `assistant_message_id`，发送后立刻可见，`parts` 从空数组开始增长）。

处理规则：
- `text` 部件：同一个 `id` 的 `text` 会变长，按 id 覆盖显示。
- `question` 部件且 `status=pending`：渲染确认卡，等用户选择，见第 6 节。
- `file` 部件且 `file.role=final`：成片，`url` 24 小时有效，请拉取后自行存储。
- assistant 的 `finish` 不为 `null` 即本轮结束：`stop` 完成、`error` 出错（看 `error.code`）、`aborted` 被中止。
- **视频生成期间 `finish` 保持 `null`**：单镜生成通常 5–10 分钟，其间 AI 的文字可能先说"任务处理中"，请继续轮询，不要停；生成完成后成片会自动挂到同一条 assistant 消息里（`file.role=final`），随后 `finish` 才变为 `stop`。会话 `status` 在此期间也保持 `busy`。
- 成片判定：`role=final` 的文件是成片；若一轮以 `stop` 结束却没有显式 `final`，最后一个视频文件即为成片（服务端已按此规则标记）。

## 6. 回复 / 拒绝确认卡

```bash
export QID=qst_01…
# 按问题顺序，每题一个数组；单选长度 1；custom=true 的题可填自由文本
curl -sS -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" $BASE/sessions/$SES/questions/$QID \
  -d '{"answers":[["可以"],["带转场和动效（约 120 积分）"]]}'
# 拒绝
curl -sS -X POST -H "Authorization: Bearer $KEY" $BASE/sessions/$SES/questions/$QID/reject
```

预期 `200 {"ok":true}`；回复后继续轮询，卡片 `status` 变为 `answered`。已回复、已拒绝或已超时的卡再次提交（包括重复提交同样的答案）→ `409 INTERACTION_RESOLVED`，此时刷新卡片状态即可。答案不在选项里且题目 `custom=false` → `400`。每张卡 `expires_at` 为出卡后 10 分钟，超时按拒绝处理。

一次创作通常两张卡（讲稿确认、分镜与报价确认）；**确认报价前不产生生成费用**。

## 7. 中止

```bash
curl -sS -X POST -H "Authorization: Bearer $KEY" $BASE/sessions/$SES/abort
```

预期 `200 {"ok":true,"aborted":true}`；会话空闲时 `aborted=false`。

## 8. 会话状态与积分

```bash
curl -sS -H "Authorization: Bearer $KEY" $BASE/sessions/$SES
```

`status` 为 `idle | busy | error`，`credits_used` 为本会话累计消耗。积分不足时发送需求返回 `402 INSUFFICIENT_CREDITS`。

## 8a. 历史会话（可选）

```bash
curl -sS -H "Authorization: Bearer $KEY" "$BASE/sessions?limit=20"
```

返回该 Key 名下的会话，最新在前：`{"data":[…会话结构同 6.2…],"next_cursor":"…","has_more":true}`，用 `cursor=<next_cursor>` 翻页。接口文档 v1.1 未列此端点，属于额外提供，用于后台查看与控制台"最近会话"。

## 9. 重新获取下载地址

```bash
curl -sS -D - -o /dev/null -H "Authorization: Bearer $KEY" "$BASE/files/fil_01…/content" | grep -iE "HTTP|location"
```

预期 `302`，`Location` 是新的 24 小时签名地址（请用服务端 GET 调用，不要用 HEAD，也不要在浏览器里带 Key 打开；签名地址本身也只接受 GET）。

## 10. 错误码核对

| 场景 | HTTP | code |
|---|---|---|
| Key 缺失或无效 | 401 | UNAUTHORIZED |
| 参数错误 | 400 | INVALID_REQUEST |
| 积分不足 | 402 | INSUFFICIENT_CREDITS |
| 会话 / 文件不存在 | 404 | NOT_FOUND |
| 会话处理中 | 409 | SESSION_BUSY |
| 确认卡已处理 | 409 | INTERACTION_RESOLVED |
| 幂等键内容冲突 | 409 | DUPLICATE_CLIENT_MESSAGE_ID |
| 文本 / 文件超限 | 413 | PAYLOAD_TOO_LARGE |
| 素材格式不支持 | 415 | UNSUPPORTED_MEDIA_TYPE |
| 频率超限 | 429 | RATE_LIMITED（看 `Retry-After`） |
| 并发会话超限 | 429 | CONCURRENT_LIMIT_EXCEEDED |

## 11. 控制台页面（可自行托管）

`gaode-console.html` 是一个单文件页面，不依赖任何外部资源：填接口地址和 Key 后，可以点按钮完成上传（可多选）→ 建会话（或从"最近会话"选择 / 输入会话 id 打开）→ 发需求 → 自动轮询并渲染整段历史（文本 / 确认卡 / 成片）→ 答卡 / 拒绝 → 中止，并显示每次请求的状态码与 `X-Request-Id`。打开一个仍在处理中的会话会自动继续轮询。

- 我们也放了一份在 `https://gaode.bossipai.com.cn/gaode-console.html`。
- 直接双击打开本地文件即可使用（接口已允许跨域）。
- Key 只在页面内存里，不写入本地存储；这是联调工具，不要放到面向终端用户的地方。

## 12. 参考脚本

`gaode_flow_e2e.py`（Python 3.10+，只依赖 `httpx`）会自动跑完整个时序并逐项断言：

```bash
pip install httpx
python gaode_flow_e2e.py --base-url $BASE --key $KEY --material road.mp4 \
  --text "帮我做一条 25 秒的五一杭州自驾攻略口播，用我上传的路况视频做素材" \
  --answers interactive        # 逐张卡在终端里选；用 first 则自动选每题第一个选项
python gaode_flow_e2e.py --base-url $BASE --key $KEY --preflight-only   # 只验 Key 与错误体
```

全部通过退出码 0，stdout 是 JSON 摘要（会话 id、卡片与答案、文件、轮询次数、耗时）。

## 13. 联调期已知情况

- 首次调用后台可能有几分钟冷启动（我们会提前预热），之后一轮文本响应约 10 秒；单镜视频生成约 5–10 分钟，多镜并行，端到端约 8–15 分钟。
- 处理中用同一个 `client_message_id` 重发同样内容，返回 `202` 与原 id（不算新需求，也不受忙碌限制）。
- 未知路径或方法一律 `404 NOT_FOUND`，错误体同样是统一格式。
- `progress` 部件首期不发；文件的 `duration_s / width / height` 为 `null`。
- 确认卡超时 10 分钟，超时按拒绝处理，AI 停下并说明，再发消息可继续。
