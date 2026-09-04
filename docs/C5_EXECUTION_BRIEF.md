# C5 · 口播视频技能细化 —— Codex 开工引子

> 2026-09-04。自包含执行单；规则明细与逐条对照见 `docs/C5_VIDEO_SKILL_ALIGNMENT.md`（§2.7 用户体验要求 U1–U8 优先级最高，§2 的 24 条「采纳」次之）。
> 从 `main`（≥ `e926093`）起分支 `codex/c5-video-skill`。
>
> **执行者须知**：只改 `backend/.openbox/skills/video-production/**` 与 `backend/tests/unit/test_video_skill_*.py`。
> 不改 `backend/tool/`、不改前端、不改后端其它目录、不改移动端。任何实拍前先报数再花钱。遇 §8 情形停下来报告。

---

## 1. 目标

**一句话**：把「做一条口播视频」从「agent 自己一路跑到底、花钱前不问、素材不用、结果不提醒」改成「三张确认卡把关、用户素材优先、STT 与时长对不上必提醒、交付走 `share_file`」，规则全部落在技能层，脚本只提示不阻断。

**完成定义**：
1. 流程只有**三个必停点**，都用 `question` 工具出卡，卡前内容完整可见：
   - 成稿卡：讲稿全文 + 时长选项（可以 / 短到约 30 秒 / 长到 60–75 秒 / 需要修改）+ 要不要字幕（默认配）。用户没给时长不单独先问。
   - 拆段卡：每段台词、每段发给模型的 prompt **原文一字不少**、每段秒数、素材清单（哪张图 / 哪段视频用在哪几段）、模型与分辨率、按这稿的总时长（与要求不符时明说）、费用报价（段数、总秒数、`estimate` 结果）。**没点「可以」一次 `submit` 都不许发**。
   - STT 结果卡：每段可播放链接 + 想说的 + 实际念出 + 判定；替换词、或实际时长与计划偏差超过 max(2s, 25%) 的段标红；由用户选重生或接受。
2. **不再自动生成锚图**：优先用户素材，零素材用文字画面基底各段一字不差，只有用户要求才 `image_gen`；素材被模型拒绝（如 720p SD 档丢视频参考）要告诉用户换模型或素材，不得自己绕。
3. **改稿链**：讲稿改了 → 拆段与 prompt 作废、重出拆段卡；拆段改了 → 费用作废；只重做受影响段。`state.py check` 报出漂移与受影响段号。
4. **硬规则进 SKILL.md 顶部**：素材外传红线（禁图床/网盘、禁 ngrok/serveo/`ssh -R`、禁对外监听；401/403 停下不绕路）、交付必须走 `share_file`、不暴露内部 id/路径/工具名、计划工具不是审批。
5. bossip 的工艺知识移植：军规④写语气不写语速 / ⑥场景堆料克制 / ⑦失败先原样重试一次；病理表；few-shot 中文范例；素材 role 词汇（person/scene/outfit/prop）与「视频锚比图片锚稳」；合规自审清单；讲稿结构模板；样片先行；续跑先读 state；每段提交即记录 job。
6. 脚本：新增 `split_script.py`；`state.py` 加 `confirm/check/--accept`；`lint_prompt.py` 加两条；全部退出 0；配单测。
7. 每关一条 15 秒样片实拍验证，三个题材各一条 45–60 秒成片经产品评审。

---

## 2. 现状（2026-09-04，main `e926093`）

| 事实 | 位置 |
|---|---|
| 技能目录：`SKILL.md`（160 行，英文）+ `references/{model-guide,prompt-recipes,quality}.md` + `scripts/{plan_shots,lint_prompt,build_ass,compare_transcript,state}.py,{compose,extract_audio}.sh` | `backend/.openbox/skills/video-production/` |
| SKILL.md 由后端读取并注入；**脚本在桌面上**，路径 `/opt/openbox/skills/video-production/scripts/`，改了脚本要用 `backend/scripts/wuying_deploy_action_server.py` 窄部署到桌面（金镜像里的是构建时的版本） | `SKILL.md:12-16`、`scripts/wuying_deploy_action_server.py:32-36` |
| 现流程（用户实测）：读模型表 → 出讲稿要一句「可以」→ 自己拆段写 prompt 跑 lint（结果不一定贴出）→ 无锚图就自动 `image_gen` → **无确认直接提交全部段** → 回合内轮询最多 13 次，超了切断回合等用户说「继续」→ 每段抽音频/上传/转写/比对刷屏 → 合成 → 给一个链接 | `SKILL.md` Workflow、`tool/video_production.py:83,2467-2483` |
| `question` 工具：build agent 已放行（默认 deny，agent 覆盖 allow）；调用后 `asyncio.Event`/Redis 等待用户作答，即「挂起等答」 | `agent/agent.py:185`、`agent/loop.py:2401`、`question/question.py:103-150` |
| 钱与能力在工具层：`submit` 必带 `idempotency_key`；在途去重；日上限 50（背压不是审批）；`action="models"` 读注册表；`action="estimate"` 免费校验；`-1` 智能时长不上线；720p SD 档拒绝视频参考 | `tool/video_production.py:111,158,1673-1689,1714-1755,1774-1832`、`tool/video_providers.py:498-525,610-611` |
| `lint_prompt.py` 有意 `return 0`；`state.py` 明写 "not a state machine, never blocks"——**保持** | 两脚本文件头 |
| 字幕与合成：ffmpeg + libass 烧 ASS（`build_ass.py` 从转写生成，字体 Noto Sans CJK SC；`compose.sh` 拼接烧录），HyperFrames 已退役，**不要引回** | `scripts/build_ass.py`、`scripts/compose.sh`、`VIDEO_ATOMIZATION_PLAN.md` §6 |
| openbox 的 `video_project` 五道哈希审批已**有意退役**；用户拍板花费护栏走积分、脚本只助不阻——所以**不复刻** bossip 的 gate.py | `video/productions.py:1-10`、`VIDEO_ATOMIZATION_PLAN.md` §1.6 |
| bossip 参照：`~/arkstudio/bossip/apps/center/skills/bossip-video-production/`（SKILL.md、references/prompt-recipes.md 与 anchor-and-prompt-syntax.md、scripts/split_script.py、scripts/gate.py 的 lint 规则）。只搬知识与 lint 规则，不搬相位机、production.md、REST 上传、HyperFrames | 对照表 `C5_VIDEO_SKILL_ALIGNMENT.md` §2 |
| 既有单测：`tests/unit/test_video_skill_scripts.py`（脚本）、`test_video_production.py`（工具，其中 2 条 + `test_video_open_generation.py` 1 条依赖本机 `openbox.json`，一直红，与本项无关） | `backend/tests/unit/` |
| 费用：720p 约 ¥0.5/秒，15 秒样片约 ¥8，45–60 秒成片约 ¥25–30；本项预算约 ¥150 | `BILLING_PLAN.md` §5.1 |

---

## 3. 前置与资料
| # | 项 | 状态 |
|---|---|---|
| 1 | 出片环境：gw2（`https://ai.bossipai.com.cn`）或本地全栈，视频/LLM 走自有 new-api | 用户确认 |
| 2 | 三个题材（装修讲解 / 产品评测 / 知识科普，或市场给的）各一段主题或讲稿 | 用户提供 |
| 3 | 一张人物锚图与一段人物视频（用于 AC-3 的素材优先与 720p 拒绝场景） | 用户提供 |
| 4 | 产品评审人 | 用户指定 |
| 5 | 桌面窄部署的执行方式（在 gw2 容器内或本机 aliyun CLI） | 用户确认 |

---

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `SKILL.md` | 顶部加「硬规则」段（§1.4）；Workflow 重写为「读模型表 → 读人设 → 出稿 → **成稿卡** → 拆段/写 prompt/lint → **拆段卡** → 提交（每段返回即 `state.py shot --job`）→ 等待（`polling_paused` 时结束回合并说明）→ 转写比对 → **STT 结果卡** → 合成 → `share_file` 交付」；加改稿链、样片先行、续跑先读 state、失败原样重试一次；删除「无锚图就 image_gen」；Pitfalls 扩到 13 条；结尾「投稿另有技能」占位。语气保持 craft-not-pipeline：卡是问法，不是闸。中文话术（卡文案、prompt 范例）用中文 |
| `references/prompt-recipes.md` | 军规④⑥⑦、素材 role 词汇与多人/换装写法、few-shot 中文范例（标注来源）、合规自审清单、讲稿结构模板 |
| `references/quality.md` | 病理表（换人 / 说错话 / 打错字 / 背景自变 / 发型变：成因与预防）；STT 豁免用法；时长偏差判定 |
| `references/model-guide.md` | 「视频锚比图片锚稳」；720p SD 档丢视频参考要换 1080p；宽高比 0.4–2.5 注意事项 |
| `scripts/split_script.py`（新） | 句/语义边界贪心拆到 ≤N 字（默认 40，可配），输出可直接作 `plan_shots.py --line` 参数；错误码 JSON；单测 |
| `scripts/state.py` | `confirm --kind script\|shots --note`（记 hash；shots 隐含费用确认）、`shot --accept "理由"`、`check [--final final.mp4]`：打印缺项——script/shots hash 漂移与受影响段号、段无 job、段无音轨、段实际时长与计划偏差超 max(2s,25%)、成片无音轨、成片时长≠Σ段；**退出 0**；单测 |
| `scripts/lint_prompt.py` | 放行「镜头跟随」；`@` 后台词与台词字段不一致 → warn；单测 |
| 桌面同步 | 改完用 `wuying_deploy_action_server.py` 部署到一台桌面，RunCommand 核对 `sha256sum /opt/openbox/skills/video-production/scripts/*` 与仓库一致 |

**不做**：gate.py / production.md / exit 非零 / REST 上传 / HyperFrames / 任何 `backend/tool/`、前端、移动端改动 / 轮询后台化（另立任务）/ 成片预览与素材库（队友在做）。

---

## 5. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 三张卡实跑 | 一次真实会话（不给时长）：成稿卡带时长与字幕选项；拆段卡带每段 prompt 原文、素材清单、总时长、费用；STT 结果卡带链接与判定。三张卡前都有完整内容；**点「可以」之前会话里没有任何 `submit`** |
| AC-2 | 改稿链 | 成稿确认后改一句台词 → agent 重新拆段并重出拆段卡；只重做受影响段；`state.py check` 报出受影响段号 |
| AC-3 | 素材优先 | (a) 传一段人物视频 + 选 720p SD 模型 → 拆段卡说明该档丢视频参考并请换模型/素材，**不调用 `image_gen`**；(b) 不传素材也不要求 → 不调用 `image_gen`，用文字画面基底且各段一字不差 |
| AC-4 | STT 提醒 | 人为制造一段替换词、一段时长偏差 → 结果卡两段标红并写明原因 |
| AC-5 | 红线 | SKILL.md 含三类禁令原文与「401/403 停下」；单测断言存在；lint 对 prompt 内 URI 仍 FAIL 提示 |
| AC-6 | 脚本 | `split_script.py` 三段样例输出符合 ≤40 字与边界规则；`state.py check` 六类缺项各一条单测；`lint_prompt.py` 两条新规则有单测；三脚本退出码均为 0 |
| AC-7 | 交付 | 成片经 `share_file` 交付，聊天里是可播放卡片；未调用前不说「已交付」 |
| AC-8 | 每关实拍 | 出稿 → 拆段 → 费用 → 生成 → STT → 合成，每关一条 15 秒样片的证据（链接或截图），每次实拍前报数并得到确认 |
| AC-9 | 三条成片 | 三个题材各一条 45–60 秒，字幕来自转写，产品评审通过，意见回填 §7 |
| AC-10 | 对照表清零 | `C5_VIDEO_SKILL_ALIGNMENT.md` §2 每条回填「已落地（文件行）/ 有意不同（理由）/ 随访」 |
| AC-11 | 不越界 | `git diff --stat main` 只含 `backend/.openbox/skills/video-production/**`、`backend/tests/unit/test_video_skill_*.py`、两份 C5 文档 |
| AC-12 | 桌面同步 | RunCommand 输出的脚本 sha256 与仓库一致 |
| AC-13 | 测试 | `uv run pytest tests/unit -q` 除既有 3 个视频配置用例外全绿 |

---

## 6. 测试方式
```bash
cd backend && uv run pytest tests/unit -q -k "video_skill or plan_shots or lint_prompt or build_ass or split_script or state"
cd backend && uv run pytest tests/unit -q          # 全量
```
真实会话：build agent（`question` 已放行），gw2 或本地全栈；AC-1～4、7～9 在真实会话里做，费用按 §2 末行估，每次先报数。

## 7. 交付证据与执行记录
1. `git log --oneline main..<branch>`、`git diff --stat main`、pytest 输出。
2. AC-1 三张卡的会话导出或截图（含卡前内容）；AC-2 改稿后的重出卡与 `check` 输出；AC-3 两种场景的会话片段；AC-4 结果卡截图。
3. AC-8 每关样片链接 + 报数记录；AC-9 三条成片链接 + 评审意见。
4. AC-12 sha256 对比输出。
5. 对照表回填（AC-10）。
6. 执行记录（执行者填）：分支 / 提交；实拍费用合计；偏离；评审意见。

## 8. 停下来报告
- 需要改 `backend/tool/`、前端或后端其它目录才能过验收。
- `question` 工具在目标环境不可用。
- 实拍前报数未得到确认。
- 桌面窄部署失败。
