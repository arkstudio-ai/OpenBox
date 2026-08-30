# 直连路径清理与强化——执行手册

> 文档状态：Execution Handbook v2（2026-08-30）<br>
> **本文件是执行助手的第一准则。** 你（执行者）没有参与前期讨论，本文即全部上下文；
> 按顺序执行，不要引入本文之外的目标。行号以 commit `1649310` 为基准，动手前先用
> grep 复核（行号会漂移，符号名不会）。<br>
> 决策规则：本文已对所有已知分歧给出定论（§5）。遇到本文未覆盖且影响范围超过单文件
> 的新情况：**停下来向用户报告**，不要自行发挥。

---

## 0. 执行者须知（先读完再动手）

### 0.1 你要做什么

删除已退役的"耐久 SkillJob 后台任务运行时"约 1.2 万行死代码（M1，四个 PR），
并把两条仍有价值的契约迁回直连工具层（M2）。运行时已于 2026-08-30 用配置开关禁用
（commit `1d453a0`），当前没有任何调用者；本手册把"禁用"推进到"移除"。

### 0.2 必读文件（按序）

1. 本文件全文；
2. `frontend-v2/docs/ENGINEERING_SPEC.md` —— 前端规范。最常踩的条款：
   §4.1 features 之间禁止互相 import、§4.2 由 routes 接线、§6.1 行数分级门禁、
   §9.4 逻辑属性（`ms-`/`me-`，不用 `ml-`/`mr-`）；
3. `mobile/README.md` —— 移动端规范（单文件 800 行门禁；locale 与 web 逐字节同步）；
4. `backend/.openbox/skills/video-production/SKILL.md` —— 现行直连流程（保留区，读懂它
   才知道哪些是"活"代码）。

### 0.3 环境地图（实测事实，非猜测）

| 事项 | 事实 |
|---|---|
| 后端启动 | `uv run --directory backend python scripts/backend_entrypoint.py --reload --host 0.0.0.0 --port 8080`（先跑 alembic 再起 uvicorn；与 `frontend-v2/vite.config.ts` 代理目标一致） |
| 前端启动 | `npm run dev --prefix frontend-v2`（端口 3000） |
| 数据库 | Docker 容器 `openbox-postgres-1`（postgres:16-alpine，5432）。**宿主机没有 pg_dump**（实测 command not found），一切 pg 工具用 `docker exec openbox-postgres-1 ...` |
| 后端配置 | `backend/openbox.json`（**gitignored**，本机真实配置）+ `backend/.env`；模板是 `openbox.jsonc.example` |
| WUYING 沙箱 | `.env` 指向生产桌面（隧道 `127.0.0.1:18000`）；`.env.wuying-dev` 指向 dev 桌面（18001，**已过期不可用**）。隧道脚本 `backend/scripts/wuying_dev.sh` |
| 热重载盲区 | `uvicorn --reload` 只看 `.py`；改 `openbox.json` / `skill.yaml` / locale 必须手动重启后端 |
| 测试基线 | 后端 `cd backend && uv run pytest -q` → **1016 passed**；前端 `npm run test`（vitest，182 例）、`npm run check`（含 tsc）、`npm run lint`（**存在 2 个既有错误**，均在 `content-view.ts`，圈复杂度 27/50——不是你造成的，也不许新增）；移动端 `cd mobile && dart analyze` 零问题 |
| 浏览器验收账号 | `qa_jobs`，**密码见 `docs/LOCAL_CREDENTIALS.md`**（gitignored，不在仓库里，本机已存在）；历史数据验收会话：`http://localhost:3000/app/s/session_7YBYRVD9SKGYEGNXHXHCXDXPG8` |
| ⚠️ 仓库可见性 | `origin` = `github.com/arkstudio-ai/OpenBox`，**公开仓库**。任何入库内容都会被公开发布且 git 历史永久留存——**绝不把密码、token、API key 写进任何被跟踪的文件**（包括本手册） |
| 提交规范 | 中文 commit message、`类型(范围): 摘要` + 正文讲清 why；每个 PR 单独提交并 push `origin main` |

### 0.4 铁律

1. **绝不重新开启** `skill_jobs_enabled` / `skill_jobs_video_write`（删除它们正是任务之一）。
2. **v1 前端（仓库根 `frontend/` 目录，若存在）完全不碰**——用户明示另行处理。
3. 不动区清单（§2）里的任何文件被你的改动波及，先停下核对本文，再动。
4. 破坏性操作（drop 表）前必须完成 §3.4 的快照步骤。
5. 每个 PR 的 Definition of Done（§6）全绿才允许提交；浏览器验收不可跳过——
   本仓库的规矩是"不信代码测试"。

---

## 1. 背景：为什么删（执行时的判断依据）

耐久运行时是 2026-08-28 落地的九态作业状态机（租约/fencing/outbox/reconciler/
session_inbox 唤醒链，历史设计底稿见
`docs/archive/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md`）。
灰度两天暴露成批缺陷（无界拨号循环、失败通知漏 3/4 条死法、operator-only 停靠在无
admin 账号的部署死锁、取消不唤醒停靠），2026-08-30 用户决定禁用，视频制作回归直连
路径：agent 按 `.openbox/skills/video-production/SKILL.md` 直接调 `video_generate` /
`video_transcribe` / `video_render` 并在回合内有界等待。

定稿的设计哲学（你在改代码拿不准时用它裁决）：**薄 agent + 硬工具**。
状态由工具提供者管控且只有一份（域表 `video_*`）；LLM 凭 `video_project(status)`
重建事实并自由决策；平台不做编排。恢复 = 用户再说一句话；付费安全 = 服务端幂等键。
codex 与 Claude Code 均为此形态，无一家构建耐久作业状态机。

由此推出删除边界：**凡是"编排"的都删，凡是"不变量"的都留。**

---

## 2. 不动区（防误删清单，删除前逐条自查）

| 区域 | 原因 |
|---|---|
| `frontend/`（v1，若存在） | 用户指示排除 |
| `backend/tool/video_workflow.py` | 共享不变量层：审批哈希、花费上限（`max_calls`）、提交时模型冻结（`resolve_segment_model`）、b-roll 豁免（`:695`）、分段 lint。两套控制面它一行未改——它就是"硬工具"本体 |
| `backend/tool/video_production.py`、`video_providers.py`、`video_identity.py` | 现行直连控制面 |
| `backend/video/`（materials 等） | 域层 |
| 表 `skill_installs` / `user_skills` 与 `backend/skill/user_library.py` | **技能中心**（创建/分享/安装）的资产，与运行时同名不同族。运行时的表只有 §3.4 列的七张 |
| 表 `kv_store` 与 `backend/storage/storage.py`、`backend/mcp/oauth.py` | MCP OAuth 在用 |
| `backend/session/abort.py`、turn-view/todo/中断分隔线相关（前端与 Dart 两侧） | todo 生命周期功能，与运行时无关 |
| `frontend-v2/src/features/chat/components/SkillJobReceipts.tsx` 与 `mobile/lib/features/chat/widgets/cards/skill_job_receipt.dart`、`mobile/lib/shared/models/skill_job.dart` | 历史回执渲染器：回执是已落库的消息 part，老会话必须继续可读。该组件自注释写明"仅凭 part 数据渲染，不依赖 jobs API"，天然存活 |
| `backend/tests/unit/test_video_model_snapshot.py` | 测的是共享层 `resolve_segment_model`，只删其中一个用例（§3.6） |
| `backend/.openbox/skills/`（全部四个技能目录） | 现行技能文档 |

---

## 3. PR#1 后端移除（M1 主体）

### 3.1 整目录/整文件删除

| 路径 | 行数 | 内容 |
|---|---|---|
| `backend/skill_runtime/`（整目录，14 文件） | 6,179 | 运行时本体 |
| `backend/builtin_skills/`（整目录） | 2,010 | 耐久版 video handlers、demo_echo 参考技能、门控版 SKILL.md/skill.yaml |
| `backend/api/skill_jobs.py` | 213 | 作业 API |
| `backend/api/skill_settings.py` | 62 | 按用户启停内置技能。**前端零调用**（已 grep 实证 `skills/settings` 在 frontend-v2 无命中） |
| `backend/tool/skill_job.py` | 278 | agent 侧作业工具 |
| `backend/db/models/`：`skill_job.py`、`skill_job_attempt.py`、`skill_job_event.py`、`skill_job_input.py`、`skill_job_artifact.py`、`session_inbox.py`、`user_skill_setting.py` | 319 | 七表 ORM 模型 |

### 3.2 接线手术点（逐处，行号基准 `1649310`）

1. **`backend/db/models/__init__.py:20-26`**：删除七个模型的 import。
2. **`backend/main.py`**：
   - `:89-91` `ensure_job_engine`；
   - `:126-133` embedded worker 启动块（含 `SKILL_WORKER_MODE` 分支）；
   - `:138-146` `InboxDispatcher` 启动，及 lifespan 尾部对应的 `inbox_dispatcher.stop()`；
   - `:168-169` `stop_embedded`；
   - `:283-286` 两个 router 的 import 与挂载。
3. **`backend/session/session.py`**：`continuation_jobs` 的完整生命周期——声明
   `:223`、收集查询 `:245`（查的是 `session_inbox`）、两个使用块 `:258` 与 `:276`
   （删除会话时取消续跑作业）。整段移除；会话删除的其余逻辑不动。
4. **`backend/api/ws.py`**：删除 `_has_active_skill_jobs`（`:45`）及其**三处调用**
   （`:180`、`:194`、循环内第三处）。⚠️ 不需要写替代——同函数已有
   `_has_active_agent_sessions` 在每个调用点**并列存在**，直连路径下"工作是否进行"
   的事实源就是它（回合在服务端跑，会话 busy 即活跃）。删完后该清理门语义完整。
5. **`backend/tool/registry.py:70-91`**：删除 `durable_video_authoritative` 条件分支，
   三个直连工具改为无条件注册；同时删除 `skill_job_tool` 的 import 与注册项。
6. **`backend/skill/skill.py`**：删除 `enabled_config_flag` 字段（`:32`、`:139-153`）、
   `gated_builtins` 逻辑与 builtin 目录扫描（`:184-209` 一带）。project/global 两级
   扫描保留。`SkillInfo.source == "builtin"` 的其他残留一并清理。
7. **`backend/core/config.py`**：删除配置簇（约 `:324-345`）：`skill_jobs_enabled`、
   `skill_worker_mode`、`skill_worker_queues`、`skill_worker_concurrency`、
   `skill_worker_lease_seconds`、`skill_worker_per_user_concurrency`、
   `skill_worker_invocation_timeout`、`skill_jobs_video_write`、
   `skill_job_chat_receipt`；以及 env 映射表（约 `:537-563`）里对应的
   `SKILL_JOBS_*` / `SKILL_WORKER_*` 条目和布尔解析列表里的旗标名。
8. **`backend/tests/conftest.py`**：删除 `_runtime_flags_are_test_owned` fixture
   **（必须与 #7 同一提交）**——它 `monkeypatch.setattr` 这两个旗标，属性一删即全场
   `AttributeError`。
9. **本机 `backend/openbox.json`**：删除 `skill_jobs_enabled` / `skill_jobs_video_write`
   两键及其注释块（gitignored，改完**手动重启后端**）；`openbox.jsonc.example` 同步
   清除运行时示例段。

### 3.3 错误公开契约下沉（与 3.1 同一 PR，不留窗口期）

**问题**：`public_error_text` 契约在 `skill_runtime/types.py`；
`backend/video/materials.py` 的 `MaterialProviderError` 按它携带逐实例的
`public_message` / `retryable` 标记（默认 `False`/保密）。运行时删除后，直连路径的
`tool/video_production.py:723 _public_error` 只输出 `"类名: operation failed"`——
用户将看不到 `请配置 material_base_url` 这类修复指引，属净退化。

**做法**：在 `_public_error` 开头加一个分支——异常对象 `getattr(exc, "public_message",
False)` 为真时，返回 `str(exc)` 截断 500 字符；其余逻辑不变（供应商响应体保密的既有
语义就在"默认 False"里，勿改动 `MaterialProviderError` 本身）。`HandlerError` 类
不迁移（无其他使用者）。

### 3.4 数据库迁移

**先快照**（宿主机没有 pg_dump，用容器内的）：

```bash
mkdir -p ~/openbox-db-snapshots
docker exec openbox-postgres-1 pg_dump -U openbox openbox \
  -t skill_jobs -t skill_job_attempts -t skill_job_events -t skill_job_inputs \
  -t skill_job_artifacts -t session_inbox -t user_skill_settings \
  > ~/openbox-db-snapshots/skill-runtime-tables-$(date +%Y%m%d-%H%M).sql
```

（若 `-U openbox` 认证失败，连接串在 `backend/scripts/backend_entrypoint.py` 的
`LOCAL_DATABASE_URL`：`openbox:openbox_dev@localhost:5432/openbox`。）

**再迁移**：`backend/db/migrations/versions/` 新增一个 revision（**动手前先
`uv run alembic heads` 确认单 head**——本仓库出过多 head 事故）：

- `upgrade()` 按依赖序 drop：`session_inbox` → `skill_job_artifacts` →
  `skill_job_inputs` → `skill_job_events` → `skill_job_attempts` → `skill_jobs` →
  `user_skill_settings`；
- `downgrade()` 从 `a2c4e6f8b0d1_add_skill_job_runtime.py` 原样复制七张表的建表
  代码（结构可回滚；数据靠上面的快照）。
- 相关旧迁移文件（`a2c4e6f8b0d1`、`c5e7f9a1b3d5_skill_job_policy_snapshot`、
  `e3a5c7d9f1b2_job_continue_agent_on_success`）**保留不动**——迁移历史不可改写。

单用户 SQLite 模式建表走 ORM `create_all`，模型删除即自然不再建；存量文件里的孤表
无害，不处理。

### 3.5 测试：删除清单

整文件删除（合计 137 用例）：`test_skill_job_outbox.py`、`test_skill_job_receipt.py`、
`test_skill_job_repository.py`、`test_skill_job_tool.py`、`test_skill_job_transitions.py`、
`test_skill_job_worker.py`、`test_skill_manifest.py`、`test_job_continuation.py`、
`test_job_failure_report.py`、`test_video_skill_handlers.py`。

### 3.6 测试：先移植后删（丢了算净退化）

1. **新建 `backend/tests/unit/test_video_error_text.py`**，从
   `test_job_failure_report.py` 移植"错误文本卫生"四例，对准 `_public_error`：
   - 普通异常（如 `RuntimeError("token=sk-live …")`）→ 输出不含原文，只有类名；
   - 带 `response` 属性的 HTTP 异常 → `HTTP {code}: {reason}`（既有行为回归）；
   - `MaterialProviderError(..., public=True)` → **原文外显**（§3.3 的验收）；
   - `MaterialProviderError`（默认）→ 原文保密。
   变异检查：注释掉 §3.3 新分支，第 3 例必须变红。
2. **`test_video_model_snapshot.py`**：删除 `test_receipt_summary_hides_identifiers`
   一个用例（其 `:275` `from skill_runtime.receipt import _summary`），其余全保留。

### 3.7 PR#1 Definition of Done

```bash
cd backend
grep -rn "skill_runtime\|builtin_skills" --include='*.py' . | grep -v .venv   # 必须零命中
uv run alembic heads                                                          # 单 head
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run pytest -q        # 全绿；预期 ≈ 1016 − 137 + 新增（以实测为准，记入 commit message）
```

后端冷启动（§0.3 命令）日志无 `skill_runtime` / worker / inbox 残留；`/api/jobs`
类路由 404。**手动重启后端**（openbox.json 已改，reload 不管它）。

---

## 4. PR#2 前端与移动端移除

### 4.1 frontend-v2

**删除**：`src/features/jobs/` 下 `components/SkillJobsDock.tsx`、
`components/SkillJobCard.tsx`（含 `.test.ts`）、`hooks/useSkillJobLiveEvents.ts`、
`api/jobs.ts`、`api/keys.ts`；`src/routes/workspace/ChatRoute.tsx` 中 dock 的 import
与"后台任务"区块接线（§4.2：接线都在 routes，正好只此一处）。

**保留**：`features/chat/components/SkillJobReceipts.tsx`（§2 不动区）。它依赖
`shared/types/api` 的 `SkillJobPart` 类型——保留该类型。若它引用了
`features/jobs/types` 里的东西，把被引部分**移入 `shared/types`** 再删源
（§4.1 禁止跨 feature import，不许留反向依赖）。`features/jobs/` 目录若因此清空
则整目录删除。

**locale**：`src/locales/{zh-CN,en-US}/jobs.json` 裁剪到回执所需——实测回执只用
`status.succeeded` / `status.failed` / `status.cancelled` 三键（`t(tone.labelKey,
{defaultValue: part.status})`，缺键可降级但不许裁出缺键）。

### 4.2 mobile（对照 web）

**删除**：`lib/features/jobs/api/jobs_api.dart`、`widgets/skill_job_card.dart`、
`widgets/skill_jobs_dock.dart`；`lib/app/router.dart` 与
`lib/features/chat/chat_screen.dart` 中的接线。

**保留**：`lib/features/chat/widgets/cards/skill_job_receipt.dart`、
`lib/shared/models/skill_job.dart`（回执 part 模型；若模型文件里混有 live-job 专用
字段类，删除未被回执引用的部分）。

**locale**：`mobile/assets/locales/{zh-CN,en-US}/jobs.json` 与 web 侧**逐字节一致**。

### 4.3 PR#2 Definition of Done

```bash
cd frontend-v2 && npm run check && npm run lint && npm run test && npm run check:i18n
cd ../mobile && dart analyze
for l in zh-CN en-US; do diff ../frontend-v2/src/locales/$l/jobs.json assets/locales/$l/jobs.json; done  # 空输出
```

lint 错误数 ≤ 2（既有 `content-view.ts` 两处）；单文件 ≤ 800 行（mobile）。
浏览器验收场景 A、B（§7）。

---

## 5. 已定决策（不再开口子）

| 决策 | 定论 | 依据 |
|---|---|---|
| `session_inbox` 表 | 随七表删除 | 未来"完成唤醒"薄层（provider 看护 → 注入会话消息）另行从零设计，不复用九态机附属物 |
| demo_echo | 删 | 纯运行时参考件 |
| `api/skill_settings.py` | 删 | 前端零调用 |
| 回执渲染器 | 保留（只读历史件） | 老会话数据完整性 |
| 运行时代码"归档分支" | 不做 | git 历史即归档（`1d453a0` 之前俱在） |
| operator-input 端点 | 不迁移 | 直连路径失败当场可见，无停靠概念 |
| `ws.py` 清理门 | 直接删 skill-job 判定，不写替代 | `_has_active_agent_sessions` 已并列存在且语义正确 |

---

## 6. PR#3 契约强化（M2）

### 6.1 等待语义统一

现状不对称（实测）：`VideoTranscribeArgs` / `VideoRenderArgs` 有
`wait_iteration` + `after_version`，`VideoGenerateArgs`（`tool/video_production.py:42`）
只有裸 `wait`。统一为三段式：

1. `submit` 幂等提交返回句柄（已有，勿动）；
2. `wait` 单次有界：给 `video_generate` 补 `wait_iteration`（必填于 wait 动作）与
   `after_version`（防拿旧态），语义抄 transcribe 的现行实现；超时返回
   `still_running` + 状态快照，不报错；
3. `.openbox/skills/video-production/SKILL.md` 步骤 6 的等待表述随 schema 核对更新
   （改 SKILL.md 后重启后端才生效）。

### 6.2 status 完整性——"仅凭 status 恢复"锚点测试

新增 `backend/tests/unit/test_status_is_the_recovery_contract.py`：构造
"分段已批、第 1 段已生成、第 2 段提交中"的库状态，然后**只**调
`video_project(action="status")`，断言返回值足以重建：当前阶段、每段状态与
`generation_job_id`、五类审批及哈希匹配性、三类幂等键、冻结的模型、花费余量
（`max_calls` − `used_calls`）。缺字段补字段（改 `tool/video_workflow.py` 的 status
组装处）。此测试今后是 status 字段的回归锚——**它存在的意义写进测试 docstring**。

### 6.3 PR#3 Definition of Done

后端套件全绿 + 两处变异检查（破坏 `_public_error` public 分支、删除 status 任一新增
字段，各须有用例变红）。浏览器验收场景 C（§7）。

**2026-08-30 复核：两处变异检查均已实测闭环。**
删 `_public_error` 的 `public_message` 分支 → `test_video_error_text.py::
test_public_material_error_exposes_the_actionable_message` 变红；
从 status 元数据删 `spend_budget`（`tool/video_workflow.py:843`）→
`test_status_is_the_recovery_contract.py` 变红。两次均在验证后立即还原，代码未留改动。

---

## 7. 浏览器验收场景（PR#2 后跑 A/B，PR#3 后跑 C）

前置：后端（8080）与前端（3000）都在跑；用 `qa_jobs` 登录，
密码见 `docs/LOCAL_CREDENTIALS.md`（该文件 gitignored，只在本机）。

- **A 历史回执**：打开
  `http://localhost:3000/app/s/session_7YBYRVD9SKGYEGNXHXHCXDXPG8`（"魔仙堡"会话，
  含耐久时代的回执与已取消作业记录）。验收：聊天流中回执 chip 正常渲染；
  **不再出现**"后台任务"区块；浏览器控制台零报错。
  **2026-08-30 实测通过**：3 条 `video-production · segment.generate` 回执均显示完成，
  3 个视频均加载到 `readyState=4`（约 5.04 秒、默认静音、带播放控件）；"后台任务"
  零命中，控制台无 warning/error。
- **B 直连全流程（零花费）**：新会话发送——"使用视频制作技能：创建一个竖屏测试项目，
  主题《清理验收》，写好完整台词并发起剧本审批。做到剧本审批为止，不要设计分段，
  不要生成任何视频。"验收：技能加载、`video_project` create/set_script/
  request_approval 全部**回合内直连**工具调用；剧本审批卡弹出；点"跳过"收尾；
  全程无作业卡。（注意：模型可能中途结束回合，任务清单显示"已中断"属正常，
  发"继续"即恢复——这是 todo 生命周期的既有行为，不是 bug。）
  **2026-08-30 实测通过**：会话 `session_7YBYQDX9GZ3DY2PN5NBCZE5VTX` 在同一回合
  完成 skill load 与 `video_project` create/set_script/request_approval/status；审批卡弹出并
  以"跳过"收尾。项目 `production_01M18J3HFP297ZMKWX0DMYXZN8` 最终为
  `needs_script_approval`，数据库复核 segments/jobs/approvals 均为 0；无 live job UI、
  `skill_job`、`video_generate` 或 `segment.generate`，控制台无 warning/error。后端冷重启后
  A/B 再次复测仍通过；同时确认 legacy provider route mismatch 只隔离、不请求当前 relay、
  不改写原任务事实。后续独立审查进一步收紧为：凡历史任务缺完整 route fingerprint，
  即使 wire 相同也默认不可验证并隔离，避免 endpoint 或供应商账号轮换后误查旧 task id。
- **C 恢复力**：场景 B 走到审批卡后批准并继续到某一段 `wait` 期间，重启后端进程，
  然后发"继续"。验收：agent 调 `status` 重建事实并接续，不重复提交（幂等键拒绝
  同键不同哈希）。⚠️ 此场景会产生**真实付费生成**——执行前向用户确认预算；
  用户此前接受过小额测试花费，但每次都要重新确认。
  **2026-08-30 用户决定：付费版不执行，本清理任务以下述零费用替代验收结项。**
  即：真实供应商链路（真实网络延迟、真实计费、供应商侧异常）在本轮**未被验证**，
  这是一个已知且被接受的证据缺口，不是遗漏。将来若视频恢复路径出现线上问题，
  此处是第一个该补的实验。
  零费用自动化已另用 loopback provider 和两个独立进程覆盖 submit→冷重启→恢复前同键
  抢跑→startup recovery→completed→完成后同键重放，确认供应商 POST、调用预算和 attempt
  始终各为 1；它补强核心恢复证据，但不冒充浏览器/真实供应商的付费场景 C。
  **2026-08-30 零费用浏览器替代验收通过**：`qa_jobs` 会话
  `session_7YBYQ7PBN0VR3V1QDQ57RZWTTG` 创建并人工批准单段项目
  `production_01M18RAKAMT1NC2FAK9HRBTKJY`，模型显式固定为 `video-sd-720p-proⅠ`；测试时把
  视频路由锁到 `127.0.0.1` loopback（真实供应商不可达），浏览器只提交一次后强制重启
  后端。跨过 120 秒活跃轮询保护窗后，后台恢复用已保存的
  `provider_task_id=task_browser_restart_1` 查询；随后浏览器按返回的 `version` 与
  `wait_iteration` 继续同一 job。最终 mock 精确为 `POST=1 / GET=3 / unexpected=0`，数据库
  只有一个 job，`attempt=1`、预算 `used_calls=1/max_calls=1`，没有重提。mock 再返回预期
  failed 终态以清理作业；页面无 live job UI，控制台无 warning/error。此证据覆盖真实
  浏览器与进程重启链路，但仍不声称完成了真实供应商付费生成。

---

## 8. 已知坑（本手册作者亲踩，按出现概率排序）

1. `conftest.py` 旗标 fixture 与 config 属性必须同一提交删除，否则全场
   `AttributeError`（§3.2 第 8 条）。
2. `uvicorn --reload` 不看 `openbox.json` / `skill.yaml` / locale——改这些必须手动
   重启后端，否则你会对着旧行为 debug。
3. 宿主机没有 `pg_dump`，用 `docker exec openbox-postgres-1 pg_dump ...`。
4. alembic 曾出多 head 事故：建迁移前 `uv run alembic heads` 必须单 head。
5. 前端 lint 有 2 个既有错误（`content-view.ts`）——不是你的问题，也不许变成 3 个。
6. locale 逐字节同步是硬门：web 与 mobile 的同名 json 必须 `diff` 为空。
7. `skill_installs` / `user_skills` / `kv_store` 与运行时表同族异名，**不许删**（§2）。
8. `test_video_model_snapshot.py` 整体是共享层测试，只删一个用例（§3.6.2）。
9. 提交信息用中文、讲 why；每个 PR 独立提交并 push。
10. **`origin` 是公开仓库**。凭据一律只进 `docs/LOCAL_CREDENTIALS.md`（gitignored）
    或已忽略的 `.env` / `backend/openbox.json`；`git add -A` 之前扫一眼
    `git status --short`，确认没有把本地凭据文件或 `openbox.json` 带进暂存区。

---

## 9. Backlog（本手册明确不做）

1. **完成唤醒薄层**：provider 任务看护 + 完成时注入会话续跑消息（Claude Code 形态）。
   等产品需要"关页面也继续"再立项，按"一根线"而非"一个运行时"评估。
2. v1 前端淘汰（用户另行处理）。
3. `qa_jobs` 测试账号清理（其密码曾出现在对话记录；环境对外前删号或改密）。
4. ~~将旧运行时规划移入 `docs/archive/` 并加墓碑头。~~ 已在 **PR#4** 完成；墓碑
   指向移除提交 `4d93463` 与本文件，所有现行引用均改为归档路径。
