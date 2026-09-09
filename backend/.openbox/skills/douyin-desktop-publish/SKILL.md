---
name: douyin-desktop-publish
description: Publish a finished video to the person's 抖音 account through the cloud desktop's logged-in 创作者中心 (auto publish), with the QR package as the fallback. Use for 自动发布/后台发布/直接发到抖音 requests, for marketing-autopilot runs, or right after a video is delivered and the person wants it posted without scanning.
allowed-tools:
  - desktop_publish
  - desktop_login
  - douyin_publish
  - question
  - share_file
---

# 云电脑自动发布到抖音

这是过渡态的自动发布：用客户自己云电脑上已登录的创作者中心替他点「发布」。规则由 `desktop_publish` 强制执行（每日上限、最小间隔、发布时段、风控熔断），你不要绕开，也不要自己开浏览器。任何一步被拒都按工具给的指引走，不重试。

## 流程

1. **先看能不能自动发** `desktop_publish(action="precheck")`。
   - `can_auto_publish=true` → 第 3 步。
   - `mode=package`（部署默认、模版指定、或该账号已被风控停用）→ 直接走 [douyin-publish](../douyin-publish/SKILL.md) 的扫码投稿包流程，并用一句话告诉用户原因。
   - `login` 不是 ok → 让用户在云电脑重新登录：`desktop_login(action="open", site="douyin_creator")`，登录后 `desktop_login(action="probe", site="douyin_creator")` 确认 `bound`；用户不在场（定时任务）就改投稿包并在报告里写明「需重登」。
   - `budget=blocked` → 不在此刻发。对话里告诉用户 `next_allowed_at`，可以用 `schedule_at` 定时；定时任务里直接改投稿包或留到下次运行。
2. **拿到成片 asset_id**：刚交付的成片在 `share_file` 返回的元数据里；资源中心的文件用 `share_file(attach=false)` 登记。只发视频。
3. **拟文案**（创作者中心比投稿包更严）：
   - 标题 **≤ 30 字**，说清讲什么、给谁看，口语化，不写「AI 生成」之类与内容无关的话。
   - 简介 ≤ 1000 字，正文 1–3 句 + `topics` 3–5 个话题词（不带 #，工具会变成话题标签）。
   - `declaration` 固定 `ai`（AI 生成内容必须声明）；带明确带货/推广的内容仍选 `ai`（创作者中心只能单选）。
   - 跟热点的视频把热点词填进 `hot_word`（能否关联成功以返回的 `hot_word_attached` 为准，失败不影响发布）。
   - `visibility` 默认 `public`；用户要求先自己看再说就 `private`。
4. **确认**：对话里用 `question` 出一张卡：标题、简介、话题、可见范围、发布时间（立即 / 定时）；选项「可以」「改一下」。用户没点「可以」不许发。**定时任务里没有卡**：模版已授权，按模版的 `publish_mode` 与预算直接发。
5. **发布** `desktop_publish(action="publish", asset_id=…, title=…, intro=…, topics=[…], declaration="ai", visibility=…, hot_word=…, schedule_at=…)`。
   - `status=published` → 告诉用户已发布，给出可见范围；有 `item_id` 就一并给出（内部 job_id 不外露）。
   - `degrade=true`（风控信号：验证码、操作频繁、账号异常……）→ 该账号自动发布已被停用并已通知用户。**这条视频立刻改用** `douyin_publish(action="publish", …)` 出投稿码（标题可放宽到 55 字），并明确告诉用户：创作者中心出现了风控提示，自动发布已暂停，确认账号正常后可让你重新启用。
   - `login_expired=true` → 用户已收到重登通知；本条不发或改投稿包。
   - 其他 `refused` → 按提示处理（缩短标题、换资产、等到 `next_allowed_at`）。工具不重试，你也不要连点。
6. **回看** `desktop_publish(action="status")` 可列最近记录；用户想恢复自动发布且确认账号正常 → `desktop_publish(action="enable_auto")`。

## 注意

- 自动发布是用客户账号在做违反平台规则的事，节奏由工具限制；不要为了「多发几条」拆分视频或建议开多个账号。
- 用户要「先试试不真发」→ `dry_run=true`：只填表、截图、暂存离开，不点发布；记录 `status=draft`。
- 演练降级路径用 `simulate_risk=true`（会真的把该账号停用，演练后要 `enable_auto`）。
- 不对用户暴露 job_id、asset_id、desktop_id；错误翻译成人话。
