# 技能与工具解耦——执行手册

> 文档状态：Execution Handbook v1（2026-08-30）<br>
> **本文件是执行助手的第一准则。** 你（执行者）没有参与前期讨论，本文即全部上下文；
> 按顺序执行，不要引入本文之外的目标。行号以 commit `c5e53d8` 为基准，动手前先用
> grep 复核（行号会漂移，符号名不会）。<br>
> 决策规则：本文已对所有已知分歧给出定论（§7）。遇到本文未覆盖且影响范围超过单文件
> 的新情况：**停下来向用户报告**，不要自行发挥。<br>
> 姊妹文档：`docs/DIRECT_PATH_CLEANUP_PLAN.md`（上一轮清理的手册，环境事实与坑
> 清单大量复用；本文引用其编号时写作 CLEANUP §x）。

---

## 0. 执行者须知（先读完再动手）

### 0.1 你要做什么

拆除"加载技能 → 解锁隐藏工具"的授予机制，让本仓库回到市场主流的技能规范：
**技能是知识（纯注入），工具是能力（恒定注册），权限只做限制、从不做授予。**
同时按信任分层（§2）区别处理两类技能：内置技能（仓库自带，可信）与用户安装到
无影云沙箱的技能（不可信数据）——后者的 frontmatter 从此不得影响后端任何行为。

改动全在后端。**前端与移动端零改动**（工具表是后端拼的，技能中心 UI 不感知本次
语义变化）。

### 0.2 必读文件（按序）

1. 本文件全文；
2. `docs/DIRECT_PATH_CLEANUP_PLAN.md` §0.3 环境地图与 §8 坑清单（端口 8080、
   docker PG、热重载盲区、公开仓库红线、`docs/LOCAL_CREDENTIALS.md` 凭据位置——
   全部沿用，本文不重抄）；
3. `backend/.openbox/skills/skill-creator/SKILL.md` —— 本仓库的技能制作规范
   （"Keep SKILL.md focused on purpose, essential workflow, non-obvious
   constraints, and routing to resources"），§6 的重写以它为准绳；
4. `backend/.openbox/skills/video-production/SKILL.md`（160 行）—— 主要重写对象。

### 0.3 基线（2026-08-30 实测）

| 事项 | 事实 |
|---|---|
| 后端测试 | `cd backend && uv run pytest -q` → **917 passed** |
| 前端 | `npm run check` 干净；lint 恰有 2 个既有错误（`content-view.ts`，不许新增）；`npm run test` 174 例 |
| 移动端 | `dart analyze` 零问题（本次不应产生任何 Dart 改动） |
| 七个门控工具 schema 总量 | Pydantic 原始中间态 **16,056 字符**；经实际 provider 序列化（内联 ref、移除 title、简化 nullable）为 **11,632 字符**。§6.3 的预算以真正发送的后者为准。 |
| 主对话 agent | `build`（`agent/agent.py:132`），其 `tools` 白名单**不含**任何门控工具——见 §5.1 关键事实 |

### 0.4 铁律

1. **权限系统一行不动**：`strip_denied`、config permission rules、服务端付费门
   （审批哈希/花费上限/幂等键）与本次无关且是安全底座。暴露 ≠ 授权。
2. 不碰 `merge_sandbox_tools`（`agent/tool_resolution.py:47`）——那是沙箱 MCP
   工具的合并通道，与技能授予链无关，别把两件事搅在一起。
3. 前端/移动端/locale 零改动；若你发现自己在改它们，先停下核对本文。
4. 每个 PR 的 DoD（§8）全绿才允许提交；浏览器验收不可跳过。

---

## 1. 背景：问题与市场实证（执行时的判断依据）

### 1.1 现状机制（授予链全貌）

```
SKILL.md frontmatter `allowed-tools`
  → skill_tool 加载时读出（本地 :98/:154，容器 :113-118）
  → ToolResult.metadata["activated_tools"]（:192 起还会打进正文）
  → processor 采集（agent/processor.py:515-527）
  → loop 的 per-run 集合（agent/loop.py:355 每轮清空）
  → resolve_step_tools → activate_skill_tools 解锁
     （agent/tool_resolution.py:93-129；:124 处默认过滤掉一切 skill_only 工具）
```

七个工具注册时自我标记 `skill_only=True`（字段定义 `tool/tool.py:68`）：
`video_generate/transcribe/render`（video_production.py:2753/2764/2775）、
`video_project`（video_workflow.py:1965）、
`image_gen`（:888）、`creator_context`（:243）、`skill_manage`（:225）。

### 1.2 为什么错（三点，均有源码级市场对照）

本机 workspace 内三家实现已逐一读源验证（opencode `packages/core/src/tool/skill.ts`、
codex `codex-rs/core/src/skills.rs` 与 `skills/src/interface.rs`、Claude Code 的
Agent Skills 规范）：

| | opencode | codex | Claude Code | 本仓库 |
|---|---|---|---|---|
| 加载技能的副作用 | 零（权限校验后纯返回内容） | 零（注入指令；隐式检测仅埋点） | 注入指令 | 注入 + 解锁 8 工具 |
| 工具从哪来 | 注册表恒定 | harness 恒定 | harness/MCP 恒定 | `skill_only` 默认隐藏 |
| `allowed-tools` 语义 | 无此字段 | 无此字段 | **限制/预授权** | **授予** |

1. **授予 vs 限制反转**：字段名抄自 Claude Code，语义抄反。可编辑 markdown
   （项目内的，甚至**无影云容器里的**——skill_tool.py:113-118 会读容器技能的
   `allowed_tools`）参与了工具暴露决策。防线只剩"只能解锁 skill_only 注册的工具"
   一条。
2. **能力不可见**：不加载技能，模型的工具表里不存在视频/图像能力；发现完全依赖
   技能列表描述。
3. **每轮重激活税**：激活集合 per-run 清空（loop.py:355），多轮制作每条新消息都要
   重新加载一遍 160 行技能文档才能看见工具，否则 unknown tool。三家均无此概念。

注意：160 行的详细工作流文档**本身不违规**（渐进披露允许详尽 workflow 与
references）；违规的只是门控机制。§6 的重写是瘦身而非推翻。

---

## 2. 信任分层：内置技能 vs 无影云用户安装技能（本手册的核心区分）

技能来源在 `skill/skill.py` 与 `tool/skill_tool.py` 里已有三级：`project`
（仓库 `backend/.openbox/skills/`，随代码评审走）、`global`（宿主机全局目录）、
**容器**（用户经技能中心安装到无影云沙箱，`ctx.sandbox.get_skill()` 拉取）。

| | 内置（project/global，宿主侧） | 用户安装（无影云容器侧） |
|---|---|---|
| 信任级别 | 第一方内容，随 git 评审 | **不可信数据**（用户可写、沙箱内可被任何执行改写） |
| frontmatter 对后端行为的影响 | **零**（改造后）。`allowed-tools` 仅作文档/展示 | **零，且必须显式忽略**：读取端删掉 :113-118 的解析，若容器技能声明了 `allowed_tools`/`tools` 字段，debug log 记一条后丢弃 |
| 内容注入对话 | 正常注入（`<skill_content>`） | 照旧注入，但它是不可信文本——本次只关"授予"这个洞；内容级提示注入加固记入 §10 Backlog，不在本次范围 |
| 可用的工具 | 与所有人相同：agent 白名单 + 权限规则决定 | 相同。它们想要的"专属能力"走沙箱 MCP 通道（`merge_sandbox_tools`），那条通道有自己的权限过滤，不动 |
| 校验时机 | `skill_manage` 安装/创建校验照旧 | 同左；可在校验层对声明了 `allowed-tools` 的上传技能提示"该字段无运行时效果" |

**一条不变式，写进代码注释与测试**：*任何来源的技能文件，其任何字段都不得改变
agent 的可调用工具集合。* 内置与用户安装的差别只在"是否额外记日志"与"内容信任
级别"，不在机制——机制上一视同仁为零效果，这样才不会留下第二次反转语义的缝。

---

## 3. 目标架构

**技能教学，工具设防，平台管暴露。**

- 八个工具**恒定注册**并进入 `build` agent 白名单（§5.1）；子 agent（task/batch
  派生的 explore/general）**不给**媒体工具——现行 SKILL.md 本就禁止把媒体工具包进
  Batch/parallel（"the wrapper does not inherit ... sequential safety
  guarantees"），改造后这条约束由白名单结构性保证，比提示词更硬。
- 加载技能变成纯读：注入 `<skill_content>`，无任何 metadata 副作用。
- token 成本用 §6 的 schema 瘦身对冲（16,056 → ≤10,000 字符）。
- 付费安全不变：它从来就在服务端（审批/幂等/花费上限），技能在场只是教学。

---

## 4. 不动区（防误删清单）

| 区域 | 原因 |
|---|---|
| `agent/tool_resolution.py:47 merge_sandbox_tools` 及 MCP 工具链 | 沙箱能力的正规通道，与授予链无关 |
| `strip_denied`、`_get_permission_rules`、`AgentDef.permission` | 限制语义的权限系统，是底座 |
| `tool/video_workflow.py` / `video_production.py` / `video_providers.py` 的**逻辑** | 只动注册处的 `skill_only=True` 一行，业务与服务端门一行不动 |
| `skill/skill.py` 的技能发现/列表/`slash` | 发现面照旧；只改 `allowed_tools` 的用途注释 |
| `tool/skill_tool.py` 的容器回退与错误区分逻辑（container_error 分支） | 上轮修过的基础设施判别，别顺手重构 |
| 前端、移动端、locale、`frontend/`（v1） | 零改动区 |
| `docs/LOCAL_CREDENTIALS.md`、`.env`、`backend/openbox.json` | 凭据与本地配置，公开仓库红线见 CLEANUP §0.3 |

---

## 5. PR#1 拆除授予链（代码）

### 5.1 关键事实（不知道这条你会把工具改没）

`activate_skill_tools` 是把工具**绕过 agent 白名单**直接塞进工具表的
（`resolve_step_tools` 先按 `agent_def.tools` 取正门工具，再叠加激活集）。
七个门控工具**不在任何 AgentDef.tools 里**。因此拆授予链的同一提交必须把它们
写进 `build` agent 白名单（`agent/agent.py:136-141`），追加：
`"image_gen", "video_project", "video_generate",
"video_transcribe", "video_render", "creator_context", "skill_manage"`。
`plan`/`explore`/`general` 等其余 agent **不加**（§3 的结构性约束）。

### 5.2 手术点（行号基准 `c5e53d8`）

1. **`tool/tool.py`**：删除 `skill_only` 字段（`:68`）、`create_tool` 参数（`:84`）
   与透传（`:125`）。
2. **八处注册**（§1.1 清单）：各删 `skill_only=True,` 一行。
3. **`agent/tool_resolution.py`**：删除 `activate_skill_tools` 整个函数
   （`:93-109`）；`resolve_step_tools`（`:111-129`）删去 `activated_tools` 参数、
   `:120-124` 的 skill_only 过滤 dict 推导（回归 `get_tools_for_agent` 直取）与
   `:126` 的激活调用。
4. **`agent/processor.py`**：删 `activated_tools` 字段（`:175`）、局部集合（`:269`）
   与 `tool_name == "skill"` 的 metadata 采集块（`:519-527`）；`:638` 的传参一并去。
5. **`agent/loop.py`**：删 `active_skill_tools`（`:355`、`:480` 传参、`:700` 汇集）。
6. **`tool/skill_tool.py`**：
   - 删三处 `activated_tools` 赋值（`:98`、`:154`，及容器分支 `:113-118` 的解析——
     容器分支按 §2 改为：检测到相关键时 `log.debug` 后忽略）；
   - 删输出里的 "Activated tools for this agent run: ..."（`:192` 起）与
     metadata 里的 `activated_tools`；
   - 函数签名/返回中一切 activation 痕迹清零。加载技能 = 纯读。
7. **`skill/skill.py:24`**：`allowed_tools` 字段**保留**（技能中心列表展示还在用它
   的解析），但把字段注释改为："展示与文档用途；对运行时工具集合零效果（2026-08-30
   解耦，见 docs/SKILL_TOOL_DECOUPLING_PLAN.md）"。`normalize_skill_tools` 保留。
8. **`agent/agent.py:136-141`**：build 白名单追加八工具（§5.1）。

### 5.3 测试（六个文件受影响，处置各不同）

| 文件 | 处置 |
|---|---|
| `tests/unit/test_skill_tool_activation.py` | **重写为反向锚点**：加载任何技能（含伪造 `allowed-tools: [bash]` 的临时技能、含声明了 `allowed_tools` 的模拟容器技能）后，metadata 无 `activated_tools`、输出无 "Activated tools" 字样；这是 §2 不变式的守卫，docstring 写明"任何来源技能的任何字段不得改变工具集合" |
| `tests/unit/test_processor_outcomes.py` | 删其中 activation 采集相关断言/夹具，其余保留 |
| `tests/unit/test_skill_execute.py` | 同上，去 activation 期望 |
| `test_video_production.py` / `test_image_gen.py` / `test_creator_context_tool.py` | 多半只是构造 ToolInfo 时带了 `skill_only`——随字段删除同步清理，业务断言不动 |

新增一个白名单锚点：`build` agent 的 resolve 结果**包含** `video_project` 等八工具、
`explore`/`general` 的结果**不包含**（结构性约束的回归锚）。

### 5.4 PR#1 Definition of Done

```bash
cd backend
grep -rn "skill_only" --include='*.py' . | grep -v .venv        # 零命中
grep -rn "activated_tools\|activate_skill_tools" --include='*.py' . | grep -v .venv  # 零命中
uv run pytest -q                                                # 全绿，数目记入 commit
```

变异检查两处：①把 `build` 白名单里的 `video_project` 临时移除 → 白名单锚点测试
变红；②在容器技能分支临时恢复 `allowed_tools` 解析并回填 metadata → 反向锚点
变红。各验完立即还原。

---

## 6. PR#2 技能文档重写与 schema 瘦身

### 6.1 `video-production/SKILL.md` 重写（160 行 → 目标 ≤90 行）

按 skill-creator 规范四要素裁剪。**删**：
- "This skill is the only place the six media tool schemas are exposed" 一句——
  改造后为假；
- 一切"反正服务端会拒绝你"的复述性条款（如 spend 未批则 submit 必拒、
  idempotency_key 必须等于服务端给的值等）——保留一句总述："全部门禁由服务端
  强制，`video_project(status)` 随时能告诉你下一步缺什么"；
- 禁止 Batch 包裹媒体工具的段落——已由白名单结构性保证（§3），删文字。

**留**：目的与适用场景、五段式 prompt 配方的 references 路由、真人 vs 虚拟主持人的
分类规则与授权流程（非显然约束）、b-roll 免转写条款、"进度问题一律先 `status`"。
frontmatter 的 `allowed-tools` 保留原列表（现在是纯文档：读者知道该技能围绕哪些
工具展开）。

改完 SKILL.md **重启后端**（reload 不看它，CLEANUP §8.2）。

### 6.2 `skill-creator/SKILL.md` 规范更新

`allowed-tools` 的描述句改为明示语义："文档性字段，声明该技能围绕哪些既有工具
展开，供列表展示；**对运行时工具可用性零效果**——工具暴露由 agent 白名单与权限
规则决定。" 防止后来者按旧语义写新技能。

### 6.3 schema 瘦身（对冲 +5.3K tokens）

八工具恒定暴露后，schema 常驻每次 LLM 调用。预算：七个原门控工具（skill_manage
不计，本就小）的**实际 provider payload** 从 **11,632 字符压到 ≤10,000**。
Pydantic 原始 `model_json_schema()` 的 16,056 含 provider 发送前必然移除的 title / nullable
噪声，不作为 token 预算口径。瘦身对象是 description 里的流程性散文（那些属于技能
文档），保留一切约束性语句（参数取值边界、幂等键格式、安全语义）。测量脚本（改前
改后各跑一次，数字记入 commit message）：

```bash
cd backend && uv run python - <<'EOF'
import json
from agent.llm import _tool_parameters_schema
from tool.registry import register_builtin_tools, get_tool
register_builtin_tools()
total = 0
for n in ["video_project","video_generate","video_transcribe","video_render",
          "image_gen","creator_context"]:
    t = get_tool(n); p = _tool_parameters_schema(t)
    s = len(json.dumps({"name":n,"description":t.description,"parameters":p}, ensure_ascii=False))
    total += s; print(f"{n:16} {s:6,}")
print(f"{'合计':16} {total:,}")
EOF
```

瘦身后跑全量后端测试——有测试断言 description 片段的话按新文案更新，**不许为过
测试而把散文塞回去**。

### 6.4 PR#2 Definition of Done

后端全绿；测量脚本 ≤10,000；`wc -l` SKILL.md ≤90；后端重启后浏览器验收 §8。

---

## 7. 已定决策（不再开口子）

| 决策 | 定论 | 依据 |
|---|---|---|
| 暴露方案 | **恒定注册 + schema 瘦身**（B1） | 与三家主流一致；视频/图像是本产品核心能力，核心能力的工具就该像 bash 一样恒在。分组 stub 按需展开（B2）另行记入 Backlog，仅当 token 预算实测成为问题再启 |
| `allowed-tools` 语义 | 纯文档/展示，两个信任层一致零效果 | 留"限制"语义都不留——本仓库技能注入主对话而非独立子上下文，加载时收窄主工具表有害无益；且零效果才没有第二次反转的缝 |
| 容器技能的 `allowed_tools` | 读取端显式忽略 + debug log | §2 不变式；log 是为了发现有人还在按旧语义写 |
| 子 agent 是否给媒体工具 | 不给（explore/general/plan 均不加） | 顺序安全约束从提示词升级为白名单结构 |
| `skill_manage` 一并解禁 | 是，进 build 白名单 | 其安装校验在服务端（路径校验/原子写/属主记录），不靠隐藏保安全 |
| 激活状态持久化 | 概念整体删除，无替代 | 工具恒在，无状态可持久 |
| 前端/移动端 | 零改动 | 工具表后端拼装；技能中心展示字段未变 |

---

## 8. 浏览器验收场景（PR#2 后跑；前置与凭据见 CLEANUP §7/§0.3）

- **A 能力直达（改造核心验收）**：新会话**不提任何技能名**，直接发——
  "帮我把一个想法做成竖屏短视频，先创建项目写好台词发起剧本审批就停。"
  验收：模型**无需先加载技能**即可调用 `video_project`（加载技能属加分不属必需）；
  全程无 unknown tool；审批卡弹出后"跳过"收尾，零花费。
- **B 多轮免重载**：场景 A 的会话里再发第二条消息（如"标题换成《解耦验收》"）。
  验收：新回合直接调 `video_project`，**不需要**重新加载技能，无 unknown tool——
  每轮重激活税确认消失。
- **C 授予面关闭（信任分层验收）**：在无影云沙箱内放一个声明
  `allowed-tools: [bash, video_generate]` 的测试技能（用 skill_manage 或直接写
  容器目录），加载它。验收：输出无 "Activated tools" 字样、metadata 无
  `activated_tools`、后端日志出现忽略记录；随后工具行为与加载前完全一致。
  验完删除该测试技能。
- **D 教学回归**：正常委托一次视频制作到剧本审批（可复用场景 A 会话），确认瘦身后
  的 SKILL.md 仍足以引导模型走对流程（creator_context 先行、完整台词入 chat、
  先 set_script 后审批）。若模型走错流程，缺的那句话加回 SKILL.md——教学内容以
  实测为准，不以行数目标为准。

---

## 9. 已知坑（含本手册作者排查中亲踩）

1. **八工具不在任何 agent 白名单里**（§5.1）——只删授予链不加白名单，能力直接
   消失，且所有测试可能照样全绿（它们多在 tool 层测）。场景 A 就是为此设的。
2. `resolve_step_tools` 的 skill_only 过滤删除时，注意 `:120-124` 是个 dict
   推导——别把 `get_tools_for_agent` 的白名单过滤一起删了。
3. 改 SKILL.md / locale 后必须手动重启后端（reload 只看 .py）。
4. `test_skill_tool_activation.py` 是旧行为的守卫，直接删会丢掉"容器技能不得授予"
   这条不变式的测试位——按 §5.3 重写为反向锚点，不是删除。
5. 别动 `merge_sandbox_tools`：搜 "sandbox" 时它会一起冒出来，它是 MCP 通道。
6. lint 基线仍是 `content-view.ts` 两处既有错误；本次不应触碰前端，出现第三个
   错误说明你改错了仓库区域。
7. 提交规范、公开仓库红线、凭据位置：CLEANUP §0.3 / §8.9 / §8.10 全部适用。

---

## 10. Backlog（本手册明确不做）

1. **分组 stub 按需展开机制（B2）**：实测全量 build 工具定义仍达到约 56.5K 字符，
   已独立立项；完整执行手册见 `docs/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md`。本手册的
   skill/tool 解耦不变式继续作为其前置条件。
2. **容器技能内容的提示注入加固**：用户安装技能的 markdown 正文仍原样注入对话，
   属不可信文本。本次只关"授予"洞；内容级防护（隔离标注、能力声明白名单化）另行
   评估。
3. `video-production` 的 `references/` 目录逐篇按同一规范复审（本次只动 SKILL.md
   主文件）。
