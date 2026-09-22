# 高德联调回归验收（2026-09-22，第二轮）

当前结论：**尚未全部通过**。历史会话和普通多素材流程已可用，上一轮多数协议问题已修复；仍有 low 未开放、确认卡草稿丢失、同名同大小的不同素材误合并三个实测问题。真实视频与 10 分钟超时结果见文末。

## 环境

- 公网入口 https://gaode.bossipai.com.cn/v1；Chrome 真实页面 `/gaode-console.html`。
- 后端镜像 `openbox-backend:20260922-gaode-a4dbd55`，healthy；本地 HEAD `d38889a`。
- 在线控制台与工作区字节一致，SHA-256 `bb7df6dbf5bbc565518cdbe833d806555a88590314eb8cc323718c6d36e43a2d`。
- 未修改实现、部署、配置或 gw2；未删除云端资源。仅新建测试素材/会话、答卡、中止自己创建的测试。
- 用户单独授权本轮生成一次 Wan 3.0、1080p、9:16、5 秒，报价 6 积分。上一轮 12 积分视频未重复生成。

## 已通过

| 项目 | 实测证据 |
|---|---|
| 会话列表及分页 | 每页 10 条完整遍历得到 44 条、44 个唯一 ID，创建时间倒序；limit=2 翻页无重复；非法 cursor、limit=0/101 为 400 |
| 浏览器历史恢复 | 从最近会话列表打开旧视频会话，展示全部四轮 user/assistant、历史卡片和 final 文件；输入会话 ID 打开本轮多素材会话同样成功 |
| 普通多素材 | 两张不同文件名 PNG 上传均 201，同一需求 attachments 含两个 ID；模型确认收到两张，历史显示两张 |
| 多素材参数约束 | 重复 ID、超过 20 个 ID 返回 400 INVALID_REQUEST |
| 运行中幂等 | 新需求 409 SESSION_BUSY；同键同正文 202 且原 user/assistant ID；同键不同正文 409 DUPLICATE_CLIENT_MESSAGE_ID |
| 结束后幂等 | 202 返回原 ID |
| 控制台重发 | 修改输入框后点幂等重发，202，页面显示“一致 ✓”，没有发送被修改正文 |
| 确认卡 | custom=false 实际生效，非选项答案 400；单选、多选、自由输入正常；重复答卡/重复拒绝均 409 INTERACTION_RESOLVED |
| 卡片期限 | 出卡 09:26:55Z，expires_at=09:36:55Z，确实设置 600 秒；自动超时另测 |
| 中止 | 发送后立即 abort=true，随后 idle，assistant.finish=aborted；再次空闲中止 false |
| 错误体 | 未知 /v1 路由返回统一 NOT_FOUND；OPTIONS 200 且有 X-Request-Id |
| 已有成片去重与角色 | 旧视频的两个重复附件现在只返回一个，role=final |
| 下载刷新 | GET content 返回 302；新签名地址 GET Range 返回 206 video/mp4，原文件总大小 36,812,935；文档已改用 GET |
| 单元测试 | test_v1_routes、test_v1_public、test_video_resume_and_card_deadline、test_tool_payload_encoding 合计 41 passed |

官方脚本 preflight 为 4/4，但其中一项仍是 `low_quality_refused=true`，**不能当作 low 已支持的证据**。

## 未通过 1：low 尚未开放

- POST /sessions `{quality:"low"}` 实际 400 INVALID_REQUEST：`quality 'low' is not available on this integration; use high or medium`。
- 两次 request_id：`req_01M3470PE5JYVT4YKF8E4DMJHV`、`req_01M34782EYW1VTJK0Q3985FED5`。
- 本地 backend/api/v1/quality.py 的 ACCEPTED_QUALITIES 仍只有 high、medium；在线控制台下拉框同样只有两档。
- 联调验证步骤仍写 low 返回 400；脚本也把拒绝 low 当成成功。需要实现、部署、控制台和文档一起对齐。

## 未通过 2：带素材历史会让答卡草稿丢失

- 会话 `ses_7YBWVRZ8954FR1HBFXHPW444BZ`，当前卡 `qst_01M3475T0J5HQ4A27FY623G5CA`。
- 该会话第一轮含两张输入图片；浏览器第二轮请求自由输入卡。
- 轮询 #14 时输入“草稿保持测试123”，可见输入框已有文本；轮询 #23 读取同一输入框，value 为空。期间没有提交或主动清空。
- 每轮文件签名 URL 的 Expires/Signature 变化。`gaode-console.html:215` 把 URL 插入 HTML，`:222` 比较整个 HTML 后替换 messages.innerHTML，于是所有卡片控件被重建。
- 建议按消息/部件 ID 更新，或显式保存恢复答卡状态；不能仅依赖整段 HTML 相等来保留草稿。

## 未通过 3：同名同大小的不同素材被误合并

- 会话 `ses_7YBWVRPKT33XQPHGRT6S6280SJ`；消息 `msg_01M3479CDG0HN6JKN45Q95CNKD`。
- 上传两张内容不同的 2×2 PNG（红、蓝），均命名 `same-name.png`、均 82 字节；SHA-256 分别：
  - `0f7765869d223939f02d7dd514fb591face3ce6f938e944d95762ed6c2184979`
  - `29dec9c496efba13b674446e3c0d377f7969dd944dff620744f5d6f72bda8205`
- 两次上传 201，文件 ID 为 `fil_01M3479BVM32WF2BN39V05E8XY` 和 `fil_01M3479C19WZXMMWCSM855ES6B`；发需求 202。
- 模型文本明确列出两个附件 ID，但公开 user 消息 parts 只有第一个 input 文件。**素材实际送达模型，丢失的是公开历史展示。**
- 原因 `backend/api/v1/public.py:204` 无条件使用 `(filename,size)` 作为去重候选，也应用于 input。文件名和大小不能证明同一内容。
- 建议输入素材按 asset ID 保留；交付重复资产用明确来源关系或内容哈希去重。

## 边界与未完成项

- Chrome 文件权限最初阻塞；浏览器重连后权限已生效，成功一次选择两张 PNG，两次 POST /files 均 201。
- 未重新压测 200MB、余额不足、并发上限；本轮针对修复和新功能。未制造其他工作区凭据验证隔离。
- 历史列表 UI 仅显示最近 50 条，消息 UI 单次取 200 条且未跟随 has_more；本轮数据未超过这些阈值，没有将超长历史验收算通过。

## 真实异步成片：通过

- 会话 `ses_7YBWVRRS4A9VEXE44FXG2M685X`；实际生成请求 `msg_01M347BAVZMV8GP3J1YGPX5MKR`，公开 assistant `msg_01M347BAVZMV8GP3J1YGPX5MKS`。
- Job `video_01M347BJAZFR5CBJ85J25MVDPV`，wan3.0-video、1080p、9:16、5 秒、attempt=1。
- 后台 09:32:37Z 创建，09:37:16Z completed，耗时约 4 分 39 秒；公开轮次约 09:37:37Z 自动交付。
- 原模型运行约 09:36:34Z 已 stop、原始 session.status=idle，但公网仍保持 busy、finish=null。后台恢复完成后自动续跑，在同一公开 assistant 中出现一个 final 文件，之后结束。**没有再发送人工“继续/查进度”消息。**
- 浏览器从已有 ID 打开该 busy 会话，自动继续轮询，最终显示“本轮结束：stop”和 final 链接。
- 文件 `fil_01M347BJAZFR5CBJ85J25MVDPW`，GET content=302、签名下载=200；8,173,661 字节。
- SHA-256 `c0a0d577a39bce92b4cd7d100f5e3721a7b77945ede3dbdcf775bde56fdc288a`。
- MP4 解析与 Chrome 播放均验证：1080×1920、5.0 秒、AVC，无音轨。播放器 ended=true，画面为蓝色背景。
- 本地成片 `/Users/wxy/Documents/Codex/gaode-retest-20260922/gaode-retest-5s.mp4`。
- 视频账单只有 **1 笔 charged，6 积分**。该会话含报价/对话/建议共 6.761458319168；其他本轮测试会话合计 0.474472733956，**本轮总计约 7.2359 积分**。
- 小项：模型在后台等待阶段仍说“请回复继续”，但这次实际上已自动恢复；建议把这句提示同步更新，避免接入方误判需要再次发消息。

## 10 分钟自动超时：通过

- 会话 `ses_7YBWVRWTJ4TFQMX8ZSE9555TDF`，卡片 `qst_01M3473BCN6CCAGJDNKS4GHM7S`。
- 实际 created_at=09:28:08.342Z，expires_at=09:38:08.335Z；未提前答卡/拒绝/中止。
- 到期后轮询自动变为 status=timeout、assistant.finish=stop；会话 idle。
- 超时后提交答案返回 409 INTERACTION_RESOLVED，request_id=`req_01M347PNZNJSK2R7C37NP49X05`。

## 浏览器多素材补充：通过普通路径

- 浏览器一次选择 `multi-0.png` 与 `multi-1.png`，POST /files 两次 201，勾选状态均保留。
- 同一条消息发送 202，request_id=`req_01M347HCFXW0GZJM05WVEG7BS4`。
- 模型明确回复附件数量 2，逐一列出文件名；历史显示两个 input 文件。此前扩展权限阻塞已解除。
- 同名同大小误去重缺陷依然存在，不因普通路径通过而消除。

## 证据目录

同目录 `gaode-retest-20260922/` 保存脱敏请求结果、浏览器观察、完整成片轮询时间线、超时结果和后台只读账单。未保存 API Key、签名下载 URL 或下载 JWT。
