# D1 · ECD 浏览器调试监控（方案 v1，2026-09-07；L0–L2 + L4 已实现，见 §7）

## 0. 一句话

出问题时不用再 `aliyun ecd run-command` 手工上桌面翻日志：后台一眼看到
**哪台桌面 / 哪个会话 / 哪个工具调用 / 卡在哪一步 / 为什么**，并能一键拉桌面诊断包。

## 1. 现状（实查，勿重查）

链路：agent 工具 → `SandboxClient.execute`（`backend/sandbox/client.py:395`，带
`X-OpenBox-Instance/Request/Session/Tool-Call/Operation/Desktop-Lease` 头）→
隧道或直连 → 桌面 `action_server /execute` → shell → `curl 127.0.0.1:9333|9222` / `npx tsx`。

已有的基础：
- 桌面端 `_emit_execute_trace`（`container/action_server.py:246`）已把每次 execute 以 JSON
  写进 journald（`uvicorn.error`），字段含 session/tool_call/operation/kind/command_sha/耗时/退出码。
- `repair_browser_runtime.py --check` 输出结构化 `problems[]`（`backend/sandbox/browser_runtime_repair.py:419`）。
- `cloud_desktops.channel_error/last_seen_at`、`fleet_alerts`、`audit_logs`、`/api/admin/fleet`、
  Fleet 页（`frontend-v2/src/features/admin/FleetPage.tsx`）。

三个结构性盲区：
1. **页面级操作不可见**。goto/click/screenshot 是模型写的 tsx，在桌面上以一条 `POST /execute`
   跑，后端只看到一个 shell 串的 sha。relay 在 local 模式只回 `webSocketDebuggerUrl`，Playwright
   直连 9333，relay 也看不到。
2. **关联单向**。`X-OpenBox-Request` 每次 HTTP 调用新生成、只发到桌面、不回显；后端日志与桌面
   trace 只能靠时间猜着对。
3. **错误被吞**。`_curl_json`（`browser.py:124`）任何失败都是 `None`；`_probe_chrome` 把 stderr
   里的原因丢掉；`verified_result` 丢 `problems[]`；`ensure_browser_runtime` 安装失败只留一句
   「去桌面看 npm-install.log」；`skill_tool.py:244`/`computer.py:474` 把异常截到 200/300 字；
   `api/dev_browser.py` 五处 `except: pass`；`channel.verify` 循环只留最后一次 `last_error`。
4. **后端从不主动读桌面日志**。唯一一处 `_log_tail`（`browser.py:233`）只在就绪轮询超时时
   把 40 行塞进异常字符串。`journalctl` 在运行时零调用。

## 2. 原则

- 复用 `X-OpenBox-*` 头体系，**以 `tool_call_id` 为主关联键**；补 request id 回显。
- **不记命令原文**（可能含密钥），沿用 sha；诊断包与事件只对 admin 开放。
- 桌面侧**零新常驻进程**：诊断 = 一个只读脚本 + action server 一个只读端点，不加 systemd 单元
  （与 `WUYING_SANDBOX.md` 「relay 不做单元」的精神一致）。
- 每段采集独立 try，**永不整体失败**；生产 legacy 桌面文件系统只读，诊断不写 `/opt`。
- 不上 OpenTelemetry / Prometheus 全家桶：当前一台 gw2，成本不值；先把「事件 + 快照」落库。

## 3. 分层

### L0 止血：别吞错（≈0.5 天）

| 位置 | 改法 |
|---|---|
| `browser.py::_curl_json` | 返回 `Probe(ok, stage=transport\|http\|parse, exit_code, stderr_tail, ms)`；调用方仍可按假值用 |
| `browser.py::_probe_chrome` | 保留探针 stderr 的 reason，进异常与事件 |
| `browser_runtime.py::verified_result` | 异常携带 `problems[]` 与 `version` |
| `browser_runtime.py::ensure_browser_runtime` | 安装失败带 stdout/stderr 尾 40 行 |
| `browser.py:809` auto/extension 回落 | 记下 `RelayUnavailable` 的原因再回落 local |
| `skill_tool.py:244` / `computer.py:474` | 截断改为「首行 + `[diag:<event_id>]`」，全文进事件表 |
| `api/dev_browser.py` 五处 `pass` | 至少 `log.warning` + 发 `devbrowser.status` 事件（常量已声明从未发布） |
| `channel.verify` | 每次尝试的失败都记事件，不只留最后一条 |
| action server | 响应头回显 `X-OpenBox-Request`；`ExecuteResponse` 加可选 `trace:{request,duration_ms}`；`/dev-browser/*`、lease acquire/release 也发 trace 行 |
| action server `_desktop_command_kind` | 新增 `browser_script`（识别 `npx tsx` / dev-browser） |

### L1 桌面诊断包（≈1 天）

新文件 `container/obx_diag.py`，随 `repair_browser_runtime.py` 同一套机制分发到
`/opt/openbox/tools/`（版本钉进 `RUNTIME_VERSION`），`--json` 输出：

```
chrome   pid/uid/启动时间/RSS/线程数、argv 白名单（--user-data-dir --remote-debugging-port --headless --display）、
         /json/version、/json 目标列表（url 截 120、不带 title）、profile 下 DevToolsActivePort 是否存在
relay    :9222 GET 结果、/tmp/obx-relay.pid 与进程存活、mode
x        obx-x 能否拿到 DISPLAY/XAUTHORITY、xrandr 当前尺寸、obx-display-guard 状态、
         /var/log/wuying/asp/linux-streaming-manager-* 最后一行 "channel clients is N"（有没有人在看画面）
unit     openbox-action-server: TasksMax/TasksCurrent/MemoryCurrent/NRestarts/drop-ins；openbox-tunnel 状态；
         openbox-browser-runtime 上次结果
runtime  直接 import repair_browser_runtime.runtime_problems → problems[] + version
logs     /tmp/obx-chrome.log、obx-relay.log、obx-ibus.log 各尾 60 行；
         journalctl -u openbox-action-server 最近 200 行只筛 execute_trace（可按 session 过滤）；
         /tmp/obx-browser-ops.jsonl 尾 100 行（L3）
disk     /workspace、/tmp 使用率；node/npm 版本；/opt/openbox/backups 数量
errors[] 每段采集失败的原因
```

- action server：`GET /diag/browser?session=&lines=`（API key 必须，不需 lease，只读）。
- 后端 `sandbox/diag.py::collect_browser_diag(desktop_id)`：隧道通走 action server；
  不通走 `channel.run_desktop_command`（云助手，root）。两条路输出同一 JSON。
- 脚本本身可用 `aliyun ecd run-command` 直接跑，隧道断了照样诊断。

### L2 事件时间线（≈1–1.5 天）

新表 `desktop_events`：

```
id, ts, desktop_id, session_id, tool_call_id, request_id,
kind, phase, status(ok|fail|timeout), duration_ms,
summary(≤300), detail JSONB(≤32KB), diag_ref
```

`kind` 枚举：`browser.ensure` `browser.chrome_launch` `browser.relay_start` `browser.probe`
`browser.runtime_check` `browser.runtime_repair` `channel.verify` `lease.acquire`
`desktop.lifecycle`。

- `sandbox/events.py::emit()` best-effort，同 `audit.record` 模式；`tool_call_id` 从
  `SandboxClient._trace` contextvar 取。
- **失败自动附诊断**：`ensure_browser` / `channel.verify` 失败时自动采 L1 诊断包存 `detail`
  （同桌面 5 分钟限频）。
- 30 天保留，清理挂 `cron/internal_tasks`。

### L3 页面级操作可见（≈1 天，成本点见 §6）

改 `container/dev-browser/src/client.ts` 的库层（模型写的脚本不变）：
- `page()` / 新建 page 时挂 `framenavigated`（主 frame）、`crash`、`pageerror`、主文档
  `requestfailed` 监听，append JSONL 到 `/tmp/obx-browser-ops.jsonl`，每行 ≤500B，>5MB 轮转 `.1`。
- 行内带 `OPENBOX_TOOL_CALL`/`OPENBOX_SESSION` 环境变量——由 action server `_exec_env` 从
  `X-OpenBox-Tool-Call/Session` 头注入。
- 时间线按 `tool_call_id` 把后端事件与桌面 ops 拼在一起。

### L4 展示（≈1 天）

- Fleet 页桌面行加「诊断」抽屉：五灯状态卡（chrome/relay/x/unit/runtime）、最近事件时间线、
  日志尾、「重新采集」按钮。
- API：`GET /api/admin/fleet/desktops/{id}/diag`、`GET .../events?session=&limit=`、
  `POST .../diag/collect`。
- 设置页浏览器状态（`BrowserPage.tsx:51`）显示结构化 reason（stage），不再只有可用/不可用。
- 聊天侧工具错误末尾带 `[diag:<event_id>]`，admin 可凭 id 直查。

### L5 告警（后置）

fleet 规则加 `browser_flapping`（1h 内 chrome_launch ≥3）、`tasks_near_limit`
（TasksCurrent/TasksMax >0.8）、`relay_down_while_assigned`。数据来源：`fleet_snapshot`
任务对 assigned 桌面每 10 分钟顺带采一次只读诊断。

## 4. 验收

1. kill 桌面 Chrome 后触发一次 computer/skill 工具：聊天错误带 event id；Fleet 抽屉能看到
   `browser.ensure fail` 事件 + chrome.log 尾 + problems；全程不登录桌面。
2. 隧道断开时 `POST diag/collect` 走云助手仍出包。
3. 一次 dev-browser 会话后，时间线里按 tool_call_id 能看到 navigate 事件（L3）。
4. 生产 legacy 桌面（只读 FS）上诊断只读，不写 `/opt`，退出码 0。
5. execute 响应头带 `X-OpenBox-Request`，旧客户端不受影响。
6. `grep` 验证：命令原文不出现在任何表、日志、诊断包。
7. 单元测试：`_curl_json` 三种失败 stage、`verified_result` 带 problems、`obx_diag` 每段失败不影响整体。

## 5. 顺序

L0 + L1 一起（收益最大、无迁移）→ L2 → L4 → L3（视需要）→ L5。

## 6. 待拍板

- **谁做**：我直接做，还是按惯例写执行单交 Codex。
- **L3 要不要**：改 `client.ts` 会变更 dev-browser 源包 → `RUNTIME_VERSION` 升级 → 全池机
  各 repair 一次（12 台 prewarm + 在用机）。收益是页面级可见性，成本是一轮舰队修复。
- **保留期与大小**：事件 30 天、detail 32KB 是建议值。
- **权限**：诊断端点只给 admin（建议），workspace owner 不开。

## 7. 实施记录（2026-09-07，分支 `d1-ecd-browser-debug`）

**已做（L0 + L1）**

- `sandbox/browser.py`：`_probe_url` 返回 `Probe(stage=transport|connect|http|parse, detail, status, ms)`；
  `_probe_chrome_detailed` 保留渲染器探针的 stderr 原因；`ChromeUnavailable`/`RelayUnavailable`
  带「last probe」原因；`_log_tail` 失败时说明为什么；`browser_status` 多 `problems`；
  auto/extension 回落带 `fallback_reason`；`ensure_browser` 失败自动 `capture_failure`。
- `sandbox/browser_runtime.py`：异常携带 `problems[]`/`output`；版本不符与校验失败分开报；
  安装失败带安装器尾行；云助手失败带原始异常。`RUNTIME_VERSION` → `20260907.5`，
  `runtime_files()` 多发 `obx_diag.py`。
- `sandbox/diag.py`（新）：内联发送采集脚本（gzip+b85，<16KiB，云助手可用）、解析、
  进程内最近 50 条环形缓存、`capture_failure`（同桌面 5 分钟限频、把 `diag_id` 挂到异常上）、
  `summarize_error`（首行 + `[diag:id]`）。
- `container/obx_diag.py`（新）：stdlib、Py3.10、每段独立 try、只读；`summary.lights` 五灯 +
  `findings` 一句话结论（stale SingletonLock、profile 属主不符、TasksCurrent/TasksMax、
  NRestarts、tunnel 状态、分辨率、runtime problems）。
- `container/action_server.py`：版本 `2026.09.07-browser-diag-v1`，capability `browser_diag_v1`；
  所有响应回显 `X-OpenBox-Request`；`ExecuteResponse.trace`；命令分类新增
  `browser_probe/browser_diag/browser_launch/browser_relay/browser_runtime/browser_script`，
  只有会动桌面的分类才要 lease（探针/诊断不再被 423）；`GET /diag/browser`；
  `/dev-browser/*` 记 trace。
- `sandbox/client.py`：`ExecuteResult.request_id`。
- `sandbox/channel.py`：verify 每次尝试都记日志；浏览器就绪失败自动采集并在 `channel_error` 尾加 `[diag:id]`。
- `tool/skill_tool.py`、`tool/computer.py`：错误改为「首行 + `[diag:id]`」，不再硬截 200/300 字。
- `api/browser.py`：`reason` 区分 `unreachable`/`not_started` 并带 `detail`；`api/dev_browser.py`
  五处 `pass` 改为带用户与原因的日志；`tool/browser_mode.py` 报出 Chrome 不可达原因。
- `api/admin_fleet.py`：`GET /diag/recent`、`GET /diag/{id}`、`POST /desktops/{id}/diag`（auto/channel/cloud，审计 `desktop.diag`）。
- 测试：新增 `test_obx_diag.py`、`test_sandbox_diag.py`、`test_browser_probe.py`，扩展
  action server 测试；相关 108 条通过。全量 unit 另有 25 条失败在 main 上同样失败（与本次无关）。

**上线需要**

1. 后端镜像照常发布。
2. action server 需重发到各桌面（`backend/scripts/wuying_deploy_action_server.py`）——
   不重发时诊断走内联脚本仍可用，只是没有 `/diag/browser`、请求 id 回显和新分类。
3. `RUNTIME_VERSION` 升级会让每台桌面在下次浏览器使用/激活时跑一次 repair（无 npm、几秒）。

**L2 已做（同分支，第二个提交）**

- 表 `desktop_events`（migration `b6d1e2f3a4b5`，已加入 `_READINESS_SCHEMA`）：
  ts / desktop_id / container_key / session_id / tool_call_id / request_id / kind / status /
  duration_ms / summary / detail(JSONB, ≤48KB 自动裁剪) / diag_id。
- `sandbox/events.py`：`emit()` best-effort 永不抛；`span()` 计时并记 ok/fail/timeout，异常上的
  `diag_id` 自动带入；`list_events`/`get`/`purge`（30 天，内部任务 `desktop_events_purge` 每 6h）。
  session/tool_call 从 `SandboxClient._trace` 自动取；`SandboxClient(desktop_id=…)` 新增标签字段，
  manager 对 `ecd-*` 容器自动填。
- 埋点：`browser.ensure`（含 requested→effective、presentation、fallback_reason）、
  `browser.chrome_launch`、`browser.relay_start`、`browser.runtime_check`（只记失败）、
  `browser.runtime_repair`、`channel.verify`（attempts/boot_recovery/最后错误）、
  `lease.acquire`（只记等待 ≥2s 或 423 被拒）、`browser.diag`（快照本体存 detail.report）。
- `sandbox/diag.py` 改为落库：`[diag:<id>]` 现在就是事件 id（`dev_…`）；库不可用时退回进程内
  `mem_…` 缓存。admin API：`GET /api/admin/fleet/events?desktop_id=&session=&kind=&status=`、
  `GET /events/{id}`、`GET /desktops/{id}/events`；`diag/recent` 支持 `desktop_id` 过滤。

**L4 已做（同分支，第三个提交）**

- Fleet 页每台桌面多一个「诊断」按钮 → 右侧抽屉 `DesktopDiagDrawer`：最近快照的五灯 + 结论 +
  采集失败段落、Chrome/Relay/journal 日志尾（折叠）、多快照选择、「重新采集」（auto：通道优先，
  回落云助手，并显示回落原因）、时间线（kind/status/耗时/摘要/`diag:<id>`，点开看 session /
  tool_call / request id）。ESC 关闭。
- 设置页浏览器状态：不可用时显示原因（桌面不可达 / Chrome 未运行 / 没有桌面 / 检查失败）与探针原话。
- hooks：`useDesktopEvents`、`useDesktopDiags`、`useDiag`、`useCollectDiag`；i18n 键 `admin.diag.*`、
  `settings.browser.status.reason.*`（中英）。测试 `DesktopDiagDrawer.test.tsx` 4 条。
- 未做真机 UI 验证（需要 admin 登录 + 有事件数据的后端）；上线后在 gw2 Fleet 页点一台机器即可验。

**未做 / 下一步**：L3 页面级 ops（改 `client.ts`，需再升 RUNTIME_VERSION）、L5 告警。

## 8. 上线记录（2026-09-08 凌晨）

- main 已包含 D1 全部提交（2620fcd → cf5eb1d → 35707a8 → 7d59c16 → b569448 → 94b2a98）。
- AWS：`20260907-d1-94b2a98`。gw2：见 DEPLOY.md 当前发布节。15 台桌面 action server 已更新。
- 埋点首战：上线 10 分钟内在 gw2 时间线上抓到 RUNTIME_VERSION 触发的修复崩溃，在 AWS 共享桌面上
  用 `[diag:id]` 记录 + 日志尾 + 单元信息定位到 XAUTHORITY 与 `protected_regular` 两个根因，全程没有
  手工登录桌面翻日志（云助手只用来确认 sysctl 和文件属主）。
- 遗留：镜像缺 `container/dev-browser`（构建上下文问题）；L3/L5 未做。

## 9. 「阿里出问题能不能在 AWS 复现」

同一套代码、同一套埋点、同一格式的快照，两边各自的 `desktop_events` 表。做法：在 gw2 Fleet 页把出事
桌面的快照和时间线拿到（五灯 + findings + 日志尾 + 事件序列），再在 AWS 的共享桌面上用同样的操作
（或直接 `POST /api/admin/fleet/desktops/{id}/diag`）取一份对照，逐段比 `summary.findings`、`units`、
`chrome.profiles`、`x`。能复现的是**代码路径和桌面配置类**问题（本次的 XAUTHORITY、protected_regular、
runtime 版本都属此类）；复现不了的是**per-user 池机特有**的状态（用户 profile、bossip 旧镜像遗留、
TasksMax drop-in 顺序），因为 AWS 只有一台共享桌面、shared 模式。这类就直接用 gw2 的快照读，
不必复现。
