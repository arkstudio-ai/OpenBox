---
name: douyin-desktop-publish
description: Publish a finished video to the person's 抖音 account. This is the default publishing route — it posts through the cloud desktop's already logged-in 创作者中心, so the person never authorizes or binds anything; the QR package (douyin-publish) is only the fallback when this route is switched off or the account tripped risk control. Use for 发布/发抖音/投稿/发布到抖音/上传到创作者中心/自动发布 requests, for marketing-autopilot runs, and right after a video is delivered and the person wants it posted.
allowed-tools:
  - desktop_publish
  - desktop_login
  - douyin_publish
  - question
  - share_file
---

# 发布到抖音（云电脑创作者中心）

用户说「发布」，意思就是把成片发到他自己已经登录的抖音号上。默认用客户云电脑里已登录的创作者中心替他发，不需要客户再做任何授权、绑定、扫码动作。规则由 `desktop_publish` 强制执行（每日上限、最小间隔、发布时段、风控熔断），你不要绕开，也不要自己开浏览器去创作者中心操作。任何一步被拒都按工具返回的类别走，不要自己另选一条路。

## 流程

1. **先看能不能发** `desktop_publish(action="precheck")`。
   - `can_auto_publish=true` → 第 2 步。
   - `mode=package`（部署或模版把自动发布关了，或该账号已被风控停用）→ 这是唯一改走 [douyin-publish](../douyin-publish/SKILL.md) 扫码投稿包的情况；用一句话告诉用户原因。
   - `login` 不是 ok → 让用户在云电脑重新登录：`desktop_login(action="open", site="douyin_creator")`，用户说登好了 → `desktop_login(action="probe", site="douyin_creator")` 确认 `bound` 后再发。视频留着，不改扫码，不出授权码。定时任务里用户不在场：这条不发，报告里写明「需在云电脑重登创作者中心」。
   - `budget=blocked` → 不在此刻发。对话里告诉用户 `next_allowed_at`，可以用 `schedule_at` 定时；定时任务里留到下次运行并在报告写明。
2. **拿到成片 asset_id**：刚交付的成片在 `share_file` 返回的元数据里；资源中心的文件用 `share_file(attach=false)` 登记。只发视频。成片多大都行，工具让创作者中心直接读云电脑上的文件，不要为了发布去压缩或转码。
3. **拟文案**（创作者中心比投稿包更严）：
   - 标题 **≤ 30 字**，说清讲什么、给谁看，口语化，不写「AI 生成」之类与内容无关的话。
   - 简介 ≤ 1000 字，正文 1–3 句 + `topics` 3–5 个话题词（不带 #，工具会变成话题标签）。
   - `declaration` 固定 `ai`（AI 生成内容必须声明）；带明确带货/推广的内容仍选 `ai`（创作者中心只能单选）。
   - 跟热点的视频把热点词填进 `hot_word`（能否关联成功以返回的 `hot_word_attached` 为准，失败不影响发布）。
   - `visibility` 默认 `public`；用户要求先自己看再说就 `private`。
4. **确认**：对话里用 `question` 出一张卡：标题、简介、话题、可见范围、发布时间（立即 / 定时）；选项「可以」「改一下」。用户没点「可以」不许发。用户说「发布前让我确认」「先别真发」→ `dry_run=true`：只填表、截图、暂存离开，留成草稿让他在创作者中心确认。**定时任务里没有卡**：模版已授权，按模版的 `publish_mode` 与预算直接发。
5. **发布** `desktop_publish(action="publish", asset_id=…, title=…, intro=…, topics=[…], declaration="ai", visibility=…, hot_word=…, schedule_at=…)`。
   - `status=published` → 告诉用户已发布，给出可见范围；有 `item_id` 就一并给出（内部 job_id 不外露）。
   - `status=draft` → 告诉用户已经传好、信息填好、存成了草稿，去创作者中心点发布即可。
6. **回看** `desktop_publish(action="status")` 可列最近记录；用户想恢复自动发布且确认账号正常 → `desktop_publish(action="enable_auto")`。

## 发布被拒时，按工具给的类别处理，三类互不混用

- **`retryable=true`（上传或页面操作没完成）**：这是我们自己的执行问题，不是账号问题。用同样参数再调一次 `publish`，最多一次；仍失败就如实说「这次没发出去，稍后再试或让我们处理」。不改扫码投稿，不出授权二维码，不自己写脚本去创作者中心传。
- **`login_expired=true`（登录失效）**：用户已收到重登通知；按第 1 步让他在云电脑重登，视频留着，登录后接着发。不改扫码，不出授权码。
- **`degrade=true`（风控信号：验证码、操作频繁、账号异常……）**：该账号自动发布已被停用并已通知用户。**这条视频立刻改用** `douyin_publish(action="publish", …)` 出投稿码（标题可放宽到 55 字），并明确告诉用户：创作者中心出现了风控提示，自动发布已暂停，确认账号正常后可让你重新启用。这是唯一会让用户看到二维码的失败。
- 「发布结果不明」（发布按钮已点过）→ 先 `status` 或让用户看创作者中心的内容管理，确认没发出去再重试，不要直接重发。
- 其他 `refused`（标题过长、资产不对、时段/额度）→ 按提示处理。工具不重试，你也不要连点。

## 话术

- 不对用户说「平台不允许 / 无法绕过 / 必须绑定」。开放平台扫码那条路的限制是它自己的限制，只在确实走了投稿包时才解释扫码规则。
- 自动发布是用客户账号在做违反平台规则的事，节奏由工具限制；不要为了「多发几条」拆分视频或建议开多个账号。
- 演练降级路径用 `simulate_risk=true`（会真的把该账号停用，演练后要 `enable_auto`）。
- 不对用户暴露 job_id、asset_id、desktop_id；错误翻译成人话。
