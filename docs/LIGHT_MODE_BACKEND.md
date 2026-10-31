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
                 "steps": [{id,label}], "subtypes": [{id,label,samples:[{id,label,description,needs,media_url}]}],
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
- 新注册用户 `extra.ui_mode="light"`；老用户为空读作 `pro`。App 冷启动要自己取一次。

## 配置

- `backend/light/entries.json`（`LIGHT_ENTRIES_FILE` 可覆盖）：7 个入口（餐饮版）。`requires.desktop=true` 的入口在免费档显示为 `needs_desktop`；`requires.binding="douyin"` 需要授权中心已绑定。`kind=coming_soon` 不可 start。
- 档位名「省钱 / 标准 / 高清」是 `openbox.json` `model_tiers.video[].label`，改配置即可（Web 专业模式同步改名，已拍板接受）。
- 迁移 `pi95b6c7d8e9`：`sessions.light_entry String(64) NULL`。

## 技能层

`video-production/SKILL.md` 加了「Light mode」段：一卡 review、每卡 `step_id`、不复问开场卡选过的样子、不报价。`C5_VIDEO_SKILL_ALIGNMENT.md` §7 记为例外。做菜单 / 做海报暂时指向 `imagegen` 技能；`menu-maker` 专用技能与样片素材是后续工作。

## 未做

- `entries.json` 的 `sample_media` / `media_url` 还是空的，样片画廊要等素材。
- 「省心托管」（结果卡开关 + 三问卡 + 托管列表）是 M2.5，不在本次。
