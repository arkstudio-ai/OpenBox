# OpenBox × 高德：接口测试操作手册

版本：1.0 ｜ 日期：2026-09-22 ｜ 配套：《OpenBox-Gaode-接口测试规范》

建议顺序：浏览器非生成测试 → API 契约测试 → 获准的一次真实生成 → 下载/积分核对 → 填写报告。首次操作先做前两部分；不要为了“快速跑通”自动确认所有报价。

## 1. 准备与目录

解压测试包后得到：

- 两份文档：本手册和接口测试规范。
- `gaode-console.html`：可本地运行的控制台。
- `gaode_flow_e2e.py`：接口流程测试脚本。
- `materials/red/same-name.png`、`materials/blue/same-name.png`：有效的同名同大小不同内容图片。
- `prepare_materials.py`：重新生成测试素材，无第三方依赖。
- `测试记录模板.csv`：逐项登记结果。

公网控制台：https://gaode.bossipai.com.cn/gaode-console.html

Key 由接口负责人单独提供。终端命令使用 Bash，依赖 curl、jq、Python 3.10+；完整脚本另需 httpx。Windows 可用 WSL/Git Bash。不要把真实 Key 填进文档、截图或代码仓库。

在解压目录执行：

```bash
# 进入 bash 后执行以下命令
export BASE='https://gaode.bossipai.com.cn/v1'
read -r -s -p 'API Key: ' OPENBOX_API_KEY
printf '\n'
export OPENBOX_API_KEY
export RUN_ID="gaode-test-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "results/$RUN_ID"
export OUT="results/$RUN_ID"
python3 prepare_materials.py
```

所有请求的 BASE 已含 /v1；Key 用 Bearer 鉴权。日志里保留 X-Request-Id，发送问题时不要发送 Authorization 或签名 URL。

## 2. 浏览器测试：约 5–10 分钟，不生成媒体

### 2.1 打开并连通

1. 打开公网控制台，填 BASE 和 Key，点击“连通性检查”。
2. 坏 Key 应得到 401；正确 Key 的探针访问不存在会话可能显示 404，这是鉴权后资源不存在，不代表服务断线。
3. 切回正确 Key；查看请求日志状态码与 request_id。

需要验证跨域时，在测试包目录执行 `python3 -m http.server 8986 --bind 127.0.0.1`，打开 `http://127.0.0.1:8986/gaode-console.html`，BASE 仍填公网地址。该本地服务仅供联调，结束时在终端 Ctrl+C。

### 2.2 多素材与 low

1. 文件选择器同时选择两张 `same-name.png`（位于 red、blue 两个目录）；上传后应显示两个不同 fil ID。
2. 确保两个素材都勾选，标题填 RUN_ID，档位选“low · 灵活 768p”，task_id 填唯一测试标识。
3. 创建会话，预期 idle、quality=low；记录 ses ID。
4. 发送下面的测试文字：

> 这是接口验收，不生成视频、图片、音频，不联网。先确认收到的附件数量。请用 question 工具出一张三题确认卡：单选 A/B，custom=false；多选甲/乙/丙，custom=false；自由输入，无选项，custom=true。等我回答后准确复述答案并结束，不再出卡。

5. 确认公开历史有两个 input 文件；模型应正常出卡。上传返回 201 本身不证明图片可被模型解码。

### 2.3 草稿、重绘与幂等

1. 选择 B、甲和丙，自由输入“草稿保留验收123”。
2. 等待至少 20 次轮询（约 50 秒），三种答案都应保留；记录轮询计数和界面。
3. 修改左侧需求输入文字，点击“幂等重发”，应返回 202 且原 user/assistant ID 一致；不能把修改后的文字发出去。
4. 点击“提交回答”，预期 200、卡片 answered、控件禁用；重绘后选择仍保留，模型准确复述答案，finish=stop。
5. 另开一轮出卡后点“拒绝”；预期 rejected；重复处理用 API 验证应为 409。

### 2.4 历史会话

点“最近会话”的刷新并选择旧会话，或输入已有 ses ID 点“打开”。核对完整已显示的多轮文本、卡片、文件，不能误操作旧报价。

**当前已知问题（8f3e54a）**：同一会话再次点“打开”可能清空历史区。临时做法：刷新整个页面，重新填 Key 和 ses ID，再打开一次。API 历史仍在，**不要因此重发付费生成请求**。修复后按规范 H04 再验。

当前控制台只取 200 条消息、最多展示最近 50 个会话；长历史用 API 分页验证。

## 3. API 手动测试

以下命令保存响应头和正文，便于事后核对。先检查 HTTP 状态，再读取 JSON 字段；不要在失败后继续拿 null ID 发请求。

### 3.1 鉴权和跨域

```bash
curl -sS -D "$OUT/bad-key.headers" -o "$OUT/bad-key.json" \
  -H 'Authorization: Bearer invalid' "$BASE/sessions/ses_missing"
cat "$OUT/bad-key.json"   # 401 UNAUTHORIZED

curl -sS -D "$OUT/missing.headers" -o "$OUT/missing.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" "$BASE/sessions/ses_missing"
cat "$OUT/missing.json"   # 404 NOT_FOUND

curl -sS -D "$OUT/cors.headers" -o "$OUT/cors.txt" -X OPTIONS \
  -H 'Origin: https://partner.example' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: authorization,content-type' "$BASE/sessions"
```

核对 error.code、error.request_id 与响应头；CORS 最终仍需浏览器实测，不只看 OPTIONS。

### 3.2 上传两张有效图片

```bash
curl -sS -D "$OUT/upload1.headers" -o "$OUT/upload1.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" \
  -F 'file=@materials/red/same-name.png;type=image/png' "$BASE/files"
curl -sS -D "$OUT/upload2.headers" -o "$OUT/upload2.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" \
  -F 'file=@materials/blue/same-name.png;type=image/png' "$BASE/files"
export F1=$(jq -er '.id' "$OUT/upload1.json")
export F2=$(jq -er '.id' "$OUT/upload2.json")
```

两次应均为 201，ID 不同。多素材是两次 POST /files，然后一次 messages 带两个 ID，不是未声明的“批量上传接口”。

### 3.3 创建 low 会话

```bash
jq -n --arg task "$RUN_ID" \
  '{title:$task,quality:"low",metadata:{task_id:$task}}' > "$OUT/session-body.json"
curl -sS -D "$OUT/create.headers" -o "$OUT/session.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @"$OUT/session-body.json" "$BASE/sessions"
export SES=$(jq -er '.id' "$OUT/session.json")
```

预期 201、low、idle、credits_used="0"。如需核对 MiniMax-H3/768p，由服务端维护人员提供该 ses ID 的只读配置/作业证据；公共响应没有模型字段，不能从中凭空推断。

### 3.4 发消息、重发、冲突

```bash
jq -n --arg f1 "$F1" --arg f2 "$F2" --arg cid "$RUN_ID-1" \
  '{text:"接口测试，不生成媒体、不联网。请确认收到两张附件，然后只用 question 工具出三题卡：单选 A/B（custom=false）；多选甲乙丙（custom=false）；自由输入（custom=true，无选项）。等回答后复述并结束。",attachments:[$f1,$f2],client_message_id:$cid}' \
  > "$OUT/message-body.json"

curl -sS -D "$OUT/send.headers" -o "$OUT/accepted.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @"$OUT/message-body.json" "$BASE/sessions/$SES/messages"
export USER_MSG=$(jq -er '.user_message_id' "$OUT/accepted.json")
export ASSISTANT_MSG=$(jq -er '.assistant_message_id' "$OUT/accepted.json")

# 同键同正文：202，原 ID 一致
curl -sS -D "$OUT/replay.headers" -o "$OUT/replay.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @"$OUT/message-body.json" "$BASE/sessions/$SES/messages"

# 同键不同正文：409 DUPLICATE_CLIENT_MESSAGE_ID
jq '.text="不同的正文"' "$OUT/message-body.json" > "$OUT/conflict-body.json"
curl -sS -D "$OUT/conflict.headers" -o "$OUT/conflict.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @"$OUT/conflict-body.json" "$BASE/sessions/$SES/messages"
```

新键、不同消息在忙碌时应返回 SESSION_BUSY。网络超时不知道是否提交成功时，先保留原键重放；不要马上换键创建第二个作业。

### 3.5 轮询与答卡

```bash
curl -sS -D "$OUT/poll.headers" -o "$OUT/poll.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" --get \
  --data-urlencode "after=$USER_MSG" "$BASE/sessions/$SES/messages"
jq --arg mid "$ASSISTANT_MSG" '.data[] | select(.id==$mid)' "$OUT/poll.json"
export QID=$(jq -er '[.data[].parts[] | select(.type=="question" and .status=="pending")][0].question_id' "$OUT/poll.json")
```

每 2.5 秒拉取一次；尚未出卡时 QID 提取失败属正常，继续等待。after 结果可能包括后续轮次，始终按 assistant ID 选择目标轮，不假定永远只有两条消息。

先核对本次实际卡片的题目、顺序、选项。只有确实是上述三题时才提交这个示例：

```bash
printf '%s\n' '{"answers":[["B"],["甲","丙"],["接口自由输入验收"]]}' > "$OUT/answer-body.json"
curl -sS -D "$OUT/answer.headers" -o "$OUT/answer.json" \
  -H "Authorization: Bearer $OPENBOX_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @"$OUT/answer-body.json" "$BASE/sessions/$SES/questions/$QID"
```

预期 200。原样再调用一次应为 409 INTERACTION_RESOLVED。另一个新卡可调用：

```bash
curl -sS -X POST -H "Authorization: Bearer $OPENBOX_API_KEY" \
  "$BASE/sessions/$SES/questions/$QID/reject"
```

拒绝已处理卡也为 409。超时用例另开卡，完整等待 expires_at 到点，期间不答卡/拒绝/中止；预期 timeout，之后处理为 409。

### 3.6 查询历史与积分、中止

```bash
curl -sS -H "Authorization: Bearer $OPENBOX_API_KEY" \
  "$BASE/sessions/$SES" > "$OUT/status.json"
curl -sS -H "Authorization: Bearer $OPENBOX_API_KEY" \
  "$BASE/sessions?limit=2" > "$OUT/history-page1.json"
export CURSOR=$(jq -er '.next_cursor // empty' "$OUT/history-page1.json")
# 仅在 has_more=true 且拿到 CURSOR 时执行
curl -sS -H "Authorization: Bearer $OPENBOX_API_KEY" --get \
  --data-urlencode 'limit=2' --data-urlencode "cursor=$CURSOR" \
  "$BASE/sessions" > "$OUT/history-page2.json"

# 仅对自己这次创建且确实要中止的测试会话执行
curl -sS -X POST -H "Authorization: Bearer $OPENBOX_API_KEY" "$BASE/sessions/$SES/abort"
```

忙碌中止预期 aborted=true，之后 assistant.finish=aborted；空闲中止 false。中止后仍检查账单和作业状态，不假定已经取消上游收费。列表翻页直到 has_more=false；公开消息分页用最后一条消息 ID 作 after，并对包含边界消息的重复 ID 去重。

## 4. 一次真实成片验收

1. 新建 high/medium 会话（正式 1080p 成片）；low 仅验证 768p 预览。
2. 要求先策划、免费估算并出报价卡，当前不要执行媒体生成。
3. 负责人核对具体模型、分辨率、画幅、时长、字幕/音频、生成次数、积分。**6 积分只是历史 5 秒测试的报价，不是本次价格承诺。**
4. 获准后确认一次报价并明确“只生成一次，不自动重试、不新增其他付费任务，报价变化先停止”。
5. 保存 202 的 assistant ID，持续轮询；模型文字可能说“回复继续”，仍以结构化 finish 和 final 为准，不另发重复生成。
6. 确认自动出现唯一 final 文件；下载并播放，核对规格和计费。聊天/建议费用单列，不混入视频单价。

### 安全刷新并下载

把真实 final 的 file.id 赋给 FIL；使用两个独立请求，避免把 API Bearer 转发到存储域名。

```bash
export FIL='替换为实际的fil_ID'
python3 -m venv .venv
. .venv/bin/activate
python -m pip install httpx
python - <<'PY'
import os, pathlib, hashlib, httpx
base = os.environ['BASE'].rstrip('/')
fid = os.environ['FIL']
r = httpx.get(f'{base}/files/{fid}/content',
              headers={'Authorization': 'Bearer ' + os.environ['OPENBOX_API_KEY']},
              follow_redirects=False, timeout=30)
assert r.status_code == 302, (r.status_code, r.text)
# 第二个请求不带 API Key，不输出签名地址。
with httpx.stream('GET', r.headers['location'], timeout=120, follow_redirects=True) as response:
    response.raise_for_status()
    target = pathlib.Path(os.environ['OUT']) / (fid + '.mp4')
    digest = hashlib.sha256()
    with target.open('wb') as output:
        for chunk in response.iter_bytes():
            output.write(chunk); digest.update(chunk)
print('file:', target, 'bytes:', target.stat().st_size, 'sha256:', digest.hexdigest())
PY
```

适用于本次 MP4 成片。下载其他类型时用对应扩展名。不要使用 HEAD/curl -I，也不要用 GET 签名测试 HEAD。原文件链接可能过期，刷新后重新下载即可，不需重新生成。

## 5. 自动化脚本

在测试包目录及已激活的 httpx 环境执行：

```bash
# 预检会创建测试会话，但不发起媒体生成
python gaode_flow_e2e.py --base-url "$BASE" --preflight-only \
  > "$OUT/preflight.json"

# 完整流程会涉及生成；确认预算后才运行，始终显式选 interactive
python gaode_flow_e2e.py --base-url "$BASE" \
  --quality medium --title "$RUN_ID" --task-id "$RUN_ID" \
  --material materials/red/same-name.png \
  --material materials/blue/same-name.png \
  --text '请先规划一条5秒竖屏无字幕视频并给准确报价，等待我确认再生成；只生成一次，不自动付费重试。' \
  --answers interactive --poll-interval 2.5 --deadline 1800 \
  --download-dir "$OUT/downloads" > "$OUT/e2e.json"
```

- 脚本读取 OPENBOX_API_KEY，无需通过 --key 把 Key 写进命令历史。
- **默认 answers=first，会自动选首项，可能批准收费。不要省略 --answers interactive。**脚本也没有自动金额上限保护，每张报价必须人工核对。
- 当前 --quality 的帮助文字仍可能只写 high/medium，服务端实际接受 low；要验 low 可显式指定 --quality low。
- deadline 到点脚本会尝试中止自己创建的会话；不保证上游已提交媒体任务取消或退款。
- 退出码 0 看 JSON checks 的具体内容；preflight=4/4 不能代替真实成片、浏览器草稿、历史、限流等验收。日志可能包含签名地址，分享前脱敏。

## 6. 常见问题与收尾

| 现象 | 处理 |
|---|---|
| 401 | 检查 BASE、Key 和 Bearer 格式，不要把 Key 发进群排错 |
| 403 | 查作用域/资源权限；不要绕过隔离读取其他身份资源 |
| 409 SESSION_BUSY | 继续轮询；重试原请求必须原 client_message_id 与原正文 |
| 409 INTERACTION_RESOLVED | 刷新卡片状态，不再提交 |
| 429 | 读取 Retry-After；暂停其他并行探针，共用 Key 的轮询也计数 |
| 上传201、模型拒收图片 | 验证图片能解码，使用套件有效 PNG；不要仅改扩展名伪造格式 |
| 点击同一会话打开后空白 | 当前已知控制台问题；刷新页面重填 Key/ID，不能重复生成 |
| 下载403 | 刷新 GET 签名；确认没有换成 HEAD、URL 完整且未过期 |
| stop但无final | 问答轮可以无文件；生成轮记录失败证据，不自行认定成片成功 |

结束时填写测试记录：预期/实际、版本、request_id、会话/消息/文件 ID、结果、费用。把 Key 从页面及环境移除：`unset OPENBOX_API_KEY`；停止本地预览服务。不要自动删除云端测试会话和素材，留给双方复查。

发送问题的最小信息：用例 ID + 时间/时区 + 环境版本 + request_id + session_id + 预期/实际 + 脱敏截图或 JSON。不要附 Key、Cookie、Authorization、签名 URL 或 token。
