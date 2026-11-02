# Light 模式（后端，M2）

> 2026-10-31。方案 `LIGHT_MODE_PLAN.md` §4，实施拆解 `LIGHT_MODE_IMPL_PLAN.md` §4.1。本文是 App 对接的契约与运维事实。首条视频见 `FIRST_VIDEO.md`。

## 接口

全部带现有鉴权头与 `X-Workspace-Id`（缺省默认工作空间）。

| 调用 | 作用 |
|---|---|
| `GET /api/light/entries` | 首页入口清单，`availability` 已按本工作空间算好 |
| `POST /api/light/start` | 从一个入口开一个对话专题页：真实会话 + 开场卡 |
| `GET /api/light/recent?limit=8` | 「最近做的」：图与视频产出按会话分组 |
| `GET /api/light/mode` / `PUT /api/light/mode {"ui_mode":"light"\|"pro"}` | 界面模式 |

```
GET /api/light/entries →
{ "version", "ui_mode": "light"|"pro", "plan_id", "desktop": bool,
  "entries": [ { "id", "title", "subtitle", "kind": "primary"|"secondary"|"coming_soon",
                 "skill", "default_tier", "price_hint", "requires": {plan, desktop, binding},
                 "steps": [{id,label}], "subtypes": [{id,label,samples:[{id,label,description,needs,media_url,poster_url}]}],
                 "opening_card": {text, text_with_sample, chips[]}, "sample_media": [],
                 "availability": "ok"|"needs_plan"|"needs_desktop"|"needs_binding"|"coming_soon" } ] }

POST /api/light/start {"entry_id", "project_id"?, "subtype"?, "sample"?} →
{ "session_id", "entry": {…同上…}, "opening_card": {"text","chips"}, "steps": [...],
  "video_model", "video_resolution" }
  400 LIGHT_ENTRY_UNKNOWN / LIGHT_SAMPLE_UNKNOWN；409 LIGHT_ENTRY_UNAVAILABLE（detail.availability 说明缺什么）

GET /api/light/recent →
{ "groups": [ { "session_id", "title", "entry_id", "updated_at",
                "outputs": [ { "asset_id", "mime", "kind", "label", "created_at" } ] } ] }
```

- `start` 之后 App 走现有 `POST /api/agent/session/{id}/prompt_async`。开场卡的 `chips` 由 App 画在输入条上，点一下就是第一句话；转录里开场卡是一条真实助理消息，前面有一条合成的用户气泡「【做一条视频】选了「餐品展示 · 烟火气特写」」。
- 会话有 `light_entry`（新列），agent 的系统提示词据此拿到入口、技能与步骤表；每张 `question` 卡的 `detail.light.step_id` 告诉 App 点亮哪一步（`question.asked` 事件与问题快照里都有）。
- `recent` 只收 `relation.role` 为 `final` / `result` 且 `mime` 为图或视频的 FilePart；中间产物（分镜段、上传素材）不算。
- 样片：`media_url` 是样片本身（视频 `.mp4` 或图片），`poster_url` 是小图（视频的截帧、图片的缩略图）。两者在 `entries.json` 里写 `oss:<key>`（私有桶），接口返回时换成 12 小时有效的签名链接（`light/media.py`）；没有 OSS 时为 `null`，App 画色块。
- 新注册用户 `extra.ui_mode="light"`；老用户为空读作 `pro`。App 冷启动要自己取一次。

## 配置

- `backend/light/entries.json`（`LIGHT_ENTRIES_FILE` 可覆盖）：7 个入口（餐饮版）。`requires.desktop=true` 的入口在免费档显示为 `needs_desktop`；`requires.binding="douyin"` 需要授权中心已绑定。`kind=coming_soon` 不可 start。
- 档位名「省钱 / 标准 / 高清」是 `openbox.json` `model_tiers.video[].label`，改配置即可（Web 专业模式同步改名，已拍板接受）。
- 迁移 `pi95b6c7d8e9`：`sessions.light_entry String(64) NULL`。

## 技能层

| 入口 | 技能 | 要点 |
|---|---|---|
| 做一条视频 | `light-video`（2026-11-02 新） | 不用云桌面：样子 + 0–3 张照片 → 一张 review 卡（画面 / 15 秒 / 店名）→ `video_generate` → 有店名时 `video_compose` 叠字。免费档可用。生成或叠字超出一轮的等待预算时，结束本轮，完成后由补扫唤醒对话贴片（见下） |
| 做菜单 | `menu-maker`（新） | 识别旧菜单照片或打字 → 一张菜品表 review 卡 → 尺寸卡 → `image_gen`（quality high）→ 对着菜品表逐行核对，错一次重画一次 |
| 做海报 | `poster-maker`（新） | 用途 → 一句话 → 三个标题 → `image_gen` 竖版；横版按需再出 |

- 三个技能都不以 `question` 卡收尾：结果卡自带「保存 / 发到抖音（视频）/ 再做一条」，悬而未答的卡会进「需要你处理」。
- 系统提示词：Light 会话带入口与步骤表；`extra.first_video` 里有店名 / 主打时加一句「这家店：店名「…」，主打「…」」，供叠字用。没有云桌面时的系统提示补了一句「不需要桌面的云端工具（video_generate / video_compose / image_gen / question）照常可用」。
- `video-production/SKILL.md` 的「Light mode」段仍在（专业用户的口播片），C5 §7 例外不变；它依赖桌面脚本，Light 的做视频入口已不再指向它。

## 合成完成后唤醒对话（F5，2026-11-02）

`video/compose_recovery.py`：补扫把一条 compose 作业推进到 completed / failed 时，如果它没有贴到任何消息（说明发起它的那一轮已经放弃等待），就用与生成相同的 inbox 续接机制唤醒对话：完成 → 「用 video_compose status 取回成片」（取回即贴片），失败 → 「看原因、先交付原片、用白话说明」。首条视频的叠字（`first_video_text:`）与省心托管（`light_autopilot:`）由各自流程交付，不唤醒。`credit_cap.IN_FLIGHT_JOB_KINDS` 加了 `compose`。

## 样片（2026-11-02）

16 条样片（做视频 9 条 15 秒 H3、菜单 4 张、海报 3 张）在 light-test 上用 `backend/scripts/light_samples.py` 走真实链路生成（账号 `light-full-samples`），拷到 `oss://bossip/light/samples/20261102/`：`<id>.mp4` / `<id>.jpg`（样片）、`<id>.jpg`（视频截帧）、`<id>_thumb.jpg`（图片缩略图）。花费：视频 9 × ¥1.35 + 图片约 ¥4，合计约 ¥16。重做样片：在测试机后端容器里跑该脚本（`--only` 选 id、`--prefix` 换目录），再改 `entries.json` 里的 key。

## 省心托管（M2.5，2026-11-02）

结果卡右下角「以后省心托管这个」→ 三问（什么时候 / 只出片还是直接发 / 每次最多花，默认 每天 10:00 · 只出片 · 10 积分）→ 一条普通 cron 任务，`template.kind="light_autopilot"`（`light/autopilot.py`）。

| 调用 | 作用 |
|---|---|
| `GET /api/light/autopilot` | 本工作空间的托管列表（`items[]`：id、task、mode、enabled、repeat、time、credits_cap_per_run、shop_name、next_run_at、last_status…） |
| `POST /api/light/autopilot {session_id, asset_id?, task?, repeat, time, mode, credits_cap}` | 从一个结果开托管；同一结果重复开返回同一条（`created=false`）。409 `LIGHT_AUTOPILOT_NEEDS_DESKTOP`（带 `availability`）= 免费档选了「直接发」或上下架 / 回评价 / 成绩复盘 |
| `PATCH /api/light/autopilot/{id} {enabled?, repeat?, time?, credits_cap?}` | 暂停 / 继续 / 改时间与上限 |
| `DELETE /api/light/autopilot/{id}` | 取消（软删） |

- **recipe**：开托管时从结果读出「怎么再做一条」：视频取生成作业的提示词（去掉上一次的镜头变化句）、参考图、时长、比例；名字叠字的合成作业再取店名和主打。图片取 `image_gen` 调用的提示词和尺寸。
- **只出片**（免费档唯一可选）：不需要云桌面、不跑 agent。执行器把这类任务交给 `light/autopilot.run`：先按会话档位报价，超出上限或余额不足就在对话里说一句并记 `skipped`；否则在原 Light 对话里落一条「【省心托管】每天 10:00，做一条视频」，用 `light/pipeline.py`（与首条视频共用）生成一条新片（每次换一个镜头变化句，避开跨用户内容复用）并叠店名，结果卡、`cron_completed` 通知都指向这个对话。
- **直接发 / 店务**（付费档）：走普通 cron agent 路径（需要桌面），执行时对临时会话 `set_cap(上限)`，付费工具超限即停。套餐过期时这类运行记 `skipped`（原来是 error，会退避直到自动停用）。
- 实测（light-test，11-02）：手动触发一次 79 秒出片，对话里多一条带店名的成片，账本扣 3.75 + 0.045，通知 +1。

## 未做

- 已经打开的 Light 对话不会实时刷出托管运行的新消息（重新进入或点通知即可看到）。
- 「直接发」与店务模板只做了服务端与 App 选项，测试机没有云桌面，未实测。
