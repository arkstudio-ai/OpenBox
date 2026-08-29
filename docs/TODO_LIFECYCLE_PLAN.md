# Todo 卡片生命周期修复计划

> 状态：**已实施并通过浏览器验收**（2026-08-29，commit 99deb1d / 4e52585）。前置调研与方案推演见 2026-08-29 会话记录；codex 对照结论已并入。
> 原则来源：skill-job 的「活卡/回执分离」、reconciler 的「平台结算死者状态」、
> `_derive_status` 的「推导不信声明」——本计划是同一哲学在 todo 上的延伸。

## 1. 病根（一句话）

Todo 卡把**实时执行状态**永久渲染进了**历史记录**：中断后没有任何机制把
`in_progress` 结算成诚实的终态，而进度条是按流逝时间渐近爬升的拟真动画
（`todo-progress.ts`，封顶 90%），于是一个死回合在界面上持续表演"正在进行"。

## 2. 已核实的代码事实（计划的全部锚点）

| # | 事实 | 位置 |
|---|---|---|
| F1 | todo 会话级存储，全量替换写；每次写**追加** TodoPart 快照进消息流 | `tool/todo_tool.py`、`db/models/todo.py`（session_id 唯一） |
| F2 | 中止有 **4 个触发点**，三处是复制粘贴的相同代码块 | `api/sessions.py:238`（新消息抢占）、`:284`（stop 端点）、`:498`（regenerate）、`tool/task.py:153`（子会话） |
| F3 | loop 检测到 abort 时已把末条消息盖 `finish="aborted"` | `agent/loop.py:366` |
| F4 | 中止路径完全不碰 todos，也不向对话注入任何东西 | 已 grep 确认 |
| F5 | 前端逐回合渲染卡片；`streaming = busy && i === turns.length-1` 已在 ChatFlow 算好 | `ChatFlow.tsx:90`、`AssistantTurn.tsx` |
| F6 | 同一回合内多次 todo_write 已合并为一张卡（回合内快照序） | `turn-view.ts:168` |
| F7 | mobile 有对应实现，需同步 | `mobile/lib/features/chat/utils/{turn_view,todo_progress}.dart` |
| F8 | 合成用户消息机制已存在且模型可见（inbox 续跑标记先例） | `session.create_user_message(synthetic=True)` |
| F9 | codex 对照：`update_plan` 无存储无生命周期；中断以带内历史标记告知模型；UI 只渲染静态快照 | `codex-rs/core/src/tools/handlers/plan.rs`、`core/src/tasks/mod.rs`（INTERRUPTED_GUIDANCE） |

## 3. 目标与非目标

**目标**
1. 中断后界面上**不存在任何撒谎的"进行中"视觉**（含崩溃场景）。
2. 模型在下一回合**带内**得知中断事实与残留状态，据此自行判断续跑/改造/不理。
3. 同会话多张 todo 卡不再互相竞争，历史快照保真。
4. web 与 mobile 行为一致；无数据迁移；可整体 revert。

**非目标**
- 不做"恢复按钮"之类的新状态通路（用户想续跑直接说话即可——少一条通路少一类 bug）。
- 不扩展 todo status 枚举（`interrupted` 只是**显示层推导**，不落库，保护 mobile 与 API 消费者）。
- 不解决"崩溃后无标记"（codex 同样只在主动中断时写标记，对齐；显示层推导已兜底崩溃的展示问题）。

## 4. 设计总览：三层各管一件事

```
带内事实层（后端）   中止 → 注入一条中断标记消息（模型下回合可见）
存量结算层（后端）   中止 → stored todos 的 in_progress → pending（现在时归零）
展示推导层（前端）   live 视觉只准出现在「streaming 回合自己的卡」上；其余按措辞表结算
```

正确性只依赖第三层（纯客户端推导，崩溃也成立）；第一层给模型；第二层给
todo_read 的诚实性。任何一层失效都只降级为美观问题，不产生误导。

## 5. 详细设计

### 5.1 中止收敛点（P0，后端）

四个触发点的复制粘贴块收敛为一个 helper（放 `session/status.py` 或新
`session/abort.py`）：

```
abort_session_turn(session_id, user_id, *, reason: "user_stop" | "preempted" | "error")
  1. trigger_abort + set_session_status(IDLE)（现有逻辑不变，含 0.3s 等待）
  2. 若该会话确有在跑的回合（status 曾为 ACTIVE）→ 注入中断标记（5.2）
  3. 结算 stored todos（5.3）
  4. 幂等：末条消息若已是本回合的标记则跳过（防双击 stop）
```

四个调用点全部改走 helper。`tool/task.py` 的子会话中止同样受益。
**顺序保证**：新消息抢占路径里 helper 在 `create_user_message` 之前执行，
所以标记永远排在新用户消息前面——模型读史顺序天然正确。

### 5.2 中断标记（P0，后端）

一条 `synthetic=True` 的用户消息，`client_message_id = f"tabort:{session_id}:{utc_ts}"`
（前缀供前端识别）。文本动态拼装，模板（仿 codex INTERRUPTED_GUIDANCE，
补 OpenBox 自己的真话）：

```
[上一回合已被用户主动中断]
- 被打断的工具调用可能只执行了一半。
- 后台 SkillJob 不受中断影响，仍在运行，勿重复提交。
- 任务清单停在中断时刻{有 in_progress 时: ：第 N 步「XXX」进行中被打断}。
是否延续该清单由你根据用户下一条消息判断；与新请求无关时不要主动接手。
```

`reason="error"` 变体首行改为「上一回合因内部错误终止」，其余相同
（挂接点：`agent/loop.py` 顶层错误处理，与 `finish` 盖章同处）。

文本要求**以自然语言可直接示人**：在前端做分隔条样式之前（P1），它作为
普通消息气泡出现也不脏。

### 5.3 存量结算（P0，后端）

在 helper 内：stored todos 中 `in_progress` → `pending`。依据：中止之后
"没有任何东西正在进行"是事实；历史（TodoPart 快照）不动，故事由标记讲。
不新增枚举值；mobile / GET 端点 / todo_read 全部零改动兼容。

### 5.4 展示档位推导（P0，前端 web + mobile）

**live 准入谓词**（唯一允许动画/百分比/现在时措辞的条件）：

```
isLive = 该卡属于 streaming 回合（ChatFlow 已有：busy && 末回合）
```

注意：**绝不能用"会话 busy"**——无关新回合会把旧卡重新点亮（推演已证伪）。

**settled 措辞表**（挂在 chat feature 的 `turn-view.ts`/`todo-progress.ts` 旁，
纯函数 + 单测）：

| 条件（按序判定） | 卡头措辞 | 进度显示 |
|---|---|---|
| 全部 completed | 已完成全部 N 步 | 满条（现状不变） |
| 回合内有 `finish=="aborted"` 或后随本回合的 `tabort:` 标记 | 已中断 · 完成 X/N，停在「第Y步」 | 离散计数，无百分比，条静止置灰 |
| 正常结束但残留 in_progress/pending | 未完成 · X/N 步 | 同上 |
| 会话内**之后**还有更新的 todo 卡 | 折叠为单行「任务清单 · 当时 X/N」，点开可看全貌 | 无 |

**百分比只在 live 档存在**。settled 档一律离散步数——百分比是对精度的承诺，
死任务不配拥有。

**折叠规则**（解决截图里多卡竞争）：一个会话的消息流中，仅**最后一张**
todo 卡有资格展开；此前的自动收成单行。

### 5.5 编辑入口收敛（P1，前端）

settled 卡隐藏增删任务入口（对休眠清单误操作无意义且会让 stored 与快照
分叉）；live 卡保持现状。不做"恢复"按钮——续跑靠用户说话，走 5.2 的带内
判断链路。

### 5.6 标记的分隔条渲染（P1，web + mobile）

前端识别 `tabort:` 前缀，把标记消息渲染为居中分隔条「⏹ 已中断」，原文进
折叠详情。P0 阶段按普通气泡显示（文本已按可示人标准写）。

## 6. 场景验收矩阵（浏览器逐条过）

| # | 场景 | 预期 |
|---|---|---|
| S1 | 执行中点停止 | 卡立即落 settled：「已中断 · 完成 X/N，停在…」；圆圈停跳；无百分比；stored 无 in_progress；对话尾部出现标记 |
| S2 | 停止后发**相关**消息（续跑） | 模型读到标记与旧清单，重新 todo_write → 新回合出新卡（live），旧卡自动折叠单行 |
| S3 | 停止后发**相关**消息（改计划：加任务/改名/整体重排） | 同 S2，新卡呈现新计划；旧卡单行保留当时快照，历史不被改写 |
| S4 | 停止后发**无关**消息 | 模型不碰 todo；旧卡保持 settled 折叠，**不因会话变 busy 而复活**；新回合无 todo 卡 |
| S5 | 双击停止 / 停止后立刻再停止 | 标记只有一条（幂等） |
| S6 | 停止后立即发新消息（抢占竞态） | 标记排在新用户消息之前 |
| S7 | 回合正常结束但清单留了未完成项 | 「未完成 · X/N」中性措辞，不写"中断" |
| S8 | 强杀后端进程再刷新页面 | 无标记（已知残留），但卡照样 settled——显示推导不依赖任何写入 |
| S9 | mobile 打开同一会话 | 档位与 web 一致 |

## 7. 测试计划

**后端单测**（`tests/unit/test_turn_abort.py` 新建）
- helper：注入标记 / stored 结算 / 幂等 / 无在跑回合时 no-op / error 变体
- 顺序：抢占路径下标记先于新用户消息（时间戳断言）
- 四个调用点全部走 helper（grep 断言无残留内联块）

**前端单测**（与组件同目录）
- live 谓词：streaming 回合亮、非 streaming 灭、**无关新回合不复活旧卡**（S4 的单测化）
- 措辞表逐行；折叠规则（仅末卡展开）
- 变异检查：删掉谓词 → 用例失败；删掉措辞分支 → 用例失败

**浏览器 E2E**：第 6 节矩阵逐条，S1–S4 必测，S5–S8 抽测。

**mobile**：turn_view/todo_progress 对照移植 + 单测；locale 与 web 逐字节同步
（既有纪律）。

## 8. 改动面

| 层 | 文件 | 动作 |
|---|---|---|
| 后端 | `session/abort.py`（新）或并入 `session/status.py` | helper：中止收敛 + 标记 + 结算 |
| 后端 | `api/sessions.py` ×3、`tool/task.py` | 内联块替换为 helper 调用 |
| 后端 | `agent/loop.py` | error 路径挂 error 变体标记 |
| 前端 | `features/chat/lib/turn-view.ts`、`todo-progress.ts` | live 谓词 + 措辞纯函数 |
| 前端 | `features/chat/components/TodoCard.tsx`、`AssistantTurn.tsx` | 档位渲染 + 折叠 + 入口收敛 |
| mobile | `utils/turn_view.dart`、`todo_progress.dart` 及卡片组件 | 对照移植 |
| i18n | `locales/{zh-CN,en-US}/chat.json` + mobile 逐字节同步 | 措辞表键值 |

无迁移、无枚举扩展、无 API 变更；revert = 一次 git revert。

## 9. 风险与已知残留

| 项 | 等级 | 说明 |
|---|---|---|
| 崩溃无标记 | 低·已接受 | 与 codex 对齐；显示推导兜底，模型可从截断的工具序列自行判断 |
| 标记被 compaction 摘要吞掉 | 低 | 压缩保留近尾（`preserve_recent_tokens`），紧邻回合必见；远期衰减是合理语义（多轮之后中断已无关紧要） |
| stored 结算与 loop 残写竞态 | 低 | helper 在 0.3s abort 等待之后执行；结算是幂等 UPDATE |
| 措辞表与 mobile 漂移 | 中 | 纯函数 + 双端同构单测钉死 |
| `skill.yaml`/SKILL.md 类比坑：reload 不监视 dart/locale | 提醒 | mobile 改动需重新构建验证 |


---

## 10. 实施记录与对计划的偏离

### 计划之外发现的三件事

1. **停止按钮端点不在计划的清单里。** 第 2 节 F2 列了四个中止点，读代码时才发现
   `api/sessions.py:600` 的 `/abort` ——**真正的停止按钮**——才是主路径，而原先
   列出的三处全是「新指令抢占」。四处一并收敛，reason 分为 `user_stop` /
   `preempted`。

2. **`tool/task.py` 的子会话中止未纳入 helper。** 它的形状本就不同（signal 子会话
   后带超时等待，不设状态、不睡 0.3s），且子会话不是用户继续的对话，写标记没有
   意义。强行套用会改变 teardown 时序。这是有意偏离，非遗漏。

3. **`storage` 的 kv_store 写入硬编码了 PostgreSQL 专有的 `NOW()`。** 写测试时
   撞上 `no such function: NOW`——单用户 SQLite profile 下这条路径本来就是坏的。
   顺手改成 Python 侧绑定时间戳。

### 浏览器验收结果

| 场景 | 结果 |
|---|---|
| S1 中断 | 卡从 `21%` 活态 → `已中断 · 完成 0/3，停在第 1 步`；无条无百分比、圆环静止、停止与编辑入口收起；库中 `in_progress→pending`、`started_at` 清空、标记 1 条 |
| S2/S3 相关续跑 + 改计划 | 模型恢复第 1 步并新增第 4 步；DOM 断言：旧卡 `open=false` 折叠、新卡 `第 1/4 步` 活态 |
| S4 无关消息 | 模型完全未碰清单；快照数与标记数均不变；**旧卡未因会话变忙而复活** |
| S8 崩溃残留 | 伪造 `in_progress` + 会话 idle + 无标记无结算 → 仍渲染 `已中断 · 完成 3/4，停在第 4 步`。**证明展示推导不依赖任何后端写入** |

S5（双击停止）、S7（正常结束留有未完成项）由后端单测覆盖；S6（抢占顺序）由
helper 的执行顺序保证并有单测。

### 验收后修的一个缺陷

标记写进了库、API 也返回了，界面却没有分隔条：`mergeTurns` 会丢弃「只含
synthetic 文本」的用户消息。该规则本是为隐藏续跑/压缩类内部提示而写，而中断
标记恰好符合那个形状。按保留前缀豁免（commit 4e52585）。这类缺陷只有真跑浏览器
才会暴露——单测测的是纯函数，看不到这一层。
