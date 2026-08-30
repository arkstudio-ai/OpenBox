# 工具目录与 Schema 延迟物化——执行手册

> 文档状态：Implemented & Verified v3（2026-08-31，无影云双平面与能力分层适配版）
> 基线 commit：`d453f78`（技能/工具解耦已经完成）
> 实现 commit：`5702fd4`（Schema 延迟物化、持久 reveal、MCP 投影与安全边界）
> v2 相对 v1 的适配（按当前产品事实收敛，全文已同步改写）：
> ① 前端只有 `frontend-v2` 与 `mobile`；`frontend/`（v1）是遗留迁移参考，零触碰。
> ② 沙箱执行面唯一生产形态是阿里云无影云电脑（`SANDBOX_PROVIDER=wuying`）；
> docker/k8s provider 代码与 `k8s/` 部署文件冻结，不作为验收环境。
> ③ agent loop 在后端、执行层在无影云的**双平面架构**成为一等设计输入（§1.4）；
> 与 codex/opencode 的单进程一体架构不同，规划期不得触隧道（铁律 12）。
> ④ 内置能力（native tool、host skill、平台 pack）与用户安装到无影云的能力
> （沙箱 skill、沙箱 MCP）按**平台面/沙箱面**分层暴露（§2.6–§2.7、§4.1）。
> 前置手册：`docs/SKILL_TOOL_DECOUPLING_PLAN.md`。本文件把其 §10 Backlog B2
> 升级为独立工程，但**不推翻**原手册的信任边界。原计划的“工具恒定注册”继续成立；
> “所有完整 schema 每个 step 恒定发送”不再是目标。两者不是一回事。
> **本文件面向没有参与前期讨论的执行者。** 必须按 PR 顺序推进；每一阶段 DoD 全绿后
> 才能进入下一阶段。遇到本文未覆盖、且会改变权限、计费或重复执行语义的问题，停下来
> 报告，不得用 `emergency_eager` 全量暴露掩盖。

---

## 0. 执行者须知

### 0.1 本轮目标

OpenBox 当前把 build agent 的 30 个完整工具定义放进每一次 provider 请求。工具很多、
描述很长时，这会同时增加输入 token、降低工具选择精度，并挤占真正的任务上下文。

本轮要实现：

1. 所有工具继续由平台注册，agent 白名单与 permission 继续决定资格；
2. 每个 step 只把“小型常驻核心 + 当前任务所需能力”完整物化给模型；
3. 明确意图由确定性路由首轮直达，模糊意图通过小型目录搜索发现；
4. OpenAI Responses / Anthropic 在经过真实协议验收后使用原生 deferred tool search；
5. Gemini、Kimi、未知 OpenAI-compatible 网关始终有 OpenBox 自己的 portable 路径；
6. 多 step、跨用户消息、重启与 compaction 后不重复搜索，也不让工具无限累积；
7. 隐藏 schema 只是一项上下文优化，任何执行仍重新经过权限、审批、付费与沙箱门；
8. 目录规划全程是后端本地计算：沙箱侧能力只以缓存投影参与，planner、预算器与
   discovery 检索不触发任何跨隧道调用（现存的每 step 目录拉取在 PR#5 收敛为投影）；
9. 内置与用户自装分层落地：resident core、deterministic intent pack、prompt fragment
   与 `same_response_safe=True` 只引用平台面能力；用户装进无影云的 MCP/技能属沙箱面，
   只经 discovery 进入，其一切字符串按不可信输入清洗与预算。

成功不是“provider payload 变小”这一件事。完整成功定义是：

- 通用编码请求首轮只看见常驻核心；
- 视频、图片、网页、浏览器、自动化等明确请求首轮直接看见对应能力包，不要求用户或
  模型先说出技能名；
- 模糊请求最多多一次 portable discovery step；
- 加载任意来源 skill（host project/global、沙箱 builtin/container）前后，资格目录和
  暴露计划都不因技能字段改变；
- denied、agent 白名单外、未购买或环境不可用的工具不进入目录搜索结果；
- Batch、模型猜名、历史 tool call、provider fallback 均不能绕过暴露与执行边界；
- 任何 paid submit 不因重试或 fallback 被执行两次；
- 用户装进无影云的 MCP 工具可被发现与调用，但从不出现在首轮 deterministic pack；
  隧道断开只造成执行期错误，不缩水资格目录，重连后无需重新发现；
- 浏览器场景与全量回归均通过。

### 0.2 与解耦计划的继承关系

`SKILL_TOOL_DECOUPLING_PLAN.md` 已经在 `d453f78` 落地。以下定论永久继承：

| 原计划定论 | 本计划如何继承 |
|---|---|
| Skill 是知识，工具是能力 | `skill` 只返回内容；不得成为 tool materialization trigger |
| `allowed-tools` 纯文档、零运行时效果 | 搜索、预加载、持久状态均不得读取它 |
| agent 白名单拥有暴露资格 | 延迟物化只能从白名单与环境形成的资格目录中做减法 |
| permission 只做限制 | 目录生成前先过滤；执行前再次检查 |
| build 拥有八个工作流工具，子 agent 默认没有 | 延迟物化不得让 plan/explore/general/custom 子 agent发现或调用它们 |
| 付费、审批、幂等由服务端强制 | schema 是否可见不改变任何业务门 |
| 内置技能与无影云用户技能按信任分层 | 推广为 §2.7 平台面/沙箱面硬边界：凡定义字节经隧道读回的能力（含全部沙箱 MCP）永不进 pack/core/prompt fragment |

本计划只取代原计划 §7 中“完整 schema 恒定进入每次请求”的 B1 发送策略；不取代工具
注册、白名单、permission 或技能语义。

### 0.3 必读文件（按顺序）

1. `docs/SKILL_TOOL_DECOUPLING_PLAN.md` 全文；
2. `docs/WUYING_SANDBOX.md` 全文：双平面拓扑、双跳隧道、action server systemd 单元、
   单租户共享桌面事实与 TUN 代理坑；
3. `backend/agent/agent.py`：AgentDef 白名单与 custom agent 默认继承；
4. `backend/tool/registry.py` 与 `backend/tool/tool.py`：注册表、ToolInfo、ToolContext、
   `.openbox/tools/*.py` host custom tool 加载（`register_custom_tools`）；
5. `backend/agent/tool_resolution.py`：native + sandbox MCP + skill listing + permission；
6. `backend/agent/loop.py` 的 `run_loop`、`_build_system_prompt` 和上下文估算；
7. `backend/agent/processor.py` 的 `process_step`：stream、工具调用持久化与执行；
8. `backend/agent/llm.py` 的 `_stream_responses_api`、`_stream_litellm_direct`、
   `_tool_parameters_schema`；
9. `backend/tool/mcp_tool.py`：当前 40 个工具阈值和 find/call 元工具；
10. `backend/tool/skill_tool.py`：动态技能目录预算与四来源合并；
11. `backend/sandbox/wuying.py`、`backend/sandbox/client.py`、
    `container/action_server.py`：执行面协议（`/execute`、`/mcp/*`、`/skills/*`）、
    `X-API-Key`、`trust_env=False`、目录接口的每 step 拉取现状；
12. `backend/tool/batch.py`：`ctx.available_tools` 的嵌套调用边界；
13. `backend/agent/prompts/system.py`：当前无条件提及的工具名；
14. `backend/tests/unit/test_skill_tool_activation.py`、`test_llm_schema.py`、
    `test_batch_parallel_safety.py`、`test_agent_registry.py`。
15. `docs/DIRECT_PATH_CLEANUP_PLAN.md` §0.3、§7、§8：本地端口、测试账号、数据库、
    无影云隧道、浏览器步骤、热重载盲区和公开仓库凭据红线。

### 0.4 术语（代码、测试和日志统一使用）

| 术语 | 精确定义 |
|---|---|
| registered | 已存在于平台 native registry 或某一 sandbox MCP catalogue |
| allowlisted | 当前 AgentDef 明确允许的 native 工具；MCP 则由配置/沙箱连接提供 |
| eligible | allowlist、租户能力、沙箱状态和 whole-tool permission 过滤后的完整资格目录 |
| discoverable | 可通过短目录搜索看到的 eligible 条目；只有名称、trigger hint 与少量参数名 |
| materialized | 完整 description + JSON Schema 已进入当前模型可见上下文 |
| executable | 当前 step 允许直接执行的 materialized 工具；仍须运行时授权与业务门 |
| resident core | 每个请求都物化、无需搜索的最小闭环 |
| intent pack | 由用户意图或产品状态一次物化的一组相关工具 |
| revealed | 通过 portable/native search 已发现，允许在后续 step 物化的工具 ID |
| 平台面（platform plane） | 定义字节由后端代码/配置产生并经 git/管理员评审：native registry、`.openbox/tools` host custom tool、host project/global skill、pack/core/prompt fragment 定义 |
| 沙箱面（sandbox plane） | 定义字节经隧道从无影云读回：沙箱 MCP 的 name/description/schema、`/opt/openbox/skills` 与 `/data/skills` 技能文件、目录列表、一切工具输出 |
| 目录投影（catalogue projection） | 后端缓存的沙箱面目录快照，带 generation/ETag；planner 与 discovery 只读它，不实时打隧道（PR#5 §12.4） |

资格、发现、物化和执行必须是不同的数据结构，禁止用一个 `dict[str, ToolInfo]` 同时代表
四层含义。

### 0.5 当前实测基线（commit `d453f78`）

测量口径：调用 `_tool_parameters_schema()`，按真实 Responses / LiteLLM wrapper 紧凑
JSON 序列化；下表是当前 legacy-eager wire，也是当前模型初始可见定义。token 使用
`o200k_base` 作为可复现代理，**不是** Anthropic/Gemini 的精确账单。native deferred
落地后必须拆分 wire catalogue 与 model-visible materialization，见 §5。

| 项目 | 当前值 |
|---|---:|
| 注册的 built-in 工具 | 32 |
| build 白名单工具 | 30 |
| Responses，含当前 4 个 host skill listing | 56,503 chars / 12,891 proxy tokens |
| LiteLLM wrapper，同上 | 56,893 chars / 12,951 proxy tokens |
| 静态 30 工具，不注入动态 skill listing | 55,460 chars / 12,599 proxy tokens |
| 动态 skill listing 增量 | 1,043 chars / 292 proxy tokens |
| 旧 22 工具 | 46,013 chars / 10,379 proxy tokens |
| 七个媒体/创作工具（不含 skill_manage） | 9,322 compact chars / 2,269 proxy tokens |
| 后端全量测试 | 930 passed / 17 existing warnings |

最大十项：

| 工具 | 当前 Responses item chars | 主要来源 |
|---|---:|---|
| `todo_write` | 11,478 | description 中的大量示例与重复规则 |
| `computer` | 5,271 | action 说明与长参数 schema |
| `task` | 4,394 | description 中的工作流与示例 |
| `bash` | 4,137 | description 中的 git/PR 流程散文 |
| `video_project` | 2,454 | 状态机参数 schema |
| `skill` | 1,966 | 基础说明 + 当前动态 skill listing |
| `image_gen` | 1,786 | 输入图片/生成参数 schema |
| `web_search` | 1,674 | description + 参数 schema |
| `batch` | 1,623 | description 示例 |
| `edit` | 1,613 | 编辑契约 |

结论：前四项占总字符约 45%；`todo_write` 单项大于整个媒体/创作包。只延迟新媒体工具
最多解决约 19%，不能解决总体问题。先瘦 description，再做延迟物化，两者都必须做。

复测脚本在 §5.1；执行者不得手工抄数字或用 Pydantic 原始 schema 代替线上口径。

### 0.6 铁律与停止条件

1. **Skill 永远不是 materialization trigger。** Skill 名、正文、frontmatter、tool
   result metadata 均不得进入 exposure planner。
2. **权限先于目录。** denied 工具的名字、描述、参数名不得出现在搜索结果或 telemetry。
3. **物化不授予。** 每次执行重新经过 `ToolHooks.authorize_tool`、服务端审批、幂等和
   sandbox 边界。
4. **Batch 不得猜名逃逸。** `ctx.available_tools` 只能是本 step 可执行集合，不能设为
   完整 eligible catalogue。
5. **partial stream 后禁止自动重放。** 一旦收到任何响应事件或 tool call，provider
   fallback 不得重发请求，避免重复副作用。
6. **structured output 永远 resident。** 本轮动态生成的合成工具不能 deferred。
7. **没有原生能力证明就走 portable。** 不得按 provider 字符串猜测支持。
8. **不使用额外 LLM 给每个用户 turn 分类。** 首轮路由使用可解释的确定性信号；模糊
   请求由主模型调用 discovery。
9. **不把所有 native 工具退化为通用 `call_tool(name,args)`。** 发现后仍向模型提供
   typed schema，保留验证、UI、审批和可观测性。
10. **普通 ToolResult metadata 不得改变 exposure state。** 只有保留 ID 的平台 discovery
    工具通过专用内部 callback/typed outcome，或经验证的 provider tool_reference，才能提交
    reveal；这条边界不能重演旧 `skill → metadata → activated_tools` 授予链。
11. **沙箱面数据不是 trigger，也永不升平台面。** 凡定义字节经隧道读回的能力与文本
    （MCP 工具名/描述/schema、四来源技能文件、目录列表、工具输出）一律按不可信输入
    处理：只能经清洗、预算与有界 canonical ID 进入 discoverable 层；不得进入
    resident core、deterministic intent pack、prompt fragment、`same_response_safe=True`
    或路由信号。平台经 bootstrap 预置到桌面盘的内容（`/opt/openbox/skills`）落盘后
    同样按沙箱面处理——桌面上的任何执行都能改写它。
12. **Exposure 规划零隧道。** `collect_exposure_signals`、planner、预算器与
    `capability_search` 检索只读后端内存/数据库与缓存的目录投影。PR#5 落地投影前，
    现存的每 step 目录拉取维持原样（坑 22），但新增代码不得引入新的规划期隧道调用。
13. 任何权限泄漏、重复 paid submit、子 agent 获得媒体工具，均是立即停止发布的 P0。

---

## 1. 当前机制与成本来源

### 1.1 每个 provider step 的真实调用链

```text
run_loop while
  → resolve_step_tools(agent_def, sandbox, config_rules)
      → get_tools_for_agent(agent_def.tools)
      → merge_sandbox_tools(...)
      → attach_skill_listing(...)
      → strip_denied(...)
  → ctx.available_tools = frozenset(tools)
  → _build_system_prompt(...)
  → process_step(..., tools)
  → stream_llm(..., tools)
      → Responses: _tool_parameters_schema → payload["tools"]
      → LiteLLM:  _tool_parameters_schema → call_kwargs["tools"]
```

同一条用户消息若经历“模型 → 工具 → 模型 → 工具”，while 每一步都会重新 resolve、
重新构造动态 skill/MCP 工具并重新发送 schema。prompt cache 可能减少计费或延迟，但
HTTP payload 仍含 definitions，模型选择空间仍然过大，不能把 cache hit 当作问题已解决。

### 1.2 当前能力包测量

以下仅用于设计边界；实现后以 §5 预算测试为准：

| 包 | 工具 | 当前 chars / proxy tokens |
|---|---|---:|
| core 候选 | bash/read/write/edit/apply_patch/glob/grep/task/question/skill | 18,642 / 4,270 |
| planning | todo_write/todo_read/plan_enter | 12,864 / 2,758 |
| efficiency | multiedit/batch | 2,807 / 666 |
| research | web_fetch/web_search | 2,769 / 640 |
| browser I/O | view_image/share_file/computer/browser_mode | 7,523 / 1,698 |
| automation | cron | 1,413 / 357 |
| media/creator | image_gen + 五个 video/identity/project 工具 + creator_context | 9,322 / 2,269 |
| skill admin | skill_manage | 1,170 / 247 |

当前 core 候选同时含 `edit` 与 `apply_patch`。目标实现必须按模型只保留一个主编辑器，
并增加很小的 discovery tool，因此最终 core 应低于该表而不是机械复制。

### 1.3 动态成本

- `skill` 描述每 step 注入可用技能。当前 `LISTING_BUDGET_TOKENS=2000` 只严格预算完整
  description；进入 names-only 后名称尾部仍可能无限增长。
- sandbox MCP 当前 `<=40` 时全部 materialize，`>40` 才切换成
  `mcp_find_tool + mcp_call_tool`。工具数量不是成本：39 个巨大 schema 仍能击穿预算。
- tool outputs 会作为下一 step 的 `role=tool` / `function_call_output` 进入历史。延迟
  schema 不会减少已经执行后的输出成本；输出截断与 compaction 仍需保留。
- `loop.py` 当前用 `len(tools) * 400` 估算 schema context；同样数量的
  `view_image` 与 `todo_write` 实际相差近 20 倍，此估算必须替换。

### 1.4 双平面架构事实（无影云）

OpenBox 与 codex/opencode 的根本差异：agent loop、permission、catalogue 规划与 LLM
调用全部在后端控制面；bash/文件/MCP server/媒体队列/dev-browser 全部在无影云桌面的
action server 执行面（`container/action_server.py`），两者之间只有一条
`WUYING_ENDPOINT=http://127.0.0.1:18000` 的双跳 SSH 隧道（laptop → relay ECS →
桌面反向隧道；完整拓扑见 `docs/WUYING_SANDBOX.md`）。

```text
backend 控制面                          无影云执行面（共享单桌面）
  run_loop / planner / permission        action_server :8000（systemd 自愈）
  EligibleCatalog / ExposurePlan           /execute /read_file /write_file /glob /grep
  provider adapters                        /mcp/tools  /mcp/tools/{server}/{tool}
        │   SandboxClient(HTTP,            /skills  /skills/{name}
        │   X-API-Key, trust_env=False)    /media/jobs/*  /dev-browser/*
        └── 127.0.0.1:18000 隧道 ────────  /alive（uptime/hostname）
```

延迟物化必须知道的实测事实：

- `resolve_step_tools` 在 `loop.py` 主循环内**每 step** 调用；其中
  `merge_sandbox_tools` 每 step `GET /mcp/tools`、`attach_skill_listing` 每 step
  `GET /skills`（后者裁剪文件列表前曾实测 55KB/step，现仍是每步全量往返，见
  action_server.py `_SKILL_FILE_SKIP_DIRS` 注释）。这是 backend↔沙箱 的 wire 成本
  与时延，不进模型 token，但它决定了 §12.4 目录投影的必要性；
- 沙箱三 provider（docker/kubernetes/wuying）代码都在
  （`backend/sandbox/__init__.py` `_create_provider`，config 默认仍是 `docker`）。
  生产唯一形态是 `wuying`；docker/k8s 是遗留冻结路径（§6），本计划不为它们写任何
  exposure 分支；
- 无影桌面是带外预置、长期存活、**所有会话共享的一台桌面**，隔离只有
  `/workspace/sessions/<id>` 工作目录，action server 不约束绝对路径——当前按
  单租户对待。目录投影与 reveal state 仍按 user/session key 隔离缓存，但
  "桌面上装了什么"在事实上是全局状态（坑 24）；
- 桌面重启后 systemd 拉起 action server 并 `reconnect_configured()` 自动重连 MCP
  （工具集可能变化），dev-browser 不自启；隧道断开时后端启动只 loud log 不 abort。

推论：codex 能在同一进程每 step 重算 ToolExposure，因为规划与执行共内存；OpenBox
的等价物必须是"执行面发布目录、控制面缓存投影"。任何把隧道调用放进规划热路径的
设计都同时损害时延与可用性。执行期与规划期的失败语义必须分开：隧道断开是执行
错误（清晰报错、重连可重试），永远不是资格目录收缩的理由（§2.1）。

---

## 2. 安全语义模型

### 2.1 强制顺序

```text
global native registry / sandbox MCP catalogue
  → AgentDef allowlist + static entitlement/config availability
  → config rules + agent rules whole-tool deny
  = eligible_catalog
  → compact discoverable catalogue
  → resident core + deterministic packs + valid revealed state
  = exposure_plan
  → provider adapter materialization
  = provider_payload_tools
  → normalized reveal evidence + direct tool calls
  = step_executable_ids
  → ToolHooks + business gates + sandbox
  = execution
```

`eligible_catalog` 是“有资格被选择的全集”，不是当前可调用集合。它不能直接放入
`ToolContext.available_tools`，也不能直接传给 Batch。

“availability”只指稳定资格（agent、租户、配置、sandbox 是否存在），不以一次健康检查或
provider 暂时不可达为由移除恢复能力。已有 video/image/MCP job 的 status、cancel、reconcile
工具必须保持 eligible/pinned，并在调用后返回真实 readiness/recovery 信息。

无影云下这条具体化为：隧道断开、`/alive` 失败或 action server 重启都不改变
eligible/discoverable 集合；沙箱依赖工具（bash/文件/MCP/媒体）此时执行返回清晰错误，
重连后可直接重试，已 revealed 的沙箱 MCP 工具无需重新发现。目录投影过期按 §12.4 的
generation 规则刷新，不做"探测失败即隐藏"。

### 2.2 Skill 零副作用

以下三类技能执行前后必须满足：

```text
eligible_ids_before == eligible_ids_after
discoverable_ids_before == discoverable_ids_after
materialized_ids_before == materialized_ids_after
```

适用来源：**四个**，不是解耦手册写的三个——host `project`（`backend/.openbox/skills`）、
host `global`（`~/.config/openbox/skills`）、沙箱预置 `builtin`
（`/opt/openbox/skills`，bootstrap 推送，action_server `BUILTIN_SKILLS_DIR`）与沙箱
用户 `container`（`/data/skills`，技能中心安装，`SKILLS_DIR`）。沙箱预置技能虽由平台
下发，落盘后可被桌面上的任何执行改写，运行时信任级别等同用户技能（铁律 11）。即使
正文或 frontmatter 写了 `allowed-tools`、`allowed_tools`、`tools`、`requires-mcp` 或
未来新增同义字段，也只能作为不可信文本/展示数据。可信插件安装 MCP 是平台安装流程，
不能由 SKILL.md 加载动作触发。

### 2.3 Permission 与隐私

- `strip_denied` 仍在目录生成前执行；不要把 full registry 先建搜索索引再过滤结果。
- exact-name 搜索 denied 工具也必须返回 0；不得为了“帮助诊断”泄露名字或 description。
- 日志、metric labels/attributes、budget breakdown 和 tracing 也只能在 permission 过滤后生成；
  denied 工具的名字、description、参数 marker 均不得进 telemetry。
- 已 revealed 工具在下一 step 遇到新增 deny 时立即从 payload、可执行集合与持久状态中
  剔除。
- 参数级 permission 无法在目录阶段判定，执行时仍由 `ToolHooks` 阻断。
- permission 决策不得进入跨用户或跨项目 cache。

### 2.4 嵌套调用

`batch` 通过全局 registry 找执行函数，因此唯一防逃逸点是
`ctx.available_tools` + `_authorize_tool`。改造后：

- portable：只包含 resident、intent pack 和之前已 revealed 的 IDs；本 step search 新
  返回的 ID 到下一 step 才加入；
- native：除上述集合外，只加入当前响应中出现过合法 `tool_reference` 的 ID；
- 猜中 hidden tool 的精确名字也必须失败；
- `batch` 本身不能触发物化，更不能执行完整 eligible catalogue。

### 2.5 Agent 与会话隔离

- build、plan、explore、general 各自计算 core 与包；不能定义一个全局 core 后复制。
- `plan_exit` 只属于 plan；媒体/creator/skill_manage 只属于 build，除非平台配置显式 opt-in。
- custom agent 未显式 tools 时继续排除 `BUILD_ONLY_WORKFLOW_TOOLS`。
- 子 session 初始 reveal state 为空；父会话不得把媒体包传给 Task 子 agent。
- agent 切换时保存各自语义 state，但切回必须重新做 allowlist、permission 与 schema digest
  校验。

### 2.6 信任层

| 来源 | 平面 | 可进入 eligible catalogue | 可决定 intent pack | 可扩大权限 |
|---|---|---|---|---|
| built-in registry + AgentDef | 平台 | 是 | 平台静态配置可定义 | 否；permission 仍可减 |
| `.openbox/tools/*.py` host custom tool | 平台 | 是（随 registry 注册） | 平台配置可定义 | 否 |
| 平台 product state（active video/job/browser） | 平台 | 只选择已有 eligible | 是 | 否 |
| 用户自然语言 | — | 只影响确定性路由/搜索 query | 是 | 否 |
| host project/global Skill | 平台（知识） | 否 | **否** | **否** |
| 沙箱技能（`/opt/openbox/skills` 预置 + `/data/skills` 用户装） | 沙箱（知识） | 否 | **否** | **否** |
| 沙箱 MCP server（catalog 安装或用户手装，一律同权） | 沙箱 | 经正规注册通道 + 清洗/预算/有界 ID | **否，永不** | 否 |
| provider tool_reference | — | 只引用已发给该 provider 的 eligible tool | 是，限当前响应 | 否 |

安装 provenance（`skill/catalog.py` 的 MCP_CATALOG 商店安装 vs 用户直接 add）只用于
UI/telemetry 展示。桌面上的 `/data/mcp/config.json` 与 server 进程本身都可被沙箱内
任意执行改写，因此 provenance 不参与任何信任判定——所有沙箱 MCP 同层。

### 2.7 平台面/沙箱面硬边界

1. **平台面能力**（native built-in、`.openbox/tools` host custom tool）：
   discovery hint、pack 归属、same-response-safe 审计、prompt fragment 全部由平台
   代码/配置定义，随 git/管理员评审；
2. **沙箱面能力**（一切定义字节经隧道读回的条目——当前即全部 MCP）：只能出现在
   discoverable 与 revealed-then-materialized 两层；`discovery_hint` 从远端
   description 清洗截断而来并计入预算；canonical ID 用 §9.1 的 `mcp:v2:` 有界摘要；
   `same_response_safe` 恒为 `False`；不进任何 deterministic pack、resident core 或
   prompt fragment；
3. **沙箱面知识**（四来源技能正文，§2.2）照旧可注入对话，但对
   eligible/discoverable/materialized 三层零效果，正文属不可信文本（提示注入加固仍
   在 Backlog）；
4. 当前**不存在** backend 直连的"平台托管 MCP"（`config.mcp` 只用于 OAuth 元数据，
   不在 session 启动时 seed 沙箱）。若未来出现，其进入 pack 的资格须单独立项与审计
   （Backlog 10），不得复用沙箱 MCP 通道的默认信任。

---

## 3. Provider 事实与策略矩阵

### 3.1 Anthropic

官方 Tool Search 支持 `defer_loading: true`：搜索工具本身必须常驻，建议保留最常用的
3–5 个普通工具。所有 definition 仍在 API request 的 tools 数组中，但 deferred definition
不进入初始模型工具区；命中后以 `tool_reference` 展开，并保持 prompt cache 前缀稳定。

官方文档：
`https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool`

实现要求：

- 不得假定 LiteLLM 会无损透传 `defer_loading`、server tool event 与 tool_reference；
- 在 wire-level contract test 通过前，Anthropic 走 portable；
- 若 LiteLLM 无法无损透传，增加 direct Anthropic adapter，不在 processor 中写 provider
  特判；
- native adapter 必须解析 search/reveal/call 顺序，并将 reveal 转成统一事件。

### 3.2 OpenAI Responses

Responses API 当前 reference 暴露 hosted/BYOT tool search configuration for deferred tools。
是否可用取决于 endpoint、API 版本与 model，不得仅凭 `provider == "openai"` 开启。

官方 reference 与 model guidance：
`https://developers.openai.com/api/reference/cli/resources/responses/methods/create`、
`https://developers.openai.com/api/docs/guides/latest-model`

当前 `_stream_responses_api` 只发送普通 function definition，也不解析 tool-search output。
native fast path 必须同时补齐 request、SSE event、history replay 与 usage；只加请求字段不算
完成。

### 3.3 Gemini

Gemini 官方明确 function declarations/description/parameters 计入输入 token，并建议 active
set 尽量保持 10–20 个、总目录大时按 conversation context 动态选择。

官方文档：
`https://ai.google.dev/gemini-api/docs/generate-content/function-calling#best_practices`

本计划不假设 Gemini 通用 API 具备与 Anthropic 等价的 deferred tool_reference 协议。
Gemini 先走 portable；`allowed_function_names` 只是调用限制，不等于删除仍发送的 declaration。

### 3.4 Kimi 与未知兼容网关

默认 portable。OpenAI-compatible 只说明 JSON 外形相近，不说明支持 Responses Tool Search、
deferred 字段、event replay 或相同的 tool call ID 约束。

### 3.5 Workspace 参考实现与市场对照

本机两套实现已逐源验证，另加 Claude Code harness 的公开可观察行为作为市场基准。
结论按"可借鉴的结构"记录，不照搬任何枚举名或默认值。

**codex（`/Users/wang/workspace/codex/codex-rs`，单进程一体架构）**

- `tools/src/tool_executor.rs` 的 `ToolExposure` 六态（Direct / Deferred /
  DeferredModelOnly / DirectModelOnly / CodeModeOnly / Hidden）：工具自报偏好、host
  每 step 终裁（`core/src/tools/spec_plan.rs` 构造并 finalize router）。可借鉴的是
  "能执行 / 本轮直接展示 / 可被搜索"三事分离，而非枚举本身；
- 原生 deferred 已在其 Responses 结构体落地：`tools/src/responses_api.rs` 的
  `defer_loading: Option<bool>` 字段，deferred 项还剥掉 output_schema；发现走单个
  `tool_search` 工具 + BM25 索引（`core/src/tools/handlers/tool_search.rs`，默认返回
  8 项），命中的完整 schema 于下一次请求物化——两段式与本计划 §4.6/§4.7 一致，
  可作 §11.1 Responses adapter 的行为对照物；
- MCP 策略：开启 search 时 MCP 默认全部 Deferred（`core/src/mcp_tool_exposure.rs`）；
  预算为 description ≤1K、单 spec 序列化 ≤8K（超限把 parameters 替换成
  `additionalProperties:true` 开放对象）、agent-plugin 聚合 ≤64K（超限直接降
  Hidden）。本计划在单项 >5K 时选择"保留原 schema 走 meta 调用"而非删参数约束
  （§5.2）——有意分歧：OpenBox 的 MCP 参数约束是执行侧验证的一部分，不牺牲；
- prompt cache 稳定性：注册表用插入序 IndexMap、namespace 内按名排序、tool_search
  handler 只在 deferred 源集合变化时重建——印证 §4.1 "tuple 排序稳定"与 §4.9 的
  片段稳定追加；
- 架构对照（本计划最重要的差异输入）：codex 规划与执行共进程共内存，per-step 重算
  exposure 零成本，沙箱是 per-command 的 OS 原生隔离（seatbelt/landlock）。OpenBox
  的执行面在无影云另一端，等价物必须是"执行面发布目录 + 控制面缓存投影"
  （§1.4、§12.4），不能翻译成每 step 跨隧道重拉。

**opencode（`/Users/wang/workspace/opencode`）**

- 现行实现每 turn 组装工具（`packages/opencode/src/session/tools.ts`），whole-tool
  deny 在请求组装层剔除；v2 `packages/core/src/tool/registry.ts` 把 whole-tool
  permission 过滤放进 `materialize()`，并返回绑定当次物化身份的 `settle`，用
  identity token 拒绝"调用上一 turn 已消失的工具"——与本计划的 schema digest /
  catalogue generation 校验同构，可作 §4.8 失效语义参照；
- `packages/core/src/tool/skill.ts` 只加载内容、frontmatter 无 `allowed-tools`
  字段、技能全量列进 system prompt——继续证明 skill 与 tool 是两条独立链；其对
  listing 无预算是弱点而非榜样，印证 §12.1 必须补 `skill_search`；
- MCP 反面教材：sanitize 后同名静默 `result[key]=` 覆盖、无数量/大小阈值——本计划
  §9.1 的碰撞 fail-closed 与双向映射正是针对这类洞；正面参考：按
  `ToolListChangedNotification` 事件刷新缓存目录、分页拉取——§12.4 的 generation
  失效应对齐"事件驱动刷新"，不是每 step 重拉；
- Code Mode（实验）把全部 MCP 折叠成一个 `execute` 工具 + 代码目录：是"折叠"而非
  "延迟"，丢失 typed 验证/审批/UI，本计划不采用（铁律 9），记录以备 Backlog 讨论。

**Claude Code harness（公开可观察行为，市场基准）**

- deferred 工具只以名字出现在上下文，模型用检索工具按 `select:<name,...>` 精确批量
  加载 schema，并被明确要求"把预期要用的工具合并进一次加载调用"——印证 §4.6 的
  exact `names` 参数与"pack 一次物化优于逐个 reveal"；
- MCP server 可自带 instructions 指明"这类任务先加载哪组工具"——与 §4.4 intent
  pack 同构；但 OpenBox 的 pack 定义只在平台面（§2.7），不接受沙箱 MCP 自述；
- 技能以一行 name+description 列出、按需加载全文——与 §12.1 listing 预算方向一致；
- Anthropic 官方 Tool Search Tool（§3.1）建议保留 3–5 个最常用工具不 deferred，与
  §4.3 resident core 的量级互相印证。

不得照搬 Codex 中可信插件/MCP 的平台安装例外到无影云沙箱面；OpenBox 的 Skill
frontmatter 与沙箱 MCP 自述仍是零能力语义。

### 3.6 策略选择

新增统一枚举：

```text
legacy_eager   # 迁移基线；PR#0/#2 默认，不是紧急授权
shadow         # 计算 exposure plan/指标，但仍按 legacy_eager 发送
portable       # OpenBox discovery + 下一 step typed materialization
native_auto    # capability probe 成功才 native，否则 sticky portable
emergency_eager # 显式人工开关：eligible built-ins direct + filtered/capped MCP meta
```

能力 cache key 至少包含：provider adapter、normalized endpoint、API version、model snapshot、
credential/account digest、region、beta headers 与 config generation。不要只用 model name 或
`_detect_provider()` 结果；同 endpoint/model 的另一个租户可能没有相同 entitlement。cache
隔离测试必须证明一个账号的 unsupported 结果不会污染另一个账号。

native 请求在**任何响应字节/事件之前**收到明确“不支持特性”的 4xx，最多重试一次
portable，并按完整 capability-key digest + TTL 记录 sticky fallback。普通 schema 400
不得吞掉后切 `emergency_eager`；
partial stream、tool_reference 或 tool call 出现后禁止重放。

---

## 4. 目标架构

### 4.1 新的数据结构

在 `backend/agent/tool_exposure.py` 建立纯数据层，禁止依赖 Skill：

```python
@dataclass(frozen=True)
class CatalogEntry:
    id: str
    provider_name: str
    discovery_hint: str
    parameter_names: tuple[str, ...]
    source: Literal["builtin", "mcp", "synthetic"]
    plane: Literal["platform", "sandbox"]
    pack: str | None
    schema_digest: str
    schema_chars: int
    same_response_safe: bool = False

@dataclass(frozen=True)
class EligibleCatalog:
    tools: Mapping[str, ToolInfo]
    entries: Mapping[str, CatalogEntry]
    generation: str

@dataclass(frozen=True)
class ExposurePlan:
    direct_ids: tuple[str, ...]
    deferred_ids: tuple[str, ...]
    discovery_ids: tuple[str, ...]
    reasons: Mapping[str, str]
    strategy: str
    schema_chars: int
```

约束：

- `plane` 由注册通道决定，不可配置覆盖：native registry 与 `.openbox/tools` host
  custom tool 为 `platform`；一切定义字节经隧道读回的条目（当前全部 MCP）为
  `sandbox`。构造器校验 `pack is not None` 与 `same_response_safe=True` 都要求
  `plane == "platform"`，违反即 fail closed（§2.7）；
- `CatalogEntry.discovery_hint` 是 1–2 句触发提示，不复制 full description；
  `plane == "sandbox"` 条目的 hint 从远端 description 清洗（去控制字符、转义
  HTML/XML）并截断（≤200 chars）得到，不得原样透传；
- `id` 是稳定、有界的平台 canonical ID，`provider_name` 才是按 provider 限制清洗/
  截断的调用名。built-in 使用固定注册 ID；MCP 使用 §9.1 定义的 59 字符
  `mcp:v2:<sha256-base32>`，并在当前 catalogue 的双向映射中无损保留原始 server/tool
  身份。旧 sanitized 名唯一时为了兼容可保留，只有发生碰撞时才给 provider 名称的
  碰撞组每项加稳定 hash suffix。执行、permission、always-allow 和持久 state 全部
  按 `id`，不按可碰撞的展示名；
- pack 与 core profile 由平台代码/配置定义，不放进 SKILL.md；
- 所有 tuple 排序稳定，保证 prompt cache 前缀与测试快照稳定；
- `schema_digest` 对 normalized provider-neutral schema + description 计算；
- `same_response_safe` 默认 `False`，只能由平台审计表设为 `True`；Skill、MCP metadata、
  tool output 不得修改它；
- synthetic 可构造一个仅用于当次 provider plan/计量的瞬时 `CatalogEntry`，但不插入
  `EligibleCatalog.entries`；
- pure planner 不访问数据库、网络或 provider，输入相同结果必须相同。

### 4.2 四个运行时对象分离

loop / processor API 必须显式传递：

1. `eligible_catalog`：平台 native/MCP 工具的资格全集，供 discovery 和重新校验；
2. `provider_plan`：当前请求真正发送/标 deferred 的定义；
3. `execution_lookup`：本 step 真正能映射到执行函数的 ToolInfo，等于 eligible 工具加
   当次瞬时 synthetic structured-output 工具；
4. `step_executable_ids`：当前允许直接执行及 Batch 嵌套的 ID 集合。

当前 `process_step(..., tools)` 同时承担序列化、错误提示和执行 lookup，应拆成上述
四个明确参数。调用时先检查 canonical ID 属于 `step_executable_ids`，再从
`execution_lookup` 取 ToolInfo；禁止只因 lookup 有该 key 就执行。
`ToolContext.available_tools` 绑定 `step_executable_ids`。unknown tool 错误只列当前 materialized
工具，不列完整 eligible catalogue。

### 4.3 常驻核心

build 目标 core：

| 类别 | 工具 | 决策 |
|---|---|---|
| sandbox 底座 | `bash` | build 常驻；其他 agent 按自身白名单，不是全局授予 |
| 文件读写 | `read`, `write`, `glob`, `grep` | 常驻，避免模型用 cat/echo/find 退化模拟 |
| 主编辑器 | `edit` **或** `apply_patch` | 按 model prompt 只选一个；禁止两者无条件同时常驻 |
| 知识加载 | `skill` | loader 常驻，正文/引用延迟 |
| 能力发现 | 逻辑 `capability_search` slot | 永不 deferred；portable/native 二选一映射 |
| 交互 | `question` | build 常驻；仍受 permission |
| 多 Agent | 精简后的 `task` | build 常驻；子 agent 不继承父 exposure state |

`todo_write` 不以当前 11.5K description 常驻。PR#1 精简后，由复杂任务信号或已有 todo
状态加载 planning pack。若产品最终要求每个 build 请求都显示 todo，必须用实测证明其收益
高于常驻成本，并单独改 §5 预算；不得默认恢复。

`capability_search` 逻辑 slot 必须像其他 native tool 一样显式进入采用 deferred 的
built-in agent 白名单；planner 不得绕过 AgentDef 临时注入。portable adapter 把该 slot
物化为 OpenBox function，native adapter 把它映射为 provider server-search primitive；同一
请求绝不同时发送两者。config-defined agent 只有在明确列出该 slot 时才有
discovery，合法 `tools: []` 仍保持完全为空。未列 discovery 的 custom agent 将其显式工具
视为管理员选择的 direct set，并在配置加载时检查 32K hard cap；超限必须增加 discovery、
缩小列表或显式选择 `emergency_eager`，不能运行中静默丢工具。若 built-in agent 的管理员
显式 deny discovery，portable 路径只用 core 与确定性 pack，不得为补偿而全量 eager。

plan/explore/general 使用各自最小 core：plan 常驻 `plan_exit`；explore 无写工具；general
保持现有白名单边界。profile 测试必须枚举所有 built-in agent，并覆盖 config-defined agent。

### 4.4 Intent packs

| pack | 工具 | 首轮确定性触发 |
|---|---|---|
| planning | `todo_write`, `todo_read`, `plan_enter` | 复杂多步请求、已有 todo、明确要求计划 |
| efficiency | `batch`, `multiedit` | 多个独立操作；不得用于媒体状态机 |
| research | `web_search`, `web_fetch` | URL、最新/查证/搜索/文档意图 |
| browser | `browser_mode`, `computer` | 浏览器/桌面操作、已有 browser workflow |
| vision | `view_image` | 用户图片、待查看本地图片或截图 |
| delivery | `share_file` | 已有待交付文件或明确下载/导出意图 |
| automation | `cron` | 定时、周期、提醒、监控意图 |
| image | `image_gen`, `view_image` | 生成/编辑图片或图片附件 |
| video | `creator_context`, `image_gen`, `video_identity`, `video_project`, `video_generate`, `video_transcribe`, `video_render` | 明确视频意图或存在 active production/job/approval |
| skill_admin | `skill_manage`, `share_file` | 创建、导入、导出个人技能 |

视频包整体一次物化，不在第一版逐状态拆成六次搜索。它当前约 2.5K proxy tokens，整体加载
可以保证 `project → generate → transcribe → render` 续接稳定；付费与顺序仍由服务端门保证。
后续只有生产遥测证明值得，才在 Backlog 中细分。

pack 成员只能是平台面 native 工具 ID（`plane == "platform"`，§2.7）。沙箱 MCP 工具
无论多常用都不进 pack——用户自装能力的首轮路径永远是 discovery（最多多一个 step）。
这是分层的硬边界而非优化取舍：pack 表由平台代码定义，若沙箱侧数据能进入它，桌面上
的任何执行就能改写首轮暴露。

### 4.5 确定性路由

路由输入只允许：

- 当前用户文本的显式信号（URL、附件类型、动作关键词）；
- session agent / model；
- 平台已有结构化状态（todo 非空、active video production/job/approval、browser mode、
  已生成待交付资产）；
- provider capability 与预算；
- 有效 reveal state。

禁止输入：Skill 名、Skill 正文、frontmatter、模型上一轮自由文本中自称“已加载能力”。

先把纯 `/skill-name`、`加载/总结某技能` 等知识加载指令从任务文本中结构化剥离；只有
指令之外的真实任务语义参与 pack 路由。例如“只加载并总结 video-production skill，
不要制作视频”只能 materialize `skill`，不得因字符串 `video-production` 触发 video pack。

明显意图直接预加载，避免额外 search step。多意图取 pack union，但最终必须经过预算器；
超预算时按以下顺序：resident core → product-state pinned pack → 当前用户显式 pack → 最近
revealed → 其他候选。不得静默删除业务状态所需的恢复工具；应回退到 discovery 并记录原因。

新增不可变 `ExposureSignals`，由 `collect_exposure_signals()` 在 planner 前一次性收集：

```python
@dataclass(frozen=True)
class ExposureSignals:
    user_task_text: str
    urls: tuple[str, ...]
    attachment_kinds: tuple[str, ...]
    has_open_todos: bool
    has_active_video_production: bool
    has_active_video_job: bool
    browser_workflow_active: bool
    deliverable_asset_ids: tuple[str, ...]
```

来源固定为当前 user message parts、todo service、视频域表、browser/session mode 和当前用户
file assets；collector 不读取 Skill，也不触发任何 SandboxClient 调用——所有信号来自
后端数据库与会话内存（铁律 12）。瞬时信号查询失败时记录 `signal_error`，不扩大工具集，
保留本 run 最近一次成功的 product-state pinned signal，并让逻辑 discovery slot 作为恢复路径。
静态 entitlement/config 缺失可以令“创建新任务”工具不 eligible；瞬时 provider readiness
失败不能隐藏已有付费任务的 status/cancel/recovery 控制面，否则用户无法对账和恢复。

### 4.6 Portable discovery 映射

在 portable 模式下，逻辑 discovery slot 映射为 OpenBox function `capability_search`：

- 入参：`query`，可选 exact `names`；
- 只搜索 eligible + discoverable entries；
- 检索在后端本地目录（含沙箱目录投影）上执行，零隧道调用（铁律 12）；
- 索引名称、discovery hint、参数名、pack/tags；
- 先 exact/prefix/词法 BM25；第一版不调用额外 LLM；
- 返回最多 5 项，总结果 ≤2,000 chars；
- 结果包含 id、短 hint、参数名和“下一 step 将提供 typed schema”，不返回完整 schema；
- reveal 通过 `ToolContext` 上只供该内置工具使用的私有 callback，或专门的 typed
  `DiscoveryOutcome` 交给 state reducer；普通 ToolResult metadata 仅供 UI/持久化展示，绝不
  作为状态权限输入；
- `capability_search` 是保留 ID，custom/MCP 注册发生同名或截断碰撞时必须拒绝/重命名，
  不能覆盖该内置 ToolInfo；
- 标记 `parallel_safe=False`，Batch 不得调用；processor 按 step 聚合限制最多 2 次 search、
  5 个 unique reveal、2,000 chars 结果，多次调用不能枚举完整目录；重复 ID 去重后再计；
- 搜索本 step 不开放这些 ID 给 Batch；下一 step planner 重新校验后才 materialize。

如果本地搜索服务异常，退化为 deterministic exact/prefix/top-K；不得把完整 eligible catalogue
以 `emergency_eager` 全量暴露作为默认恢复方式。

### 4.7 Native discovery

native provider adapter 不发送 OpenBox function `capability_search`，而是把同一逻辑 slot
映射为 provider server-search primitive，并把预算内所有 eligible、非 resident definition 标为
deferred；processor 仍区分：

- `direct_ids`：请求开始即可执行；
- `native_revealed_ids`：当前响应中已收到合法 tool_reference；
- `eligible_catalog`：只用于验证 reference 和执行 lookup。

统一 stream 事件增加：

```python
{"type": "tool_revealed", "tool": "video_project", "source": "native"}
```

只有该事件出现后，同一响应中的 deferred direct call 才可执行。provider 返回未知、denied、
schema digest 不匹配的 reference 视为协议错误，不做 fuzzy 映射。

processor 必须维护按 stream 顺序演进的 response-local set：

```text
response_executable = direct_ids
on verified tool_revealed(id): response_executable += id
on tool_call(id): require id in response_executable, then lookup id in eligible_catalog
```

上述同响应加入还必须满足 `CatalogEntry.same_response_safe=True`。默认 `False` 的
工具收到合法 reference 时只持久 semantic reveal；本响应若紧接 target call，processor
必须保存该 call，用**相同 call ID**生成 provider 标准的非执行 blocked tool result
（稳定 error code `deferred_until_next_step`），且 executor 计数为 0。call/result 按原序落库
并可重放，不插 `_noop`、不留悬空 tool_use、不把旧 arguments 后端自动重放。下一
step 经重新 permission/schema-digest 校验、注入 conditional prompt 后才加入 executable，
由模型使用新 call ID 重新发起。
只有经审计证明安全约束已存在于 schema/validator/ToolHooks/service 的工具才能在平台
映射表中标记 `True`；动态 MCP 默认 `False`。

首版生产审计快照固定为：

```python
SAME_RESPONSE_SAFE_TOOL_IDS_V1 = frozenset({
    "read", "glob", "grep", "todo_read",
})
```

| ID | 为什么可同响应 |
|---|---|
| `read` / `glob` / `grep` | 只读 sandbox 资源，路径/权限仍由 ToolHooks + sandbox 强制 |
| `todo_read` | 只读当前 session todo，不改状态 |

其他全部默认 false，特别是 bash/write/edit/apply_patch/task/batch/cron/computer/browser、
skill/skill_manage/share/view_image、所有 image/video/creator 和混合 read/write action 工具。
`view_image` 虽名为查看，但当前实现会上传 OSS、创建 `FileAsset` 与新的 `FilePart`，且没有
幂等键，因此不得列入 true 快照。以后每增加
一个生产 true ID，必须同时提交上表级别的安全证据和快照变更；动态 MCP 和
Skill metadata 永远不能自证。Browser I 的 echo `True` 是本地 QA-only override，必须由
测试环境开关注入并在验收后删除，不进生产快照。

不能在 stream 开始时把 eligible 交给 `_repair_tool_name`，也不能等 stream 结束后把所有
reference 与所有 call 无序合并；“call 先于 reveal”必须失败。每个 response 最多接受
2 次 search、5 个 unique reveal、2,000 chars discovery result；重复 ID 不重复计，超限的
reference 不进入 executable set并产生协议错误/有界结果。

provider 原始事件名不同，adapter 必须先按实际接收顺序归一化为
`search_started → search_result → tool_revealed → tool_call`；禁止先缓冲全部原始块再重排。
各 provider 的原始类型与顺序分别在 §11.1/§11.2 wire contract 中锚定。

需要重放的原始有序块使用 API-hidden provider transcript part 落库，但不与 public
`PartORM` 共表，也不能只靠 Pydantic `exclude=True`。PR#3 的 migration 建立独立
`InternalPartORM`，至少含 id/session/message/user、kind、full provider-capability-key digest、
response/request chain ID、`stream_seq`、session-global `origin_seq`、data/created_at，并以 message
foreign key cascade 删除。旧服务从不查询该表，因此不会解析或泄漏未知 internal part。

受控 `save_internal_part()` 只写该表、绝不 publish event bus。`get_messages()`/REST/SSE
完全不 join 该表；只有 LLM 路径的私有
`get_provider_replay_parts(session_id, user_id, capability_key_digest, response_chain_id)` 可读。该查询
必须校验 session ownership 与全部 binding；即使 dialect 相同，endpoint/account/API
version/model/beta headers 任一不同也不重放。processor 为每个新 public/internal part
分配同一 assistant message 内单调 `stream_seq`，public ToolPart 也持久该字段，重建时按
`(message order, stream_seq)` 恢复全序。不提供可被 REST 调用的通用 internal 开关。
fork 不复制 internal row；provider binding 变化时不读旧 row；compaction 不把 opaque 内容
写进 summary，只按 adapter 合约保留尚未完成 call/result 配对所必需的最小内部块。
非 DB storage fallback 使用独立 internal namespace 并执行相同 binding/隔离/fork-drop，不得
成为绕过路径。

### 4.8 Reveal state 与生命周期

在 `db.models.session.Session` 新增确定字段 `tool_exposure_state: JSONType`，`nullable=False`、
server default `{}`；同一 Alembic revision 建立 §4.7 的独立 `InternalPartORM` 表。先 expand：
加 nullable/default、创建表与索引、回填旧 session，再收紧 non-null；writer feature flag 在全部
实例部署了新 reader 前保持关闭。
`session.session.Session` 增加 `Field(default_factory=dict, exclude=True)`，REST/SSE/前端 session
payload 均不序列化。内部结构固定为：

```json
{
  "v": 1,
  "next_origin_seq": 18,
  "agents": {
    "build": {
      "catalog_generation": "...",
      "revealed": {
        "web_search": {
          "schema_digest": "...",
          "last_used_at": "...",
          "origin_message_id": "msg_...",
          "origin_part_id": "part_...",
          "origin_seq": 17
        }
      }
    }
  },
  "provider_fallback": {
    "full_capability_key_digest": {
      "mode": "portable",
      "failed_at": "...",
      "expires_at": "...",
      "probe_generation": "...",
      "config_generation": "..."
    }
  }
}
```

规则：

- 当前 run 内使用内存 set-union，数据库写失败也不让下一 step 忘记；
- 每次经验证的 portable/native reveal 通过唯一 `commit_tool_reveal()` 提交：同一数据库
  事务先对 session row `SELECT ... FOR UPDATE`，分配/递增 `next_origin_seq`，幂等插入
  `ToolRevealEventPart` internal row，再对投影做 set-union/LRU/version 校验并更新 session。
  事务全成功或全回滚，不允许 event-only/state-only；不复用 `update_session` 的
  last-write-wins。内部 event 包含 agent、canonical tool ID、schema/catalogue generation、
  evidence source/origin message/part 和 sequence；JSON 只是这些存活事件的可重建投影；
- session 加载发现投影 version/序号与 internal event 不一致时，在锁内从事件重建并告警；
- 每 agent 最多保存 8 个非 pinned revealed IDs，默认 TTL 30 分钟，LRU 淘汰；
- product-state pinned pack 每 step 由真实状态重算，不靠 JSON 永久钉住；
- schema digest、agent allowlist、permission、registry/MCP generation 变化立即失效；
- agent 切换隔离，child session 初始为空；
- 所有 reveal/branch mutation 固定同一锁序：先 `SELECT ... FOR UPDATE` 锁 session row，
  再按稳定主键顺序锁/删 public message/part 与 internal event，最后在同一事务中更新
  JSON 投影；任何路径不得反向先锁 message/event 再等 session。regenerate/delete branch
  删除目标及后续 message 时，必须取得这把 session row 锁，再删掉其内部 reveal event、
  从存活 event 重建投影；禁止与并发 `commit_tool_reveal()` 产生丢更新或复活已删 reveal。
  fork 不复制
  reveal/internal-provider part，子 session 只从真实 product state 重算 pinned pack；
- provider opaque reference 只落 API-hidden internal part，不进 state JSON；切 provider 后不读它，
  只用语义 ID 重建目标 dialect；
- fallback 记录使用完整 capability-key digest 并必须包含 `failed_at/expires_at`、probe/
  config generation；过期才允许一次新 probe，配置变化立即失效，不得永久 sticky
  或每 step 重试；
- 旧 session 视为空；功能回滚只关 writer/转 portable 并保留新表，不立即做 schema
  downgrade。真正 downgrade 前必须停止新写、等待无 in-flight native response，把已闭合
  chain 投影为 provider-neutral 历史并清空 exposure state；存在活跃/无法转换 internal row
  时 preflight 必须拒绝 downgrade，不得直接 drop table 丢失唯一隔离/重放信息。

### 4.9 Prompt 与工具集合必须同源

当前 `system.py` 无条件要求模型使用 todo、web、cron、computer、browser 等。如果只隐藏
schema，模型会调用不存在的工具或用 Bash 模拟。

改造要求：

- `_build_system_prompt` 接收 `ExposurePlan` / `visible_tool_ids`；
- 将工具特定规则拆为稳定顺序的 prompt fragments，并声明 `requires_tools`；
- 只有对应工具 materialized 时才注入其详细规则；
- discovery slot 常驻时加入 adapter-specific 规则：portable 说“能力未显示时先
  `capability_search`”，native 改为 provider server-search 的官方名称；两者都补上
  “不要用 Bash 模拟需要审批、付费或专用 UI 的能力”；
- URL、视频、浏览器等预加载决定与 prompt fragment 使用同一 plan；
- 单测只做必要的单向断言：prompt 中每个“必须调用”的工具名都位于**初始 direct_ids**；
  不要求每个可见工具都在 prompt 重复出现，也不把 native deferred catalogue 当 direct；
- 保持 core fragment 在前、intent fragment 稳定追加，保护 prompt cache。

native 同响应 reveal 后可能立刻调用，来不及等下一 step 注入 conditional prompt。因此所有
会影响权限、付费、不可逆副作用、幂等和顺序的约束必须保留在 schema description、validator、
ToolHooks 或 backend service；只能把教学与低风险流程散文移到 conditional prompt。若某工具
仍依赖只存在于 prompt fragment 的安全规则，则该工具禁止 same-response native execution，
reference 后结束当前响应，下一 step 注入 fragment 后再调用。

---

## 5. 可机测预算规范

### 5.1 唯一合法序列化器，三种不同指标

把测量逻辑抽成生产代码纯函数（建议 `agent/tool_payload.py`），两条 provider adapter 与
测试共用，禁止测试复制一份近似 serializer。同一 serializer 必须分别报告：

| 指标 | 含义 | 用途 |
|---|---|---|
| `catalogue_wire_definition_chars` | HTTP request 中上传的全部 definitions，含 native deferred | 传输/provider limit/异常目录增长 |
| `initial_model_visible_definition_chars` | 请求开始时模型实际看见的 non-deferred/direct definitions | 核心上下文与首轮选择质量硬门 |
| `revealed_model_visible_definition_chars` | 当前响应/历史经 reference 展开的新增 definitions | active context 与单次 reveal 硬门 |

portable 模式下 wire 与 model-visible 基本相同；Anthropic/OpenAI native 可能在线上传输完整
catalogue，但只让 direct/revealed definition 进入模型上下文。不得从 wire 中剔除 deferred 后
仍把指标命名为 wire，也不得用较小的 materialized 值冒充网络 payload。

临时复测命令：

```bash
cd backend && uv run python - <<'PY'
import asyncio, json
from agent.agent import AGENTS
from agent.llm import _tool_parameters_schema
from tool.registry import register_builtin_tools, get_tools_for_agent
from tool.skill_tool import build_skill_tool_with_listing

register_builtin_tools()

async def main():
    tools = get_tools_for_agent(AGENTS["build"].tools)
    tools["skill"] = await build_skill_tool_with_listing(None, [])
    responses = []
    litellm = []
    for name, tool in tools.items():
        params = _tool_parameters_schema(tool)
        responses.append({"type": "function", "name": name,
                          "description": tool.description, "parameters": params})
        litellm.append({"type": "function", "function": {
            "name": name, "description": tool.description, "parameters": params}})
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    print("responses", len(compact(responses)))
    print("litellm", len(compact(litellm)))

asyncio.run(main())
PY
```

CI 同时记录 chars 和 tokenizer proxy；硬门槛使用 chars，避免 tokenizer 升级造成无关漂移。
生产日志再记录 provider 返回的 input/cache tokens，但不能从总 input 中假装精确拆出工具费。
native 上线前必须用固定 messages、冷 cache 的 legacy-eager/deferred A/B 验证 provider input token
确实下降；“字段被接受”不等于模型侧真的延迟。

### 5.2 硬预算

| 项目 | soft cap | hard cap |
|---|---:|---:|
| 初始 model-visible resident core | 20,000 chars | 24,000 chars |
| model-visible core + 当前一个主要 intent pack/reveal | 28,000 chars | 32,000 chars |
| portable wire definitions | 28,000 chars | 32,000 chars |
| native catalogue wire definitions | 96,000 chars | 128,000 chars；超限改走 portable/meta |
| 单个 tool item | 2,500 chars | 5,000 chars |
| 单个 intent pack | 10,000 chars | 12,000 chars |
| capability_search 单次结果 | 1,500 chars | 2,000 chars |
| 单 step 所有 discovery 结果合计 | 1,500 chars | 2,000 chars |
| 单 step 新增 unique reveal IDs | 3 个 | 5 个 |
| 动态 skill listing 全部内容（含 names-only） | 6,000 chars | 8,000 chars |

24K/32K 是 `portable`/`native_auto` 正常 exposure plan 的 model-visible 硬门，MCP、
synthetic structured output、动态 skill 描述都必须计入。PR#0/PR#2 为保持线上行为等价
使用的 `legacy_eager`/`shadow` 明确是迁移例外：只测量并高优告警超过 32K，不因
现有 56.5K 基线拒绝请求；它们及 `emergency_eager` 只受 128K
`catalogue_wire_definition_chars` provider ceiling。这个例外不能被解释为新功能可以
绕过 24K/32K。不能给 MCP 建“独立预算”后与 native 两边各自达标、合起来超标。

预算器超限时必须保留语义，不是对序列化 JSON 做 slice：

- resident core 与 synthetic structured-output 工具永不丢；
- 已有 paid job/production 的 status、cancel、reconcile/recovery 工具高于新任务 pack；
- 用户当前明确 pack 高于历史 reveal；被挤出 materialized set 的 eligible 项仍可
  discoverable，不得从 catalogue 删除；
- native catalogue wire >128K 时在发请前整体切 portable/meta，不截断 definitions；
- 平台管理的 built-in/custom schema 单项 >5K 使 CI/配置校验失败，先精简后部署；
  不可控的动态 MCP 单项 >5K 时保留原 schema 和可发现性，走权限过滤后的
  `mcp_call_tool` meta 路径，不修剪参数约束。

对抗性测试同时构造 core + active paid recovery + explicit pack + historical reveal >32K、
native catalogue >128K 和单项 >5K，断言上述优先级、fallback 与 discoverability，并断言
所有 source ToolInfo/raw_schema 字节不变。

目标 proxy token：resident core ≤6K，core + 一个包 ≤8K。provider 实际 tokenizer 可能不同，
只用于趋势，不替代 chars hard cap。

### 5.3 Description 瘦身规则

保留：

- 工具何时使用的一句判别；
- 不显然且会造成不可恢复后果的约束；
- 参数不能表达的幂等、顺序、安全语义；
- 返回值中调用方必须回传的 ID/version/key。

删除或迁移：

- 大段正反例；
- 与 system prompt 重复的流程；
- 服务端 schema/validator 已精确表达的枚举、required、长度说明；
- Git/PR、完整视频制作、浏览器操作等低频流程散文，移到 conditional prompt fragment 或
  Skill/reference；
- 宣传式语句和重复强调。

任何从 description 删除的安全约束，必须证明已在参数 validator、permission、服务端状态机
或 conditional prompt 中存在。不能为了预算删除唯一防线。

### 5.4 CI 输出

预算失败必须输出：

- provider dialect；
- materialized tool IDs 与来源（core/pack/revealed/MCP/synthetic）；
- 每项 chars、description chars、schema chars；
- 总量、cap、增长量；
- 最大五项。

测试不能只断言“≤ cap”；同时锚定每个 permanent 工具 schema 非空、required/action enum/
关键安全边界存在，防止把 schema 清空后假绿。

---

## 6. 不动区与非目标

| 区域 | 本轮要求 |
|---|---|
| `skill_only` / `activated_tools` | 永不恢复；grep 继续零命中 |
| `allowed-tools` | 纯文档；不得进入 planner、state、search |
| permission 系统 | 保留语义，只允许为“过滤目录”和“执行重验”接线 |
| 视频/图片业务状态机 | 不改变审批、花费、幂等、provider route 和恢复逻辑 |
| MCP 执行协议 | PR#2 先做无歧义 ID、discovery evidence 与底层 permission 闭包的安全修复；PR#5 才改预算/目录拓扑 |
| 前端（frontend-v2）/移动端（mobile） | 第一阶段零运行时代码改动；诊断 UI 进 Backlog。`frontend/`（v1）是遗留迁移参考，任何 diff 触碰它即错误 |
| 沙箱形态 | 执行面唯一生产形态是无影云（`SANDBOX_PROVIDER=wuying`）。docker/k8s provider 代码与 `k8s/` 部署文件为遗留冻结区：不修改、不删除、不为其新增 exposure 分支、不作为验收环境。无影拓扑（action server、隧道、relay、bootstrap）只允许 §12.4 明确列出的目录接口增量，bash 仍在无影桌面执行 |
| 凭据 | 不进入 schema、目录、日志、Skill 或测试 fixture |
| Skill 内容提示注入 | 仍是独立安全课题，见 Backlog |
| learned router | 第一版不用额外模型/embedding 服务 |

隐藏 `bash` 或删除专用工具不是本轮目标。Bash 是 build 的执行底座，但隐藏 schema 从来不是
安全边界；命令、网络和文件系统安全继续由 sandbox/permission 负责。

---

## 7. PR#0：测量与配置（零工具暴露变化）

### 7.1 代码手术

1. 新建统一 payload serializer/meter，Responses 与 LiteLLM 调用同一 schema normalization；
2. 在 `core/config.py` 固定 `ToolExposureConfig`，作为 `OpenBoxConfig.tool_exposure`，来源是
   `openbox.json`/环境配置合并链；字段至少包括：`mode`、resident/active/native-wire budgets、
   `reveal_ttl_seconds=1800`、`max_persisted_reveals=8`、
   `max_search_calls_per_step=2`、`max_reveals_per_step=5`、
   `max_search_result_chars_per_step=2000`、native endpoint/model allowlist、
   `allow_emergency_eager=false`。默认 `legacy_eager`；
3. 每请求记录工具数、三种 definition chars、proxy tokens、来源分布、最大项；
4. 用实际 serialized chars 替换 `loop.py` 的 `len(tools) * 400`；
5. 只对 `skill_tool.render_listing()` **最终完整输出**做精确 meter 与 8K 超限告警，
   不在这一 PR 截断 description/names-only，不让原本可见的尾部 Skill 暂时丢失。
   硬上限与独立 `skill_search` 必须在 PR#5 原子落地；
6. 日志不记录参数值、signed URL、credentials 或完整 MCP output；
7. metric labels 不使用 user/session/tool description 等高基数字段。

### 7.2 测试

- 两条 provider payload 捕获测试继续调用相同 meter；
- 同一 ToolInfo 连续测量两次结果相同、源 raw_schema 不变；
- 动态 skill listing 和 synthetic structured output 被计入；
- per-tool breakdown 相加等于总 payload；
- 1/250/1,000 个技能及超长恶意 description/name 的完整 listing 测量精确；超 8K
  产生高优告警但不删项，最后一项仍在 legacy wire 中；
- 指标异常绝不阻断 LLM 请求。

### 7.3 DoD

```bash
cd backend
uv run pytest -q
```

- 全量绿；
- `legacy_eager` 时工具暴露与 browser 行为零变化；
  `mode=shadow/portable/native_auto/emergency_eager` 在本 PR 配置
  校验阶段明确拒绝启动，直到对应实现落地；
- `legacy_eager` 超过 32K 只产生带分项的高优告警，不改变基线请求；超过 128K
  provider ceiling 才 fail closed；
- 生产/本地日志可看到真实 payload chars，不再出现固定 400/token 估算；
- 记录 `d453f78` 基线并生成最大项表。

---

## 8. PR#1：高收益 description 瘦身

### 8.1 优先顺序与目标

| 工具 | 当前 | 目标 |
|---|---:|---:|
| `todo_write` | 11,478 | ≤2,500 chars |
| `task` | 4,394 | ≤2,500 chars |
| `bash` | 4,137 | ≤2,500 chars |
| `computer` | 5,271 | ≤5,000，争取 ≤4,000 |
| `batch` | 1,623 | 删除宣传/完整示例，保留并行与禁止项 |

`todo_write` 的示例与 system prompt 重复最多，单项预计可节省约 9K chars；收益大于延迟整个
媒体包。先完成这一步，才能判断真正需要的 deferred 复杂度。

### 8.2 约束迁移

- 当前 todo service 允许多个 `in_progress`，不能假定已有硬约束。PR#1 要么先在 service 层
  实现并测试“至多一个 in_progress”等真实不变量，要么把对应短规则保留在 schema/prompt；
  在硬约束落地前不得仅为预算删除唯一说明；
- bash 的危险操作与 git 行为，若是平台规则移到稳定 system fragment，若是安全规则下沉到
  permission/sandbox；
- task 只保留 agent 选择、输入要求、子会话隔离和结果语义；
- computer 保留坐标/状态/批处理不可由参数表达的关键契约，其余操作说明由 action schema
  与 browser conditional prompt 承担。

### 8.3 测试与变异

- 对工具关键 description 只锚定不可缺的安全短句，不锚整段散文；
- schema properties/required/enums 全部保留；
- 暂时删除幂等/USER_NOTE/parallel safety 等唯一约束时，目标测试必须变红；
- budget 输出证明不是靠清空 description/schema 过关；
- 全量测试与通用编码浏览器 smoke 通过。

### 8.4 DoD

- 四个目标工具达标；
- 30 工具全量 payload 明显低于 §0.5 基线并记录差值；
- resident core 候选低于 20K chars；
- `git diff --check`、全量 pytest 通过。

---

## 9. PR#2：资格目录、物化计划与执行集合分层

### 9.1 先重构，仍 legacy eager

这一 PR 不启用 discovery。它只消除“一份 tools dict 代表一切”的结构风险：

1. 保留当前 `resolve_step_tools()` 结果作为 full eligible catalogue；可在调用点稳定后重命名
   `resolve_eligible_tools`，但不要同时改变 permission 顺序；
2. 建立 CatalogEntry / EligibleCatalog / ExposurePlan（含 `plane` 字段与其构造校验，
   §4.1/§2.7：native/host custom 为 platform，全部沙箱 MCP 为 sandbox）；
3. legacy-eager planner 的 direct_ids = eligible IDs，保证行为不变；
4. processor 分开接收 eligible catalogue、provider plan、execution lookup 与 executable IDs；
5. `ctx.available_tools` 只从 executable IDs 构造；
6. structured output 在 planner 后作为**本 step 瞬时 synthetic tool**同时加入 `execution_lookup`、
   provider direct plan 和 `step_executable_ids`，并计入预算；它不进 global/persisted
   catalogue、discovery index 或 reveal state，step 结束即丢弃；
7. `_repair_tool_name()` 只接收 `step_executable_ids` 对应的 ToolInfo；调用名验证可执行后
   才允许从 `execution_lookup` 取函数。精确名、大小写修复与未来任何 fuzzy repair
   都服从同一 executable 集合；
8. prompt builder 接收 visible IDs，但 legacy-eager 下输出与旧版等价；
9. 本 PR 实现 `shadow`：计算完整 ExposurePlan 与指标但仍按 legacy-eager 发送，
   PR#0 不重复建 planner；
10. **先消除 MCP ID 歧义并限制长度**：对长度前缀编码的
    `(stable sandbox/server identity, raw tool name)` 计算 SHA-256，再用无 padding 的小写
    Base32 生成固定 52 字符摘要；canonical ID 精确为
    `mcp:v2:<52-char-digest>`（总长 59，始终小于当前 permission subject 的
    `VARCHAR(128)`）。原始 server identity/raw tool name 不塞进 permission subject，
    而在当前 catalogue 的 server-side 双向映射中无损保留；构建时若同一 digest 对应
    不同原始 tuple，相关项全部 fail closed 并告警。catalogue、配置解析与 permission/
    approval 持久化边界都拒绝未知版本或超过 128 字符的 canonical subject，不能依赖
    数据库截断。provider-visible 名称清洗/截断后，旧 sanitized 名唯一时保留，只有
    碰撞组每项加稳定 hash suffix，并建立单值双向映射。跨 server 同名、64 字符
    截断碰撞、10K 字符恶意名字和恶意同名必须在 permission/index 前解决，禁止
    `dict.update()` 静默覆盖；
11. **兼容编译旧 MCP permission/approval**：为每项保留 `legacy_sanitized_id` alias，在每次
    catalogue generation 后把旧 ruleset 按原顺序编译到 canonical subjects。唯一 alias 无损映射；
    wildcard rule 保留它原有的广泛意图；碰撞 alias 上的 legacy deny 对全部候选 fail
    closed，legacy exact allow/always-allow 不能授权任何候选，要求管理员重新以
    bounded versioned canonical ID 明确批准。已持久 approval 只在 alias 唯一时迁移；歧义项
    标 stale/需重新批准。新格式使用显式 identity version，不静默改写用户配置；
12. **持久 tool 双身份**：在 ToolPart JSON 中保留旧 `tool` 展示字段，新增 API-hidden
    `canonical_tool_id`、`wire_tool_name`、`provider_binding_digest`、`provider_dialect`、
    `stream_seq`。adapter 使用当次请求的不可变双向映射先把 wire name 解析为 canonical ID；
    同 binding replay 使用原 wire name，切 endpoint/model/account/dialect 后仅由 canonical ID 映射
    目标 provider 当前名。旧 ToolPart 只在 `tool` 对当前/历史 alias 映射唯一时懒回填；
    歧义历史 fail closed、不重放、不执行，要求用户从该 call 前 regenerate。安全 lookup
    永远不使用旧 `tool`/wire name 直接授权；REST/SSE 只保留现有展示字段，不暴露
    binding digest/dialect/内部 canonical alias map；
13. **提前修复 MCP meta 权限闭包**：`create_mcp_tools` 在建立 direct/meta 索引前按上述
    canonical ID 应用 whole-tool rules；`mcp_find_tool` 只索引过滤后的列表，并生成绑定
    user/account、session、agent、sandbox/run、catalogue generation、schema digest 和 TTL 的服务端
    内部 discovery evidence（不向模型发 bearer token）；`mcp_call_tool` 只接受已由
    find/planner 正式 reveal 的 exact canonical ID，执行前再次调用 `ctx._authorize_tool`
    检查底层 ID 与真实 arguments。模型仅猜中 hidden canonical/provider 名不能 call；
    meta 外壳自身的权限检查也保留。PR#3 rollout 不得带着旧旁路启动。

### 9.2 必须新增的测试

建议新建 `tests/unit/test_tool_exposure.py`：

- catalogue 不修改 ToolInfo/raw_schema；
- stable ordering；
- eligible 包含 build 八工具，其他 agent 仍不含；
- denied 工具不在 CatalogEntry；
- 全部沙箱 MCP 条目 `plane == "sandbox"`；构造 `plane="sandbox"` 且 `pack` 非空或
  `same_response_safe=True` 的条目在构造期 fail closed，错误信息不含远端 description；
- legacy-eager plan 的 provider/executable IDs 与旧 resolver 完全相同；
- Batch 精确猜一个 eligible 但未 executable 的名字会失败；
- 顶层模型在没有 reveal 时 exact 猜 hidden 名、用不同大小写或近似名调用均失败，executor
  计数为 0，错误不得列出 full eligible；
- structured output 始终同时出现在当次 executor lookup/provider direct/executable 三集合，
  可执行一次，不进下一 step 或持久 reveal；
- custom agent `tools: []` 仍为空；
- config-defined custom agent 省略 `tools`、使用默认 `mode=all` 时可被 Task 启动，但
  `BUILD_ONLY_WORKFLOW_TOOLS` 全集与 `plan_exit` 在 eligible/discoverable/materialized/
  executable 四层都不存在；exact/case/fuzzy/Batch 猜名均失败；
- custom agent 显式 opt-in 的无副作用工具正常进入它自己的 catalogue，证明上一条
  不是靠禁用 custom tools 假绿；
- skill 四来源（host project/global、沙箱 builtin/container）加载前后 planner
  输入输出完全相同。
- 构造 >40 个 MCP：denied canonical tool 的 exact search 返回 0，直接/模糊 call 均拒绝；
  未经 find/planner reveal 的 allowed tool 即使猜中 exact 名也不能 call；经 reveal 后可 call，
  且底层 authorize callback 收到 canonical ID 与参数；MCP refresh/generation 变化使旧
  discovery evidence 立即失效。
- 构造跨 server 同名、超 64 字符共同前缀和 sanitized-name 碰撞：provider name 唯一且
  稳定，always-allow A 不能授权 B，deny B 不能隐藏 A，所有 permission/audit 记录
  使用 59 字符 canonical-v2 ID；当前 catalogue 从该 ID 精确映射回原始 tuple。
- 构造 10K 字符 raw server/tool 名、未知 canonical 版本、129 字符 permission subject 与
  人工 digest collision：前者仍只产生固定 59 字符 ID 且 approval 可持久化；后三者在
  写数据库前 fail closed，不能截断、覆盖或抛未处理的数据库长度错误。
- 用 `d453f78` 前版格式的 permission rules/已持久 approval 做升级 fixture：唯一旧 alias
  的 deny/allow 无损保留；碰撞 alias 的 deny 限制所有候选，碰撞 allow/always-allow
  不授权任何候选；只有显式 canonical-v2 approval 可解锁指定项。
- ToolPart 新行保存 canonical/wire/binding/dialect/stream_seq：同 binding replay 原名，provider
  switch 从 canonical 重建新名；旧行唯一 alias 懒回填，碰撞旧行不重放且不执行。
- ToolPart 的 REST/SSE 快照仍只含现有 UI 展示字段，canonical/binding/dialect/
  stream-sequencing 内部字段均不对前端序列化。
- MCP discovery evidence 不能从另一 session/account/agent/sandbox/run/fork 重放，任一 scope
  或 TTL/generation/digest 不匹配时 executor 计数为 0；同 scope 内经合法 find 的正向路径仍通。

### 9.3 Mutation checks

1. 把 catalogue 建立移到 `strip_denied` 前 → denied catalogue test 必须红；
2. 把 `ctx.available_tools` 改为 eligible IDs → Batch escape test 必须红；
3. 把 `_repair_tool_name` 的 lookup 改回 eligible → 顶层 hidden-call 测试必须红；
4. 把 MCP meta 索引恢复为未过滤列表、删掉底层 authorize 或取消 discovery
   evidence 校验 → MCP 负向测试必须红；
5. 取消 MCP provider-name hash suffix/双向唯一检查，或让 permission 按 provider name 缓存
   → 碰撞 + always-allow/deny 测试必须红；
   把 raw identity 直接拼入 canonical subject、取消 128 字符边界校验或忽略 digest
   collision → 超长名称/持久化/fail-closed 测试必须红；
6. 删掉 legacy alias permission compiler，或把歧义旧 allow/approval 映射给全部候选 →
   previous-release 升级测试必须红；
7. ToolPart 只存 wire name，或 provider switch 不经 canonical remap → 历史重放/歧义回填
   测试必须红；
8. discovery evidence 删掉 session/account/agent/sandbox/run 任一 scope → 跨边界 replay
   executor-zero 测试必须红；
9. 把 skill allowed_tools 接进 pack → 现有四来源反向锚点必须红；
10. 把任一沙箱 MCP 条目改标 `plane="platform"`，或删除 `plane` 构造校验 →
    plane 边界与 pack 隔离测试必须红。

### 9.4 DoD

- `mode=legacy_eager` 的 provider wire 与 `d453f78` 等价；MCP 仅收紧此前缺失的安全边界；
- `mode=shadow` 计算 planner 但 provider wire 仍与 legacy-eager 相同；
- 上述 `legacy_eager`/`shadow` 可以超过 32K 但必须告警，且
  `catalogue_wire_definition_chars` 不得超过 128K；
- 全量 930+ 测试绿；
- eligible catalogue/provider plan/execution lookup/executable IDs 四个对象从类型和调用
  签名上可区分；
- denied MCP 无搜索/调用旁路；
- 不新增 DB migration，不启用 schema 延迟的用户可见行为。

---

## 10. PR#3：Portable discovery、intent packs 与持久状态

### 10.1 实施顺序

1. 注册 `capability_search`，只在有 deferred eligible 项时常驻；
2. 实现 deterministic router 与 §4.4 pack 表；
3. 实现预算器；
4. 实现 discovery 专用 callback/typed outcome → state reducer → 下一 step materialize；普通
   ToolResult metadata 不得接入；
5. 同一 Alembic revision 增加确定的 `Session.tool_exposure_state` JSONB 和
   独立 `InternalPartORM` 表、回填/收紧 default，实现内部 part 专用读写通道与
   event + state 同事务原子更新；
6. 实现 `ExposureSignals` collector（零隧道，铁律 12）；
7. 条件化 system prompt；
8. feature flag 对 build 开启 `portable`，其余 agent 先 shadow；
9. MCP 继续使用 PR#2 已完成底层过滤/二次授权的 direct/meta 执行器并纳入总预算；禁止把
   权限修复推迟到 PR#5；
10. provider 支持 prompt cache key 时传递稳定的 session-scoped salted digest；不发送原始
    user/session ID，也不用跨租户共享的字面量 `"default"`。

### 10.2 Portable 时序

```text
step N request: core + relevant deterministic packs + capability_search
  → model calls capability_search("...")
  → backend searches eligible catalogue, returns top ≤5 IDs
  → processor atomically records reveal state
step N+1:
  → re-resolve agent + MCP + permission
  → validate schema digest / TTL / agent
  → append revealed typed definitions
  → model calls real tool
  → execute with ToolHooks and business gates
```

明显视频、图片、URL、浏览器和 cron 意图不走上面的额外 search；首轮直接加载 pack。

### 10.3 状态测试

新增 `test_tool_exposure_state.py` / loop integration：

- search 在 step 1，typed tool 在 step 2，step 3 仍保留；
- 重复 reveal 幂等；LLM retry 不丢也不重复；
- 新用户 turn、compaction、后端重启后仍按规则恢复；
- regenerate/delete 回滚产生 reveal 的消息分支后，JSON 投影从存活 internal event 重建，
  被删分支的工具不再 materialize；fork 不复制 internal event/state；
- 用两个真实事务与 barrier 覆盖 delete-vs-reveal 两种交错：两条路径都先取得同一 session
  row lock，再按稳定主键顺序处理 message/part/event；提交后投影必须精确等于存活事件，
  不死锁、不丢并发 reveal，也不复活已删除分支的 reveal；
- TTL/LRU 生效，不会逐轮累积到 30 工具；
- agent switch 隔离，切回重新校验；child session 不继承；
- schema digest、registry generation、MCP refresh、permission tightening 立即剔除；
- 数据库写失败保留本 run 内存 state 并告警；
- 两个并发更新 set-union 不丢失；
- 不把 provider opaque block 存入 JSON。
- `save_internal_part()` 只写 `InternalPartORM`、不发 SSE/event-bus，普通
  `get_messages()`/REST 不 join 该表；只有同时通过 session ownership 和完整 provider
  binding 检查的内部 replay 路径可读；
- 两个 session/account 的 prompt cache key 不同，同 session 内 core 前缀与 key 稳定；
- 从 migration 的 previous head 创建旧 session，再 upgrade 到 new head：旧行得到 `{}`、
  `InternalPartORM` 表存在且 Alembic 仍单 head；writer gate 关闭时旧/新 reader 行为不变。
  再验证安全 downgrade preflight：无活跃/无法转换 row 时可退，否则拒绝；
  REST/SSE payload 永远无内部 part；
- 在 `commit_tool_reveal()` 的插入前、插入后/投影前、commit 前做故障注入；重启后
  只能观察到 event + projection 全有或全无，两个并发提交的 `origin_seq` 唯一单调；
- skill、普通 built-in、custom/MCP 工具伪造 `metadata.revealed_ids` 全部无效；只有保留 ID 的
  capability_search typed outcome 可提交，且必须同时校验 generation、eligible subset、
  per-step 聚合上限；
- 并行/连续 2–N 次 search、重复 ID、超 5 ID/2K chars 均确定性截断，Batch 调 search 被拒绝；
- 用计数版 fake SandboxClient 断言：planner、预算器、`ExposureSignals` collector 与
  `capability_search` 检索全程沙箱 HTTP 调用数为 0（目录数据只来自本 step 已有的
  解析结果/投影）；工具执行路径不受此限制。

### 10.4 路由与 prompt 测试

- 普通代码请求：只有 core；
- URL：首轮 research；
- “每天上午 9 点提醒我……”：首请求含 automation/`cron`，对应 prompt fragment 与
  plan 同源；普通代码请求不含 `cron` 或其强制说明；
- 图片附件/生成：首轮 image/vision；
- 视频直达：首轮完整 video pack，无需 `skill("video-production")`；
- active video production 下“继续第三段”：即使文本无“视频”，仍加载 video pack；
- “只加载并总结 video-production skill，不执行视频”不加载 video pack；
- skill create/export：skill_admin + delivery；
- 多意图 union 在 32K hard cap 内；超限输出确定性裁剪理由；
- prompt 中所有强制调用的工具都在 initial direct_ids；只对声明了 conditional fragment 的
  pack 检查其片段，禁止要求每个可见工具都在 prompt 重复出现；
- model-specific editor 只出现一个。

### 10.5 DoD

- portable feature 下所有模型可工作，不依赖厂商原生特性；
- resident / active budget 全绿；
- 明确意图零额外 discovery step，模糊意图最多一个；
- permission/Batch/skill 负向测试全绿；
- browser §13.6–§13.13（A–H，含真实 portable discovery 成功路径）与 §13.15
  （Browser J 首跑：无影自装 MCP 分层与断连恢复）通过后才允许扩大 portable 灰度。

---

## 11. PR#4：Provider 原生 deferred adapters

### 11.1 OpenAI Responses

- 在 payload builder 中按官方当前协议添加 tool search 和 deferred definitions；
- core 不 deferred；discovery slot 只映射为 Responses server-search，wire 中不再发
  OpenBox `capability_search` function；
- 捕获真实 httpx JSON，断言 deferred 标记与预算计划一致；
- 用当前官方 schema/真实录制 fixture 锚定 Responses 原始 output item/SSE event 类型与顺序，
  再归一化 search、reference、target call、usage；不复用 Anthropic 原始类型名；
- normalized `tool_revealed` 必须先于 target execution；
- 复用 PR#3 已建立的独立 `InternalPartORM`/专用读写通道，新增
  `ProviderToolSearchPart` 并按原序保存 search/result/reference opaque blocks；同 provider
  replay 保留必要关联，API/SSE/event bus 都不把整个 internal part 交给前端；
- 同一 response 搜索并调用目标时不得额外 portable step。

### 11.2 Anthropic

- 优先做 direct wire adapter；若继续 LiteLLM，测试必须捕获转换后的真实 HTTP wire，而不是
  只 monkeypatch `litellm.acompletion` kwargs；
- 验证 `defer_loading`、tool search type、tool_reference 没被 `drop_params=True` 丢弃；
- discovery slot 只映射为 Anthropic server tool search，不同时发 OpenBox
  `capability_search` function；
- deferred definitions 不放 cache breakpoint；core 顺序稳定；
- 解析 `server_tool_use` / `tool_search_tool_result` / `tool_reference` / `tool_use`；
- 使用相同 API-hidden transcript part 保存并重放有序 opaque blocks；compaction 后只在 provider
  协议允许时保留最小关联，不能把引用顺序压成普通文本；
- request bytes 仍含 definitions，不得在指标中假装网络传输减少；模型可见 input 与 cache
  才是 native 收益口径。

### 11.3 Portable providers

参数化 contract test 至少覆盖：Gemini、Kimi/Moonshot、OpenAI Chat Completions 与未知代理：

- payload 中没有 unsupported native 字段；
- 第一次只有 core/pack/discovery，第二次才有 reveal schema；
- tool call/result ID 继续满足现有兼容测试；
- provider switch 可从语义 ID 重建，不重放 native opaque reference。

### 11.4 Fallback

- pre-stream feature-unsupported 4xx：恰好一次 native → portable；
- 普通 schema 400：返回错误，不偷偷切 `emergency_eager`；
- 首 event 后断流：不重放；
- capability failure 对完整 §3.6 capability key/session sticky，TTL 后才探测；
- 表驱动逐一变更 provider adapter、normalized endpoint、API version、model snapshot、
  account/credential、region、beta headers、config generation；每一维都必须 capability
  cache miss/重探测，不得复用另一 binding 的结果；
- fallback payload 仍只含 eligible，不泄漏 deny/outside-agent；
- catalogue stale 时刷新一次再报 not-found，不 fuzzy 授予；
- `emergency_eager` 只有 `allow_emergency_eager=true` 且运维显式切换时开启，
  且仅处理当前 eligible catalogue；built-in 可 direct，
  MCP 继续使用经过权限过滤的 meta/capped 模式，catalogue wire 不得突破 128K provider cap。

### 11.5 DoD

- 两个 native adapter 有 wire + stream + replay 三级契约测试；
- response-local 状态机测试覆盖合法 reveal→call、call-before-reveal、exact/case/近似 hidden
  call、多个 search/reference 聚合上限，非法路径 executor 计数为 0；
- fake clock 验证 fallback 在 `expires_at` 前不重 probe、到期后仅 probe 一次，probe/config
  generation 变化使旧记录立即失效；
- API-hidden transcript part 的保存、同 provider replay、provider switch、compaction 与
  fork-drop、REST/SSE/event-bus 排除测试全绿；
- 同 dialect 但 endpoint/account/API version/model/beta headers 任一不同时不读旧 opaque
  row；public ToolPart 与 internal row 按 `stream_seq` 重建的顺序与原 stream 逐项相同；
- `same_response_safe=False` 默认项在 reference 后同响应 call 仍拒绝，`True` 审计项
  才可同响应执行；false fixture 必须产生同 call ID 的 blocked result、历史重放
  闭合、无 `_noop`、executor 为 0，下一 step 新 call 才执行；Skill/MCP metadata 不能把
  false 改为 true；
- 生产 `SAME_RESPONSE_SAFE_TOOL_IDS_V1` 的 key 快照必须精确等于 §4.7 四项，并逐项
  验证表中安全门。枚举全部其他 registered 工具，尤其 paid/approval/不可逆/
  混合 action 工具以及会写 OSS/FileAsset/FilePart 的 `view_image`，reference 后同响应
  call 全部 executor=0；未同时增加审计证据就把
  任一 ID 加入 true snapshot 时测试必须红；
- 任一环节未覆盖就保持 portable 默认；
- paid-tool double execution 测试为零；
- 默认仍为 portable/native disabled；merge DoD 不依赖尚未部署的生产 canary 指标。

生产 native canary 与 browser 成功路径属于 §14 rollout exit criteria，不能作为合并前无法
取得的证据。

---

## 12. PR#5：动态 Skill/MCP 目录预算化

### 12.1 Skill listing

PR#0 只建立完整 listing meter/超限告警，没有删掉任何可见 Skill。本 PR 必须在
同一个原子发布中同时引入独立轻量 `skill_search` 和 8K hard cap：搜索没有通过全部
正向/权限测试前，不允许开启截断。

- 保留对最终渲染完整 listing 的 chars/tokens 计量，包括 XML 标签与所有名字；
- description 继续单项裁剪；
- 超出 hard cap 时在 listing 尾部固定告知“更多 Skill 请用 `skill_search`”，不保留
  无限 names-only 尾部；
- `skill_search` 是独立 built-in，参数只有 `query` 和可选 exact `name`；只搜索名称/
  短描述，返回最多 5 项/合计 2K chars，不加载正文也不触发工具；模型再用
  现有 `skill(name=...)` 加载选中正文；
- `skill_search` 作为 `skill` 的条件 companion 显式进入相同 AgentDef，只在过滤后 listing
  超 8K 时 resident 并计入 24K；没有 `skill` 的 agent 不得单独获得它；
- `skill_search` 在建索引前对每个 Skill 用现有 `skill` permission pattern 过滤，
  exact denied 也返回 0；结果只是知识发现，不进 tool exposure state；
- exact denied skill 不返回；
- 恶意超长名字/描述、HTML/XML 字符做转义并受预算；
- 250/1,000 skill 压力测试不超过 cap。

### 12.2 MCP

删除单一 `MCP_META_TOOL_THRESHOLD = 40` 决策，改为：

- catalogue discovery 与完整 schema wrapper 分离；
- 对 normalized schema 实测 chars；
- 高频、明确选中的 MCP tool 可 typed materialize；
- 大目录继续支持 find/call meta fallback；
- direct MCP definitions 与 native tools 共用 32K 总预算；
- 保留 PR#2 的 permission-before-index 与调用时底层二次授权，任何重构不得回退；
- MCP add/remove/connect/refresh 更新 catalogue generation，并使旧 reveal 失效；
- 保留 PR#2 已建立的有界 canonical-v2 ID、原始 tuple 无损 server-side 映射，以及带稳定
  hash suffix 的 provider-name 双向映射；
  目录重构不得恢复 `tools.update()` 静默覆盖；
- resource list 元数据和 resource body 分开；body 不得进入 catalogue hint、provider wire、
  搜索结果、预算明细或 telemetry，只有模型显式调用 `read_resource`、且通过该 resource
  的权限检查后，正文才能作为这一调用的 tool result 进入上下文。测试 resource 使用唯一
  secret sentinel 与超长正文，避免只靠正常短文本假绿。

### 12.3 Cache

catalogue cache key 至少：user/account、project、sandbox ID、MCP generation、registry/config
generation、region 与 credential digest（只存 hash，不存 secret）。
权限过滤在 cache 读出后执行，不能缓存授权结论。TTL + singleflight；连接失败不做长时间
negative cache。schema normalization 按 raw schema hash + dialect/version 缓存，copy-on-read。
用表驱动测试逐一改变 user/account、project、sandbox、MCP generation、registry/config
generation、region、credential digest；每个维度都必须 cache miss/重建，不同租户或
sandbox 的 catalogue 不得串用。另用 fake clock 与受控 loader 验证：同 key 的 N 个并发
请求只触发一次加载；TTL 到期后的 N 个并发请求也只重建一次；首次瞬时连接失败不会写入
长期 negative cache，下一次请求可立即恢复；schema normalization 的缓存结果始终
copy-on-read，按 Responses → LiteLLM 和 LiteLLM → Responses 两种顺序构建都不修改源
schema，也不让一个 dialect 的修改污染另一个 dialect。

无影云补充：cache key 还必须纳入 §12.4 的执行面 boot 身份（action server
`START_TIME` + hostname digest）与沙箱目录 generation——桌面重启或目录变化即失效。

### 12.4 沙箱目录投影（无影云双平面适配的核心工程）

现状是每 step 两次跨隧道全量拉取（`GET /mcp/tools`、`GET /skills`，§1.4）。本 PR 按
"执行面发布、控制面订阅"改造为缓存投影：

- `container/action_server.py` 为 `/skills` 与 `/mcp/tools` 增加目录 generation：对
  当前目录内容（技能名/文件 digest、MCP server 连接集合与工具列表 hash）计算稳定
  hash，经 `ETag` 返回并支持 `If-None-Match` → 304；另提供聚合
  `GET /catalog/version`（skills generation + mcp generation + `START_TIME`）。
  技能安装/删除、MCP add/remove/connect/disconnect、以及 server 端 `listChanged`
  通知（对齐 opencode 的事件驱动刷新，server 支持时）都必须使 generation 变化；
- backend 维持按 §12.3 cache key 隔离的投影缓存：每 step 至多一次条件请求（304 时
  目录零字节），TTL 内可完全不请求；`START_TIME` 变化视为桌面/action server 重启，
  强制全量刷新并 bump catalogue generation，旧 reveal 与 MCP discovery evidence 随
  §12.2 规则失效；
- 版本兼容：旧版 action server 响应无 `ETag` 头即判定不支持，退回现状全量拉取——
  不崩溃、不缓存错误 generation。本项改动必须用
  `python backend/scripts/wuying_deploy_action_server.py` 下发到桌面后才生效：
  后端热重载不会更新桌面（坑 23），验收前先核对 `/alive` 的 uptime 已重置；
- 投影只服务目录/规划；工具执行、skill 正文获取（`GET /skills/{name}`）仍实时打
  隧道，语义不变；
- 隧道断开时投影按最后一次成功快照继续服务 discovery（§2.1），执行期错误照常
  上抛；断连不写长期 negative cache。

### 12.5 DoD

- 1/39/40/41/200 个大小不同的 MCP 工具均按 bytes 而非数量做正确选择；
- 大目录 initial model-visible/portable wire 仍在 32K cap，native catalogue wire ≤128K；
- denied MCP 名称/描述/参数不泄漏；
- listing ≤8K 时 `skill_search` 不进 payload；1,000 个 Skill 超预算时它作为条件
  companion resident，列表外最后一项仍可 exact search → `skill(name=...)` 加载正文，
  denied exact 返回 0，且加载前后 tool exposure plan 完全相同；
- allowed 小 MCP 目录完成 search/reveal → typed schema → call，allowed 大目录/单项
  >5K schema 完成 find → generation-bound evidence → meta call；两者底层 authorize
  均收到有界 canonical ID 与真实参数，并能从当前 catalogue 无损解析到原始 tool tuple；
- MCP refresh 使旧 schema digest/evidence 失效，重新发现后可再调用；跨 server 同名与
  截断碰撞测试通过；
- 含唯一 secret sentinel 和超长 body 的 MCP resource fixture，在 list/catalogue/search/
  provider wire/log/metrics 中全部零命中；未授权 `read_resource` 不返回正文，只有显式
  授权读取的配对 tool result 含 sentinel，且仍受通用 output 截断预算；
- catalogue/schema cache 的 fake-clock、N 并发 singleflight、瞬时失败立即恢复、TTL 到期
  单次重建、跨 dialect 双向 copy-on-read 测试通过；
- 目录投影生效：同 generation 连续 step 的沙箱目录传输为 304/零目录字节，目录变化后
  恰好一次全量刷新；旧版 action server（无 ETag fixture）自动退回全量拉取且行为等价；
  `START_TIME` 变化触发投影、reveal 与 discovery evidence 联动失效；隧道断开期间
  discovery 仍用最后投影服务，执行报清晰错误，重连后已 revealed 工具无需重新发现；
- skill 目录无论多少项都严格受总预算；
- 全量与 Browser A–H 必过、Browser J 复跑必过；Browser I 只对已进入 native allowlist
  的 endpoint + model + account 组合必过，无 entitlement 不阻断 provider-neutral PR#5。

---

## 13. 全链路验收矩阵

### 13.1 静态与后端

```bash
cd backend
grep -rn "skill_only" --include='*.py' . | grep -v .venv
grep -rn "activated_tools\|activate_skill_tools" --include='*.py' . | grep -v .venv
uv run pytest -q
cd ..
BASE="$(git merge-base HEAD origin/main)"
git diff --check "$BASE"
git diff --name-only "$BASE"
git status --short
git ls-files --others --exclude-standard
```

前两个命令零命中。新增测试至少覆盖：

- `test_tool_exposure.py`
- `test_tool_exposure_state.py`（含 InternalPart 持久化）
- `test_capability_search.py`
- `test_prompt_visibility.py`
- `test_llm_tool_search_responses.py`
- `test_llm_tool_search_anthropic.py`
- `test_mcp_security.py`
- `test_tool_part_identity.py`
- `test_tool_exposure_migration.py`

保留现有：skill 来源不变式（现有三来源测试保留，PR#2 扩为四来源，§2.2）、agent
registry、自定义空白名单、schema 规范化、Batch 安全、call-id compatibility、视频
付费/审批/幂等测试。

每个 PR 都必须人工审查上述 diff/status/untracked 输出：本计划默认不改
`frontend-v2/src`、`mobile/lib`、遗留 `frontend/`（v1 迁移参考，任何触碰即错误）、
`k8s/` 与 docker/k8s sandbox provider、真实 `openbox.json` 或
`docs/LOCAL_CREDENTIALS.md`；`container/action_server.py` 只有 PR#5 §12.4 的目录
接口增量允许改动。若任务明确扩展到前端，还要运行 `npm run check --prefix
frontend-v2`（已包含 Vitest）；若明确修改移动端，运行 `cd mobile && dart
analyze`。本地凭据、测试用 signed URL 和真实 API key 不得出现在 diff 或 fixture。

### 13.2 Provider payload

每个场景同时捕获 Responses 与 LiteLLM portable wire：

- 初始 JSON 中 deferred tool 的 name、unique description、parameter marker 全部不存在；
- native JSON 只含 eligible definition，deferred 标记正确；分别断言 catalogue wire ≤128K、
  初始 model-visible ≤24K、active materialized ≤32K，不能混成一个指标；
- core/pack/reveal 顺序稳定；
- 预算总量和每项非空契约；
- 同一请求构建两次，source schema 不变；
- structured output 和历史 tool calls 不错误触发 `_noop`。
- schema normalization 分别按 Responses → LiteLLM、LiteLLM → Responses 顺序重复构建，
  provider payload、缓存副本与 source schema 互不污染。

### 13.3 权限与信任边界

- exact-name 搜索 denied/outside-agent/build-only 工具返回 0；
- 给 denied 工具放入唯一 name/description/parameter sentinel，捕获该请求全部 logs、
  metrics labels/attributes、budget breakdown 与 traces，所有 sentinel 均零命中；
- 先 reveal 后新增 deny，下一 step payload、available_tools 和 state 都消失；
- 参数级 deny 在执行时阻断；
- Batch 猜 hidden ID 失败；
- 顶层模型在没有 reveal 时 exact、大小写变体、近似名调用 hidden tool 全部失败，
  executor 计数为 0，错误不列出 full eligible；
- native reference 只开放被引用项，不开放整个 pack；
- Skill、普通 built-in、custom/MCP 伪造 `revealed_ids` metadata 均不改变 state；
- MCP discovery evidence 绑定 user/account/session/agent/sandbox/run/generation/digest/TTL，
  跨 session、account、agent、sandbox 或 fork 重放均被拒且 executor 为 0；
- MCP resource fixture 的 unique secret/超长 body 在 catalogue、search、provider wire、
  logs/metrics/traces 全部零命中；未授权读取 executor 为 0，只有授权后的配对
  `read_resource` tool result 可包含经截断的正文；
- 同一 step/response 多次 search/reference 合计仍受 2 次搜索、5 个 unique ID、2K
  result hard cap，不能分批枚举目录；
- 四来源（host project/global、沙箱 builtin/container）恶意 Skill 加载前后完整
  exposure plan 相同；
- custom child agent 省略 tools、默认 mode=all 且真正由 Task 启动时，无法发现/
  物化/执行 `BUILD_ONLY_WORKFLOW_TOOLS` 任一项或 `plan_exit`；exact/fuzzy/Batch 均失败。

### 13.4 高风险 Mutation 证据

每项变异都必须使指定测试变红，否则对应测试不足以作为安全证据：

1. 在 permission 过滤前建 catalogue/index → denied 搜索泄漏测试红；
2. portable search 后在同 step 立即开放工具 → N→N+1 时序与 Batch 逃逸测试红；
3. 每 step 清空 reveal state → 新 user turn、compaction、重启恢复测试红；
4. partial event 后仍自动 fallback/retry → executor-once / paid 幂等测试红；
5. meter 漏掉 synthetic structured output、动态 skill listing 或 MCP → payload 分项求和测试红；
   或在 permission 过滤前生成 tool-name/description metric/log → denied sentinel telemetry 测试红；
6. MCP meta index 恢复未过滤底层目录、取消 discovery evidence/碰撞映射，或 call
   取消底层 authorize → denied/guess/collision/always-allow MCP 测试红；
7. capability cache key 逐一删掉 adapter、endpoint、API version、model snapshot、account/
   credential、region、beta headers、config generation 任一维度 → binding 污染测试红；
8. catalogue cache key 逐一删掉 project、sandbox、MCP generation、region 或 registry generation
   → 表驱动隔离/失效测试红；
9. 删掉 native reference 的 eligible/digest/顺序验证，或先缓冲再重排 raw events →
   OpenAI/Anthropic raw-wire + stream 状态机测试红；
10. 不落 API-hidden opaque part、误发 SSE 或 fork 复制它 → replay/隔离测试红；
11. 把 `same_response_safe` 默认改 true，或读取 Skill/MCP metadata 覆盖它 →
    false-fixture executor-zero 测试红；把任一未审计/paid/混合 action ID 加入生产 true
    集合 → 快照与逐工具负向测试红；
12. 调换超预算优先级、对 JSON 直接 slice、删除 overflow 项的 discoverability，或让
    >128K native catalogue 继续发送 → core/synthetic/paid-recovery 保留与 fallback 测试红；
13. regenerate/delete 后不从存活 reveal event 重建 state、删除路径不先锁同一 session row，
    或把 message/event 与 session 的锁序反转 → 分支回滚与 delete-vs-reveal 并发测试红；
14. 删除 automation/cron router 或 conditional prompt fragment → “每天提醒”首请求/
    prompt 同源测试必须红；
15. 让 Skill 名称/正文或“只加载技能”文本触发 intent pack → 纯知识加载路由负例红；
16. 把 MCP resource body 混入 catalogue hint/search/provider wire/telemetry，或绕过
    `read_resource` 权限直接返回正文 → secret-sentinel 隔离测试红；
17. 删除 catalogue singleflight、永久缓存瞬时失败、TTL 后重复并发重建，或返回可共享
    修改的 schema cache 对象 → 并发计数、失败恢复、fake-clock 与跨 dialect source
    immutable 测试红；
18. 把任一 `plane="sandbox"` 条目放进 deterministic pack/resident core，或让远端
    description 未经清洗截断直接成为 discovery_hint → plane 边界与 hint 清洗测试红；
19. 隧道断开/健康检查失败时收缩 eligible、清空投影或清除 reveal state → 断连恢复
    测试（§10.3、Browser J 步骤 4）红。

### 13.5 Browser 环境与零费用前置

1. 确认 Docker 数据库 `openbox-postgres-1` 运行（docker 只剩本地依赖，没有 docker
   沙箱）；
2. 无影隧道就绪：`backend/scripts/wuying_tunnel.sh` 常驻运行，
   `curl http://127.0.0.1:18000/alive` 返回 `status:ok` 且 hostname 是目标桌面；
   `WUYING_API_KEY` 只存在于 gitignored 的 `backend/.env`，禁止出现在 diff/日志；
3. 后端用 `uv run --directory backend python scripts/backend_entrypoint.py --reload
   --host 0.0.0.0 --port 8080` 启动；
4. 前端用 `npm run dev --prefix frontend-v2` 启动，打开 `http://localhost:3000`；
5. 使用 `qa_jobs` 测试账号，密码只从 gitignored 的
   `docs/LOCAL_CREDENTIALS.md` 读取，禁止抄入日志、截图说明、文档或 commit；
6. fallback、断流、重试和 paid-action 默认用 loopback/fake provider 验证。除非当次
   得到新的用户确认，不向真实付费供应商提交生成任务；
7. 热重载盲区有两层：backend `--reload` 只看 `.py`（改 config/skill/locale 必须
   重启后端并核对新 PID）；桌面上的 action server 完全不随后端重启更新——凡改
   `container/*` 必须跑 `python backend/scripts/wuying_deploy_action_server.py`
   下发，并用 `/alive` 的 uptime 重置确认生效。桌面重启后 dev-browser 需重新
   Enable（涉浏览器自动化场景时）。

### 13.6 Browser A：通用编码只用 core

新会话请求一个不需要网页/媒体的真实代码修改。

验收：

- 首个 provider request 只有对应 agent core + synthetic 必需工具；
- 没有 media/browser/research/planning schema；
- 模型正常读、改、测；
- schema ≤24K chars，无 unknown tool。

### 13.7 Browser B：视频能力首轮直达

新会话，不提技能名：

> 帮我把一个想法做成竖屏短视频，先创建项目、写完整台词并发起剧本审批后停止。

验收：首轮 materialize video pack；可先自行加载教学 Skill，但不是获得工具的前置；
creator_context、完整台词、set_script、script approval 顺序正确；到审批停止，零付费。

### 13.8 Browser C：视频跨轮/重启续接

在 B 中追加“标题换成《延迟物化验收》”，再重启后端继续。

验收：active production 使 video pack 自动保留；无需重新搜索或重新加载 Skill；ID、审批与
幂等键来自 status，无 unknown tool、无重复项目。

### 13.9 Browser D：图片直达

不提 imagegen Skill，直接生成一张测试图片，再用 asset_id 做一次局部编辑。

验收：首轮 image pack；附件/OSS/resource centre 正常；不得重复 share。默认使用
loopback/fake image provider；未得到**本轮**用户明确确认时不得向真实付费 provider submit。

### 13.10 Browser E：Research、Browser 与 Automation

- 带 URL 的请求首轮 research；
- 登录/交互网页首轮 browser（可加载 dev-browser 教学，但工具资格不来自 Skill）；
- 用唯一 QA 标签创建一个短期测试提醒：首请求有 automation/`cron` 且 prompt 同源，
  list 确认后立即 delete；普通编码不得携带 cron schema；
- 普通编码会话不携带 computer 的 5K schema；
- 从 research 切换 browser 时 union 不超预算。

### 13.11 Browser F：恶意 Skill

容器安装声明 `allowed-tools: [bash, video_generate, plan_exit]` 的测试技能并加载。

验收：加载前后 eligible/discoverable/materialized/executable IDs 完全相同；输出和 metadata
无 activation；日志只记录忽略字段；plan agent 仍发现不了视频工具。验完删除测试技能。

### 13.12 Browser G：Provider fallback

用 fake/测试代理让 native 请求在 pre-stream 返回明确 unsupported 4xx。

验收：仅一次 portable 重试，UI 最多多一个 discovery step，工具只执行一次。再模拟首个
event 后断流：不得重放，不得产生双卡、双文件或双付费 job。

### 13.13 Browser H：Portable discovery 真实成功路径

这一场景不得由确定性 intent pack 直达替代，否则不能证明 discovery 本身可用：

1. 在本地 `backend/.openbox/tools/qa_deferred_echo.py`（后端运行 cwd 对应目录）注册一个
   只回传短文本、无网络/文件/付费副作用的 `qa_deferred_echo`；建立仅用于 QA 的
   config-defined agent，显式
   allowlist 只含 `capability_search` 与 `qa_deferred_echo`；
2. 新 session 请求“请找到能把文本原样回显的能力，并回显 `portable-ok`”，
   不说工具 ID；
3. 捕获第一个 provider wire：含 `capability_search`，但整个 JSON 中不得出现
   `qa_deferred_echo` 的名称、unique description 或参数 marker；
4. 验证 step N 搜索后，step N+1 才出现 typed echo schema 并执行一次；
5. 新 user turn 再回显 `persisted-ok`，不重搜；触发 compaction 后重试，再完整
   重启后端、打开同一 session 重试，都从持久 state 恢复且先重验权限；
6. 验收后删除 QA agent/custom tool/config 修改，它们不得进入生产 commit。

### 13.14 Browser I：Native search 同响应成功路径

仅在 endpoint + model + account 已完成真实 capability probe 时使用 Browser H 的无副作用
fixture 开 native canary。在 QA 平台审计表中显式把 echo 标记为
`same_response_safe=True`。新 session 的 wire 中只能有 provider-native search primitive，
不得同时出现 OpenBox function `capability_search`。原始事件名按对应 adapter 合约记录，
公共层必须严格按以下归一化顺序：

```text
search_started → search_result → tool_revealed → tool_call
```

目标 call 必须在同一 response 执行一次，call-before-reference 必须失败；同 provider
续接能重放 API-hidden opaque transcript，切换 provider 只用语义 reveal ID 重建。echo 目录
只用于证明协议和时序；它可能比 server-search primitive 更小，不对它设 token 节省门槛。
真实输入 token 收益只由 §14.3 的工具密集、冷 cache A/B 判定。
另用一个 `same_response_safe=False` 的无副作用 fixture 验证：reference 可持久，但同响应
call 收到同 ID blocked result、可原序重放、无 `_noop`，且 executor 为 0；下一 step
注入 conditional prompt 并重验后，模型使用新 call ID 才能执行。
如果测试账号没有原生能力，结论是“native rollout 被阻断，继续 portable”，不得用
mock 成功、字段被服务端接受或 Browser G fallback 代替这条真实证据。

### 13.15 Browser J：无影云用户自装 MCP 分层与断连恢复

验证“用户装进无影云的能力”走且只走 discovery 层——这是 §2.7 分层的端到端证据。
PR#3 后首跑（发现与断连语义），PR#5 后复跑（投影 generation 语义）。

1. 经技能中心/API 在无影桌面安装一个无副作用 MCP server（推荐 `skill/catalog.py`
   目录里的 `everything` 或 `memory`，或本地 echo server；需含名字唯一可辨识的
   工具）；
2. 新会话发一个普通编码请求：首轮 provider wire 与所有 deterministic pack 中不得
   出现该 MCP 任何工具的名称、unique description 或参数 marker；
3. 再请求“找到能〈该工具功能〉的能力并使用一次”（不说工具名）：模型经
   `capability_search`（大目录下为 `mcp_find_tool`）发现 → 下一 step typed/meta
   调用经隧道执行恰好一次；canonical ID 为 `mcp:v2:` 形态，authorize callback 收到
   底层 ID 与真实参数；
4. 停掉 `wuying_tunnel.sh` 后同会话再调用一次：执行返回清晰的沙箱不可达错误，
   目录/资格不缩水、无 fallback 重试造成的第二次执行；重启隧道后同一 reveal 直接
   可用，无需重新搜索；
5. PR#5 复跑时另验：TTL 内连续 step 沙箱目录传输为 304/零字节；卸载该 MCP server
   后 generation 变化、旧 reveal 失效、exact 搜索返回 0；
6. 对该 server 的一个工具加 deny 规则：目录、搜索、logs/metrics 全程零泄漏；
7. 验收后卸载测试 server 与 deny 规则，桌面不留残留配置。

### 13.16 真实日志核对

每个场景记录：

- exposure mode / strategy；
- visible tool count，以及 catalogue-wire / initial-visible / revealed-visible 三类 schema
  chars/proxy tokens；
- core/pack/revealed/MCP 来源；
- discovery query 数与结果数；
- native fallback；
- provider input/cache tokens、TTFT；
- unknown tool、permission deny、执行次数。

日志不得包含用户 prompt、工具参数、凭据、signed URL 或 Skill 正文。

---

## 14. 发布、观测与回滚

### 14.1 灰度顺序

1. `legacy_eager` + metrics：建立至少一周真实基线；
2. `shadow` 100%：比较计算出的 plan 与真实任务工具使用；
3. `portable` 仅 build 1%，且先排除 paid submit 场景；
4. 扩至 10%/50%/100%，再纳入 state-pinned 媒体流程；
5. OpenAI native 单独 canary；
6. Anthropic native 单独 canary；
7. 其他 provider 保持 portable，除非完成自己的协议验收。

### 14.2 核心指标

- `catalogue_wire_definition_chars`、`initial_model_visible_definition_chars`、
  `revealed_model_visible_definition_chars` / proxy tokens；
- provider `input_tokens` / `cached_tokens`；
- exposed count 与 pack source；
- discovery calls、search miss、平均额外 step；
- unknown tool、invalid tool、permission denied；
- task success / user retry / abandonment；
- TTFT、完整 turn latency；
- native fallback rate；
- paid submit/create 的幂等冲突与重复执行（目标永远为 0）。

第一阶段目标：普通请求 `initial_model_visible_definition_chars` 中位数至少下降
40%，成功率相对 legacy-eager 不下降超过 1 个百分点，
unknown-tool 不增加超过 0.1 个百分点；明确意图不增加 discovery step。

### 14.3 Native rollout exit criteria

对每个 endpoint + model snapshot + account/credential capability 组合单独验收，不得用某一
账号的成功为整个 provider 开启：

- §13.14 Browser I 的真实 search → reference → call 同响应路径通过；
- 固定 messages、冷 cache 的工具密集 fixture 中，provider-reported input tokens 中位数
  相对 legacy-eager 至少下降 20%；同时单独报告 cached tokens，不把 cache hit 当作 deferred
  收益；
- 与同模型 portable control 相比，task success 下降不超过 1 个百分点、unknown-tool
  增加不超过 0.1 个百分点、平均额外 step 不劣化；
- native fallback rate <1%，partial-stream replay 和重复执行均为 0；
- 连续 canary 期满足以上条件才扩大 allowlist。无 entitlement 或无法取得真实 usage
  证据时保持 portable，不得放宽门槛。

### 14.4 立即回滚条件

- 任一 denied/outside-agent 工具出现在目录或被执行；
- 任一 paid action 因 fallback/retry 重复；
- 子 agent 获得 build-only workflow tools；
- unknown-tool 增加 ≥0.5 个百分点；
- 成功率下降 ≥2 个百分点；
- provider adapter 出现无法重放的历史污染。

从 native 回滚时首选切到 portable，因为它保留短目录和 typed materialization。只有
portable 本身也故障时，且运维已显式设置 `allow_emergency_eager=true` 时才切
`emergency_eager`：eligible built-ins 可 direct，MCP 仍使用
权限过滤后的 meta/capped 模式，不发送原始全量 MCP definitions。整个请求的
`catalogue_wire_definition_chars` 仍不得超过 128K；如果 eligible built-ins 自身已
超限，必须 fail closed 并告警运维回退到已验证的上一版发布/缩小 catalogue，
不得再跳回已判定故障的 portable 路径，也不能静默截掉工具。
任何回滚都不回到 global registry，也不重新暴露 denied 项。已有 reveal JSON 保留但忽略，
避免紧急回滚时做破坏性数据迁移。

### 14.5 清理时机

portable 稳定两周且 Browser A–H+J 重复通过后，才删除 portable 对应的 shadow
compat 分支。某个 native adapter 还必须另外满足 §14.3 并通过 Browser I，才能清理
它的 native shadow/compat。`emergency_eager` 保留一个发布周期。任何清理不得删除
permission 双重检查。

---

## 15. 已定决策

| 问题 | 定论 |
|---|---|
| 延迟什么 | 延迟完整 schema 进入模型上下文，不延迟平台注册与资格 |
| Bash | build core；不是所有 agent 的无条件基础，也不是隐藏工具的安全替代 |
| Skill | loader 常驻，正文/引用渐进披露；永不触发工具物化或授权 |
| 明确意图 | deterministic pack 首轮直达，不先跑额外分类 LLM |
| 模糊意图 | capability_search，portable 最多多一个 step |
| 视频 | 命中后整体加载包，第一版不按状态拆六次 |
| native vs portable | portable 先行；endpoint+model 协议验收后才 native |
| Gemini/Kimi/未知代理 | 默认 portable |
| Batch | 只能调用 step_executable_ids，不能查 full eligible catalogue |
| persisted reveal | session/agent 隔离、TTL/LRU、schema digest 重验；state JSON 不存 opaque ref，同-binding opaque 只存独立 internal 表 |
| MCP 阈值 | 按真实 schema 总预算，不按工具数量 |
| prompt | 与 ExposurePlan 同源、条件化；不得命令模型调用隐藏工具 |
| budget | portable/native resident ≤24K chars、active ≤32K、单包 ≤12K；native/legacy/emergency wire ≤128K |
| 回退 | pre-stream unsupported 最多一次；partial stream 后绝不重放 |
| 内置 vs 无影自装 | 平台面（native/host custom）可进 core/pack/prompt fragment；沙箱面（全部 MCP、四来源技能）只经 discovery，恒非 same-response-safe（§2.7） |
| 技能来源 | host project/global + 沙箱 builtin/container 四来源一律零能力语义；沙箱预置技能落盘即按不可信数据处理 |
| 沙箱目录获取 | 缓存投影 + generation/ETag 条件请求（PR#5 §12.4）；规划期零隧道调用（铁律 12） |
| 沙箱形态 | 执行面唯一生产形态为无影云；docker/k8s provider 冻结，不写 exposure 分支 |
| 隧道断开 | 只是执行期错误；不缩资格目录、不清 reveal、重连免重新发现 |

再次固定：原解耦计划的“工具恒定注册”继续成立；“完整 schema 每轮恒定发送”不再是目标。

---

## 16. 已知坑

1. **HTTP request bytes 不等于模型可见 context。** Anthropic native deferred 仍要求请求携带
   definition；要分别记录 wire bytes、模型 input/cache tokens 与本地 materialized 计划。
2. **只测 helper 会假绿。** Responses、LiteLLM、Anthropic 必须抓最终 wire/event。
3. **allowed tool names 不等于少发 schema。** Gemini/OpenAI 的调用限制字段不能代替真正删
   declaration 或 native defer。
4. **prompt 会泄漏旧工具假设。** `system.py` 有大量无条件 web/todo/cron/computer 指令；
   不同步条件化就会 unknown tool 或 Bash 模拟。
5. **Batch 是旁路。** 它从 registry 查工具；`available_tools=eligible` 会使所有优化与顺序
   边界失效。
6. **历史 tool call。** 下一请求即使只剩 discovery，也不能错误插 `_noop` 或删除历史所需
   call/result 对应；provider switch 必须跑兼容测试。
7. **structured output。** 这是动态 synthetic tool；必须 resident、计预算、保留 tool_choice。
8. **state 无限增长。** “发现过就永久保留”最终会回到 30 工具；TTL/LRU 与 product-state
   pinned 必须区分。
9. **权限缓存。** catalogue 可以缓存，授权结论不能跨 user/project/cache key。
10. **schema 变化。** 仅存名字会让旧 reveal 指向新契约；schema digest/generation 必须校验。
11. **provider fallback 双执行。** partial stream 后重试最危险，尤其视频/图片付费 submit。
12. **MCP 名称碰撞。** 64 字符 provider-name 截断可能让两个 server/tool 得到相同调用名；
    必须为碰撞组生成稳定唯一名称并保留双向映射，不能静默覆盖或误当 canonical ID。
13. **Skill names-only 尾部。** 当前 description budget 不是最终 listing budget；大量技能仍会
    挤占 context。
14. **缓存键。** prompt cache 的 core 顺序必须稳定；新增 pack 只在尾部追加。
15. **估算器误导。** `len(tools)*400` 必须移除；同数量 schema 差异可达一个数量级。
16. **隐藏不是安全。** bash/curl/sed 仍可能模拟能力；真正边界必须在 sandbox、permission、
    backend service。
17. **MCP ID 升级。** canonical ID 必须是适配 `VARCHAR(128)` 的 bounded opaque ID，
    原始 tuple 通过 catalogue 映射无损保留；不能把任意长 raw identity 直接塞入 permission
    subject，也不能直接取代旧 sanitized subject。旧 deny/approval 必须经 alias compiler
    安全迁移。
18. **ToolPart 双身份。** 模型调用名是 provider binding，不是安全 ID；历史/切 provider
    必须从 canonical ID 重建。
19. **Internal history 降级。** 内部 provider block 不能与 public part 共表后期望旧服务忽略；
    独立表、writer gate 和 downgrade preflight 都必须落地。
20. **Reveal 因果与锁序。** event 与 JSON 投影必须同事务；reveal 与 branch mutation 都先
    锁 session row，再按稳定顺序处理 message/part/event。regenerate/delete/fork 要按存活
    事件重建，不能丢并发更新或保留已删分支的能力可见性。
21. **Native blocked call 也要闭环。** `same_response_safe=False` 不等于丢弃 tool call；
    必须用同 call ID 的无副作用 blocked result 闭合历史。
22. **每步隧道目录拉取是现状。** `merge_sandbox_tools` / `attach_skill_listing` 每
    step 各打一次隧道（`GET /skills` 裁剪前曾实测 55KB/step）。PR#5 之前这是既有
    行为——PR#0–#3 不要顺手改它，否则行为等价性验收失真；也不要把它误当模型
    token 成本（它是 backend↔沙箱 wire 成本）。
23. **执行面部署盲区。** `container/action_server.py` 跑在桌面上，后端重启/热重载
    不会更新它；改动必须 `wuying_deploy_action_server.py` 下发，且下发前后新旧版本
    会共存——一切协议增量（ETag/generation）必须先探测再使用、无头时优雅退回。
24. **共享单桌面。** 所有会话共用一台无影桌面：`/data/skills`、`/data/mcp/config.json`
    是全局状态，且可被沙箱内任意执行（包括 agent 自己跑的 bash）改写。目录投影与
    审批按 user key 隔离缓存，但"装了什么"事实上全局；absolute path 可逃逸 session
    工作目录。多租户上线前必须 revisit（Backlog 11）。
25. **TUN 代理与 trust_env。** 沙箱 HTTP 客户端 `trust_env=False` 不可回退；新增的
    目录投影/健康检查客户端同样必须显式关闭代理继承，否则被 TUN 级代理劫持后表现
    为"端口开着但请求挂死"（WUYING_SANDBOX.md 排障节）。
26. **桌面重启的连带效应。** systemd 自动拉起 action server 并 `reconnect_configured()`
    重连 MCP（工具集可能变化）、dev-browser 不自启、`START_TIME` 重置。`START_TIME`
    是唯一可靠的重启信号，必须进目录 generation 与 evidence 失效链。
27. **config 默认值陷阱。** `sandbox_provider` 代码默认仍是 `docker`；一切验收环境
    必须显式 `SANDBOX_PROVIDER=wuying`。不要因为本地 fixture 用 fake sandbox 就误以
    为 docker 路径被测试覆盖——它是冻结区，不是回归目标。

---

## 17. Backlog（本手册明确不在首轮做）

1. 根据生产 telemetry 自动调整 core/pack；
2. embedding/learned router、跨工具语义 reranker；
3. 视频包按 project state 进一步拆成 identity/generate/transcribe/render 子包；
4. 容器 Skill 正文提示注入隔离与 provenance 标注；
5. 前端“本轮工具为何可见/未可见”诊断 UI；
6. 用户可配置的工具 pin/favorite；
7. 跨会话共享 reveal（当前明确禁止）；
8. 将低风险 MCP 工具编译成 provider code execution callable（对照 codex/opencode 的
   Code Mode"折叠"范式，需先解决 typed 验证/审批/UI 的等价物）；
9. 自动根据 provider 实际 tokenizer 调整 chars 预算——在稳定、可测试前仍以 chars 为硬门；
10. backend 直连"平台托管 MCP"平面：可信 MCP 进入 deterministic pack 的前提工程
    （独立注册通道 + 管理员审计，不复用沙箱 MCP 默认信任，§2.7 第 4 条）；
11. 无影多租户隔离：per-user 桌面或 action server 路径约束，对齐
    `docs/MULTI_USER_STORAGE_PLAN.md`；在此之前共享桌面按单租户运营（坑 24）；
12. 目录投影的事件推送升级：action server 主动推送 skills/MCP `listChanged`，替代
    §12.4 的条件请求轮询。

---

## 18. 实施完成记录（2026-08-31）

本章记录 `5702fd4` 的实际落地与最终验收。它是执行结果，不改变 §2 的安全不变式，也
不把 Browser/Provider 环境限制伪装成代码通过。后续维护者应以本章的真实测试口径和
§13 的验收矩阵共同判断回归。

### 18.1 最终数据流

```text
平台注册 + AgentDef allowlist + 沙箱目录投影
                    ↓
         permission-before-catalogue
                    ↓
             eligible catalogue
                    ↓
 resident core + explicit intent + product state + valid reveal
                    ↓
 20K resident soft / 24K resident hard / 28K active soft / 32K active hard
                    ↓
 provider-visible definitions + step_executable_ids
                    ↓
 typed reveal evidence → session state → 下一 step 再验权限/契约
                    ↓
       executor hooks / approval / paid guard / sandbox
```

这里有四个必须继续分开的集合：

1. `eligible`：AgentDef、环境与 permission 过滤后的完整资格目录；
2. `materialized`：本次 provider 真正看到的完整 schema；
3. `step_executable_ids`：本次可直接执行及可被 Batch 嵌套调用的集合；
4. `revealed`：有可信 typed evidence、已持久化、且当前 generation/schema/permission
   复验仍有效的历史发现集合。

Skill 正文、frontmatter、普通工具 metadata 与沙箱返回值都不能把 ID 写进第 2–4 个集合。

### 18.2 已落地能力

| 模块 | 实际结果 |
|---|---|
| 常驻与路由 | build 使用 lean resident core；视频、图片、research、browser、automation 按明确意图首轮直达；运行中的视频、待办、交付资产按 product-state 以更高优先级保留 |
| Portable discovery | 新增 `capability_search`；模糊能力先返回最多 5 个受权限过滤的候选，下一 step 才物化完整 schema；每 response 的搜索次数、唯一 ID 与返回字符有聚合硬限 |
| 大 Skill 目录 | 小目录继续使用 `skill` listing；超过 8K 时原子切换为独立 `skill_search`，正文仍由 `skill` 按需加载；四来源 Skill 的工具字段继续零效果 |
| 持久状态 | session 保存版本化 reveal 投影，独立 InternalPart 保存 provider 私有事件；agent、catalogue generation、schema digest、TTL/LRU、删除/重生/fork 均有 fail-closed 处理 |
| Provider | OpenAI Responses 只在 endpoint + model allowlist 命中后发起 binding-scoped native canary；account/headers 属 binding 与 capability cache 隔离维度；明确 unsupported 后 sticky portable；Anthropic LiteLLM、Gemini、Kimi 与未知代理使用 portable |
| 历史身份 | ToolPart 分离 canonical ID 与 provider wire name；同 binding 原样 replay，切 provider 按当前唯一映射重建，碰撞或不完整身份拒绝执行 |
| MCP | action server 提供 `/catalog`、generation 与 ETag/304；后端 TTL/singleflight/LKG；canonical v2 ID、碰撞处理、permission-before-index、搜索证据、执行前二次授权、资源正文截断均已落地 |
| 无影云断连 | 暖目录失联时保留最近可信投影且不破坏 reveal；调用只执行一次并返回清晰不可达；恢复后无需重新发现 |
| Schema 预算 | provider-exact serializer、逐工具与总目录计量已进入测试；描述瘦身不删除 enum、required、边界、幂等、审批或安全约束 |
| Retry | 仅首个 provider event 前的明确可重试错误可以重放；任一 event 后断流只结束当前 step，禁止自动重放与双执行 |

### 18.3 量化结果

| 口径 | 结果 |
|---|---:|
| 改造前 build 30 工具 + 动态 Skill listing，Responses 形态 | 56,503 chars / 12,891 `o200k_base` 代理 tokens |
| 描述瘦身后 legacy eager，Responses 形态 | 38,982 chars |
| Browser A 首个 portable payload | 10 个 definitions / 12,603 chars / 2,843 代理 tokens |
| Browser A 当次 deferred catalogue | 21 个工具 |
| 七个永久媒体/creator schema 的既有安全预算 | 9,882 chars，≤10,000 |
| video-production 主 SKILL | 83 行，≤90 |

`o200k_base` 仅用于稳定比较，不冒充其他 provider 的精确计费 token。OpenAI/Anthropic
上线判断仍必须用 provider usage 与冷缓存 A/B；HTTP 请求字节也不能冒充模型可见 context。

### 18.4 自动化验收

最终代码提交前复跑结果：

- `cd backend && uv run pytest -q`：**1188 passed，17 warnings**；warning 仅为既有
  Pydantic class Config 与 Python 3.12 sqlite datetime adapter deprecation；
- `uv run python -m compileall -q agent api core db models sandbox session tool`：通过；
- Alembic：单一 head `c7d9e1f3a5b7`；旧库到新 head、API 隐藏内部状态和安全 downgrade
  均有测试；
- `git diff --check`：通过；Python 中 `skill_only`、`activated_tools`、
  `activate_skill_tools`：零命中；
- `frontend-v2`：22 个文件、174 个测试通过，`npx tsc -b` 与 i18n 检查通过；
  `npm run check` 仍只报告未改文件 `frontend-v2/src/features/chat/lib/content-view.ts`
  的两条既有 complexity 错误及既有 warning；本轮无前端运行时代码 diff；
- 遗留 `frontend/`、`mobile/`、`k8s/`、Dockerfile 与 docker/k8s sandbox provider：零改动；
  允许范围内仅更新 `container/action_server.py` 的无影云目录投影协议。

### 18.5 浏览器 A–J 实测

所有可能产生费用的场景使用 fake/loopback 或停在审批前。联网场景按当前网络条件改用
中国大陆可访问的 `https://www.baidu.com`；国外站点不可达不计为 OpenBox 回归。

| 场景 | 结果 | 关键证据 |
|---|---|---|
| A 通用编码 | 通过 | 首包仅 10 个 core definitions；无 media/browser/research/cron/MCP；真实创建、测试并删除临时 Python 文件 |
| B 视频直达 | 通过 | 未提技能名，并显式要求不加载 Skill；首轮仍出现 video pack，完成 project、完整台词与 script approval 后停止；零付费 |
| C 视频续接 | 通过 | 第二轮和后端完整重启后直接 status/继续，无重搜、无重复项目 |
| D 图片直达 | 通过 | 本地 fake provider 生成 1024×1024 蓝图，再以同一 asset_id 编辑为绿图；零付费，fixture 已删除 |
| E Research | 通过 | `web_fetch` 成功读取百度标题“百度一下，你就知道” |
| E Browser | 路由与调用通过；视觉环境受限 | browser pack 首轮出现，`computer` 执行打开百度与截图；无影桌面当时停在应用总览，模型明确报告未看见页面且未盲目重试。此项不作为 schema/权限实现通过证据，发布环境仍需按 §13.5 启用 dev-browser 后复测页面内容 |
| E Automation | 通过 | 唯一 QA 标签的 cron 创建、list 确认、delete 清理；普通编码首包不含 cron |
| F 恶意 Skill | 通过 | 无影云测试 Skill 同时伪造 `allowed-tools`/`tools`；加载前后四集合不变，清理后目录无残留 |
| G fallback | 通过（契约测试） | pre-stream 明确 unsupported 仅一次 portable fallback；首 event 后断流不重放，executor 不重复 |
| H Portable discovery | 通过 | 临时 custom agent 首包只有 `capability_search`；N→N+1 才物化 echo；第二轮及后端重启后直接执行；fixture/config 已删除 |
| I Native | 条件未满足，安全保持 portable | 测试账户没有可证明的 live native entitlement；未用 mock 冒充 canary。OpenAI raw wire/stream/replay 契约测试通过；Anthropic adapter 未验证，明确不启用 |
| J 无影云 MCP | 通过 | 安装无副作用 MCP 后首包不泄漏；搜索→单次调用得到 42；ETag 200/304；隧道断开只调用一次并清晰失败，恢复后免重搜；refresh generation 变化；最终卸载清理 |

### 18.6 浏览器阶段发现并闭环的缺陷

浏览器与对抗审查不是形式验收；本轮实际发现并修复了以下实现问题：

1. 旧开发进程未运行新迁移而 `/health` 假绿：增加完整 schema-head readiness；
2. config-defined agent 无法显式 opt-in portable：只对明确列出 discovery slot 的 custom
   agent 开放，默认 custom/plan/explore/general 仍 shadow；
3. 多 ID reveal 逐项提交会部分落库：改为同 session 锁与单事务的整批原子提交；
4. provider 首 event 后网络错误会自动重放：改为 ERROR 且 executor 零重放；
5. 无影云外部托管 sandbox 健康失败会替换暖 client、丢失 LKG：断连时保留真实 client，
   stale/unavailable 不做 destructive prune；
6. 大 MCP 目录 permission-before-index、canonical ID 碰撞、搜索证据、资源正文与远端
   identity 字符串存在旁路或放大风险：全部加边界、截断和二次授权；
7. catalogue snapshot wrapper 与真实 sandbox 的身份不同，导致 `find → call` 证据失效：
   identity 统一解包到真实 live client，同时保持不同 sandbox 严格隔离；
8. 同一 MCP generation 每 step 重做 schema digest/index：增加 generation/scope/dialect 绑定的
   60 秒、64 项 LRU 与同 event-loop singleflight；授权结论不缓存，每次读取后重新过滤；
9. product-state probe 瞬时失败会隐藏在途付费任务工具：增加每 user/session/probe 的短 TTL
   布尔 LKG；首次失败仍 fail-small、失败不续 TTL、成功 False 覆盖旧 True。
10. “不要加载任何 Skill，直接做视频”中的负向 Skill 子句曾让提示清理逻辑误吞同句视频
    意图：改为只清理 Skill 指令片段，保留同句真实任务信号，并由 Browser B 锁定。

### 18.7 发布结论与剩余条件

`portable` 路径和无影云目录投影已经达到代码合并条件；`native_auto` 仍必须 fail-safe：

- OpenAI 只有 endpoint + model allowlist 命中时才允许发起 binding-scoped native canary；
  account、headers、credential/config 等进入 binding/cache 隔离键。首次 canary 本身就是 probe，
  未命中 allowlist 或明确 unsupported 时使用 sticky portable；
- Anthropic/LiteLLM 没有真实 wire-level conformance 证据，保持 portable，不能仅凭 SDK 参数
  被接受就开启；
- Browser I 的 live native canary 与冷缓存 token A/B 是 rollout exit criteria，不是本次代码
  合并的伪造前置；
- Browser E 的页面视觉内容需要在无影云 dev-browser 已 Enable、桌面不在应用总览的环境中
  用国内站点复测；其当前限制不影响 research、automation 或 schema 分层结论；
- 发布 `container/action_server.py` 时必须使用无影云窄部署脚本，并确认 `/alive` 显示
  `2026.08.30-catalogue-projection-v1`、uptime 重置及 `/catalog` ETag 行为；
- 工作区中与本任务无关的未跟踪嵌套仓库 `deepseek-harness/` 未进入 `5702fd4`，后续提交也
  必须继续排除，禁止使用无审查的 `git add -A`。

最终不变式仍是：**Skill 只注入知识；平台注册与 AgentDef 决定资格；permission 只做减法；
Schema 物化只优化上下文；执行必须重新经过权限、审批、计费、幂等与沙箱边界。**
