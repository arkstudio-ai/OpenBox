# C5 · 口播视频技能对照 bossip 细化 —— 调研结论 + 独立执行单

> 2026-09-04。对照对象：bossip `apps/center/skills/bossip-video-production/`（v0.9.2，SKILL.md 300 行 + gate.py 650 行 + 7 份 references + CHANGELOG）
> 与 openbox `backend/.openbox/skills/video-production/`（SKILL.md 160 行 + 3 份 references + 7 个脚本）。
> 两边全部实读；openbox 工具层（`tool/video_production.py`、`tool/video_providers.py`）里的规则位置也一并查了。
> 本文前半是调研结论，后半是给 Codex 的执行单。**C5 只改技能目录，不改后端、不改前端、不改工具层**；工具层发现的问题单独列在 §4 作为随访。

---

## 0. 一句话结论

bossip 技能的价值分两半：**一半是工艺知识**（prompt 军规、病理表、素材语法、必停点的问法与顺序、交付口径），这半 openbox 缺得多，应该移植；
**另一半是硬闸机制**（gate.py 八相位状态机、exit 非零、contentHash 作废确认），这半 openbox 是**有意退役**的（`docs/VIDEO_ATOMIZATION_PLAN.md` §1.6：花费护栏走积分/额度不做审批卡，脚本「只助不阻」），不能搬。
bossip 之所以把一切做成硬闸，是因为他们的模型（qwen）会冲过对话式确认（CHANGELOG 0.9.1、2026-08-08 两次实锤）；openbox 面向更强的模型，钱的护栏放在工具层（幂等键、在途去重、日上限、B4 之后的积分）。
所以 C5 的做法是：**把 bossip 的纪律翻成 openbox 的「知识 + 建议式脚本 + question 工具必停点」**，而不是复刻 gate.py。

---

## 1. 设计前提（编码前必须认同）

| 前提 | 依据 |
|---|---|
| openbox 已退役 `video_project` 工具与五道哈希审批（script/segments/spend/quality/render），表只读保留渲染历史 | `backend/video/productions.py:1-10`、`VIDEO_ATOMIZATION_PLAN.md` §8 |
| 用户拍板：花费护栏走积分/额度，不做审批卡；脚本做松散状态管理，只助不阻 | `VIDEO_ATOMIZATION_PLAN.md` §1.6 |
| 工具层已持有：幂等键必填、在途去重、日上限（背压不是审批）、`estimate` 免费校验、`models` 读注册表、`-1` 不上线、每模型时长/分辨率校验 | `tool/video_production.py:111,158,1673-1689,1774-1832,1714-1755`、`video_providers.py:498-523` |
| openbox 的 build agent **允许 `question` 工具**（默认规则 deny，build/plan 覆盖为 allow）；`question` 会挂起等用户作答，等价于 bossip 的 `request_user_input` | `agent/agent.py:185,199`、`agent/loop.py:2401`、`tool/question_tool.py` |
| `lint_prompt.py` 有意返回 0（"non-zero would turn look-at-this into stop"）；`state.py` 明写 "not a state machine, never blocks" | 两脚本文件头 |
| bossip 的 `durationSec: -1` 与其计费漏账绑定（提交时按 1 秒计）；openbox 实测 `-1` 在 SD 档恒返 5.06s、Seedance 每段 15s≈55 字 | 审查 §4.1、`references/model-guide.md:76-83` |

---

## 2. 对照表（bossip 规则 → openbox 现状 → 处置）

处置三值：**采纳**（进技能知识层/建议式脚本）、**有意不同**（保留 openbox 做法，写明理由）、**随访**（属工具层，不在 C5）。

### 2.1 流程与必停点

| # | bossip 规则 | openbox 现状 | 处置 |
|---|---|---|---|
| 1 | 八相位线性推进，`gate.py pass-*` exit 非零 = 未完成，禁止宣称 | 无状态机；`state.py` 只记笔记 | **有意不同**：硬闸退役是拍板过的架构决定；改为 `state.py check` 给出「缺项清单」但退出 0 |
| 2 | 五个必停点（讲稿 / 分段 / **费用** / STT 结果 / 成片形式），全部用 `request_user_input` 卡，任何两个不合并进同一轮 | 只有讲稿一处「get a plain yes」；**没有费用确认**；无 STT 结果确认；无成片形式确认 | **采纳**：五处都用 `question` 工具出卡（build agent 已放行）；工具不可用（cron/无 question）时退化为文字提问并结束回合。费用卡用 `video_generate(action="estimate")` 的结果明码：段数、每段秒数、模型、分辨率 |
| 3 | 先给东西再问话：同一条消息内容在前、卡在后；讲稿贴出来之前不许出任何卡；可推断项一律取默认先出稿 | 无此约束 | **采纳**（进 SKILL.md「必停点纪律」） |
| 4 | 展示不完整 = 没展示：分段确认必须贴每段**完整 prompt 原文** + 素材清单；STT 确认必须带**可播放链接** | 步骤 8 说 "Show the person the video, the intended line, the actual words" 但没说链接是硬要求 | **采纳** |
| 5 | contentHash 绑定确认：确认后改台词/画面/素材 → 确认作废，重生前须重新确认费用 | 无；可能对着用户改过的稿重生而无人察觉 | **采纳（建议式）**：`state.py confirm --kind script|shots|spend` 记录当时内容 hash；`state.py check` 在 hash 漂移时打印「已确认内容已改动，重新确认」，退出 0 |
| 6 | 样片先行：用户说「先做一段」才只做一段，其余段属新一轮费用 | 无 | **采纳** |
| 7 | 用户回来问进度：先 `gate.py status` + 读制片单，不凭记忆重走 | 有 `state.py` 但未写成规则 | **采纳**：SKILL.md 加「续跑先读 state」 |
| 8 | 计划工具不是审批；标 completed ≠ 用户点头 | 无 | **采纳**（一句话进 SKILL.md） |

### 2.2 讲稿与拆段

| # | bossip | openbox | 处置 |
|---|---|---|---|
| 9 | 讲稿 = 纯台词，结构 钩子→展开→转折→收尾；默认 45–75s | 步骤 2 有 4 字/秒换算，无结构模板 | **采纳**结构模板与默认时长 |
| 10 | 语速 3.2 字/秒（智能时长下） | 4.0 字/秒，分档 3.4/4.0/4.6，实测（2026-09-01） | **有意不同**：openbox 用显式时长，数字来自实测 |
| 11 | `split_script.py`：按句/语义边界贪心拆到 ≤40 字 | 无拆段脚本，`plan_shots.py` 吃已拆好的行 | **采纳**：移植为 `split_script.py`，输出直接喂 `plan_shots.py --line` |
| 12 | 单段 ≤40 字（gate 硬上限 48，绑 sd2 15s） | 每模型上限从 `models` 读；Seedance 15s≈55 字 | **有意不同**：上限按模型算，不写死 48；40 字作为「建议」保留 |
| 13 | `durationSec: -1` 一律 | 显式时长，`plan_shots.py` 算 | **有意不同**（理由见 §1 末行） |

### 2.3 素材与 prompt

| # | bossip | openbox | 处置 |
|---|---|---|---|
| 14 | 素材四类 role：person/scene/outfit/prop；视频锚比图片锚稳 | 只有「一张锚图」 | **采纳**词汇与「视频锚更稳」；多人/换装写法进 `prompt-recipes.md` |
| 15 | 素材编号按类型分别计数；prompt 正文禁写 URI | 已有（`prompt-recipes.md:47-49`，lint 有 URI 检查） | 已有 |
| 16 | 宽高比 0.4–2.5 否则被拒；中心裁剪修复；禁 sips | 技能与工具层都没有 | **随访**（工具层加校验，§4）；技能层先写进 pitfalls |
| 17 | 🚫 素材外传红线：禁图床/网盘、禁 ngrok/serveo/`ssh -R` 隧道、禁对外监听；401/403 停下不绕路 | 无。openbox agent 有 bash + 网络，同样会绕 | **采纳**（硬规则，进 SKILL.md 顶部） |
| 18 | 零素材：文字画面基底各段一字不差 | 有（anchor sentence byte for byte） | 已有 |
| 19 | 七条军规：①@台词 ②镜头纪律 ③画面基底 ④**写语气不写语速** ⑤无字幕 ⑥**场景堆料克制** ⑦**失败先原样重试一次** | 有 ①②③⑤；缺 ④⑥⑦ | **采纳** ④⑥⑦ |
| 20 | 病理表：换人 / 说错话 / 打错字 / 背景自变 / 发型变，各配成因与预防 | 只有零散 "What actually goes wrong" | **采纳**（表格移植进 `quality.md` 或新 `pathology.md`） |
| 21 | few-shot 范例：HOOK/BODY/CLOSING + 换装 + 走动变体 | 只有骨架与角色提示 | **采纳**（中文范例原样移植，标注来源 4,968 条记录） |
| 22 | lint：允许「镜头跟随」；`@` 后台词与台词字段不一致 → warn | `lint_prompt.py` 无走动镜头放行、无一致性 warn | **采纳**两条 lint（仍返回 0） |
| 23 | 合规自审：无医疗/解剖词、无品牌/名人 IP、personRef 非公众人物 | 无 | **采纳**（一条清单） |
| 24 | 真人素材被拒（"may contain real person"） | openbox 有 `video_identity` 实人授权流程 | **有意不同**：指向 openbox 自己的流程 |

### 2.4 生成与自检

| # | bossip | openbox | 处置 |
|---|---|---|---|
| 25 | 每提交一段立刻写回 id，中断不丢已花钱的段 | 步骤 7 说 poll together，`state.py` 可记 job | **采纳**：明写「每个 submit 返回即 `state.py shot --job`」 |
| 26 | FAILED 原样重试 1 次再判失败 | 无 | **采纳**（军规⑦） |
| 27 | 30 分钟 PENDING 自动 FAILED | openbox 有 `polling_paused` + 恢复补扫 | **有意不同** |
| 28 | STT 相似度 0.90 + notes 兜底 | 已移植（替换检测收紧到 ≥1 字） | 已有 |
| 29 | `STT豁免` 字段：用户试看接受后记理由与日期 | 无 | **采纳**：`state.py shot --accept "理由"` |
| 30 | pass-generate 校验：每段有音轨 | 无脚本校验 | **采纳（建议式）**：`state.py check` 用 ffprobe 查每段音轨 |

### 2.5 合成与交付

| # | bossip | openbox | 处置 |
|---|---|---|---|
| 31 | HyperFrames 0.7.94 + 系统 Chrome，版本钉死 | ffmpeg concat + ASS 字幕 | **有意不同**（确定性、无 Chrome 下载） |
| 32 | 字幕用 STT 实际念出的词 | 已有 | 已有 |
| 33 | 成片时长 ≈ Σ 段实测（容差 max(2s,5%)）且有音轨 | `compose.sh` 末尾只打印 ffprobe | **采纳（建议式）**：`state.py check --final final.mp4` 做这两条断言，退出 0 |
| 34 | 交付 = https URL 可访问；`pass-deliver` 不过只能说「本地渲染完成，尚未交付」 | 交付走 `share_file`，SKILL 说 "verify it has audio and the length you expect" | **采纳口径**：交付 = `share_file` 返回的可下载链接；未拿到链接不许说「已交付」 |
| 35 | 成片 REST 上传、`BOSSIP_INTERNAL_TOKEN`、label ASCII | 不适用 | **有意不同** |
| 36 | 投稿交接给 `bossip-browser-cdp`（GUI 才可） | 无投稿；A5 授权中心后再说 | **有意不同**（留接口：SKILL 末尾一句「投稿另有技能」占位） |

### 2.6 会话约定与错误

| # | bossip | openbox | 处置 |
|---|---|---|---|
| 37 | 全中文、报错翻人话、不暴露内部 id/路径/工具名 | 无 | **采纳** |
| 38 | `propose_memory` 宁可漏不可烦，每轮最多一次 | openbox 有记忆系统 | **采纳**精神（一句话，指向 openbox 的记忆工具） |
| 39 | `io-schema.json` 错误码 9 个 | 工具 pydantic schema | **有意不同** |
| 40 | Pitfalls 表 20 条 | "What actually goes wrong" 5 条 | **采纳**其中模型无关的 8 条（人物漂移、背景自变、审核拒、找不到项目目录、成片没声音、字幕溢出、`-32602` 类参数错=不是工具坏、`估算≠实测`） |

统计：采纳 24 条，有意不同 12 条，已有 4 条，随访 1 条。

---

## 3. 执行单（给 Codex）

### 3.1 目标
把 §2 的 24 条「采纳」落进 `backend/.openbox/skills/video-production/`，每关实拍一条 15 秒口播验证；不改后端、前端、工具层；`state.py` 与 `lint_prompt.py` 保持退出 0。

### 3.2 现状
- 技能目录结构见 §0；脚本在沙箱路径 `/opt/openbox/skills/video-production/scripts/`（SKILL.md 开头有说明），随 `wuying_bootstrap.py` 与金镜像分发——**改了脚本要刷到桌面**（后端首次用工具时会同步，或 RunCommand 手动刷）。
- SKILL.md 英文、prompt 与用户话术中文混用。
- 单测：`backend/tests/unit/` 里对技能脚本有测试（grep `plan_shots`、`lint_prompt`、`build_ass`），新增脚本要配测试。
- 真实出片费用：720p 每秒约 ¥0.5，一条 15 秒样片约 ¥8；每关一条、三个题材各一条成片（45–60s）≈ ¥30 × 3，总预算约 ¥150 内，**每次实拍前报数**。

### 3.3 必要资料
| # | 资料 |
|---|---|
| 1 | 可出片的环境：gw2 账号或本地全栈 + 网关 token（LLM、视频都走自有 new-api） |
| 2 | 三个题材的讲稿或主题（装修讲解 / 产品评测 / 知识科普，或市场给的） |
| 3 | 一张人物锚图（或允许 `image_gen` 生成） |
| 4 | 产品评审人（三条成片的验收人） |

### 3.4 改动清单
| 文件 | 改动 |
|---|---|
| `SKILL.md` | 新增「硬规则」段（素材外传红线 #17、必停点纪律 #2/#3/#4、交付口径 #34、不暴露内部信息 #37）；Workflow 改为带五个 `question` 卡的流程（#2），加样片先行 #6、续跑先读 state #7、计划≠审批 #8、每段提交即记录 #25、失败原样重试一次 #26；Pitfalls 扩到 13 条 #40；结尾投稿占位 #36。保持「craft not pipeline」的语气：卡是问法，不是闸 |
| `references/prompt-recipes.md` | 补军规 ④⑥⑦ #19、素材 role 词汇与多人/换装写法 #14、few-shot 中文范例 #21、合规自审 #23、讲稿结构模板 #9 |
| `references/quality.md`（或新 `pathology.md`） | 病理表 #20；STT 豁免 #29 的用法 |
| `references/model-guide.md` | 「视频锚比图片锚稳」#14；宽高比 0.4–2.5 先写进注意事项 #16 |
| `scripts/split_script.py`（新） | 移植 bossip 版：句/语义边界贪心 ≤40 字（可配），输出可直接拼成 `plan_shots.py --line` 参数；配单测 |
| `scripts/state.py` | 新增 `confirm --kind script\|shots\|spend --note`（记 hash）、`shot --accept`、`check [--final]`（缺项清单：确认 hash 漂移、段无 job、段无音轨、成片时长≠Σ段、成片无音轨；**全部只打印，退出 0**）；配单测 |
| `scripts/lint_prompt.py` | 加「镜头跟随」放行与 `@` 台词一致性 warn #22；配单测 |
| 桌面同步 | 改完在一台桌面上 RunCommand 核对 `/opt/openbox/skills/video-production/scripts/` 与仓库一致 |

**不做**：gate.py、production.md、exit 非零、REST 上传、HyperFrames、任何 `tool/` 改动。

### 3.5 验收
| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 对照表清零 | §2 每条状态为「已有 / 采纳（已落地，指向文件行）/ 有意不同（理由）/ 随访」，执行者在 §5 回填 |
| AC-2 | 必停点实跑 | 一次真实会话：五张 `question` 卡按顺序出现，内容在卡前；费用卡显示段数/秒数/模型；用户改一句台词后 `state.py check` 报「已确认内容已改动」 |
| AC-3 | 红线 | 单测：SKILL.md 含素材外传红线三类禁令原文；lint 对 prompt 内 URI 仍 FAIL 提示 |
| AC-4 | 脚本 | `split_script.py` 对三段样例输出符合 ≤40 字与边界规则；`state.py check` 五类缺项各有一条单测；`lint_prompt.py` 新增两条有单测；全部退出 0 |
| AC-5 | 每关实拍 | 讲稿→拆段→费用→生成→STT→合成，每关一条 15s 样片证据（截图或链接）；每次实拍前报数 |
| AC-6 | 三条成片 | 三个题材 45–60s 各一条，字幕来自 STT，产品评审通过；评审意见回填 §5 |
| AC-7 | 不越界 | `git diff --stat main` 只含 `backend/.openbox/skills/video-production/**` 与 `backend/tests/unit/test_video_skill_*.py` |
| AC-8 | 桌面同步 | RunCommand 输出显示桌面脚本 sha256 与仓库一致 |

### 3.6 测试方式
```bash
cd backend && uv run pytest tests/unit -q -k "skill or plan_shots or lint_prompt or build_ass or split_script or state"
```
真实会话在 gw2 或本地全栈，用 build agent（`question` 已放行）。

### 3.7 交付证据
1. `git diff --stat main`、pytest 输出。
2. 五张卡的会话截图或导出（AC-2），含改台词后的 `check` 输出。
3. 每关 15s 样片链接（AC-5）与费用报数记录。
4. 三条成片链接 + 评审意见（AC-6）。
5. 桌面脚本 sha256 对比（AC-8）。
6. §5 回填。

---

## 4. 随访（不在 C5，记给工具层）

| 项 | 说明 | 去向 |
|---|---|---|
| 宽高比 0.4–2.5 校验 | bossip 上游（tokenspace/seedance 执行器）会拒；openbox `video_production.py` 输入解析只查 mime/归属/≤8 个，不查比例。加一条免费的 ffprobe 校验 + 提示中心裁剪 | 工具层小改，可并入 B2 之前任一后端会话 |
| 费用确认的机器保障 | C5 只在技能层用 `question` 卡确认；真正的钱闸是 B4 积分。B4 之前的唯一硬护栏是日上限 50 | B4 |
| 技能语言 | SKILL.md 英文、范例中文；若产品要求全中文，另开小任务统一 | 待定 |

---

## 5. 执行记录（执行者填写）
- 分支 / 提交：
- 对照表回填（每条状态 + 文件行）：
- 实拍费用合计：
- 评审意见：
- 偏离：

## 6. 停下来报告
- 任何需要改 `backend/tool/`、前端或后端的情形。
- 实拍前报数未得到确认。
- `question` 工具在目标环境不可用。
