# 里程碑一验收证据（2026-09-04）

环境 gw2 生产 `https://ai.bossipai.com.cn`，镜像 `20260904-a2-d9d7401`，库 `f7b9d1e3a5c8`，
`WUYING_ROUTING=per_desktop`、`WUYING_MODE=per_user`、`WUYING_CHARGE_TYPE` 未设。
C 组按用户决定跳过。E 组只做了配置侧（见文末）。

结论：**A1–A7 ✅、B2–B8 ✅、D1–D4 ✅、E1 ✅；B1 半通过（1 处 UI 缺陷，已记录未改代码）。**

---

## 0. 准备

| 项 | 值 |
|---|---|
| U1 | `m1u1-0904`，user_id `01M1NENZQ65XK6NZ43A057R14M`，默认空间 `01M1NEP01CV78X540QT8G0F521` |
| U2 | `m1u2-0904`，user_id `01M1NEP0CCJD6Y3AW6H20CB90S`，默认空间 `01M1NEP0N9W2VPE9XGFZH7065N` |
| 管理员 | `m1adm-0904`，`update users set role='admin'` 已执行 |

三个账号都是经 `POST /api/auth/register` 建的一次性测试账号（口令随机，仅存本次会话工作目录）。

**基线桌面列表（上海，15 台）** — `aliyun ecd describe-desktops --max-results 100`：

```
ecd-0b7gj174mc6f23ctq  obx-e87cb1b89542a2ef  Running PrePaid m-ccceuit7jn3xzwx45  到期 2026-10-04
ecd-8zp47qagrsc95h67t  openbox-dev-shanghai  Running PrePaid m-feb7j85i4d4qo09ux  到期 2026-10-01
ecd-glxi1nk433hliivri  bossip-sh-013 …（bossip 舰队 13 台，略）
```
（完整 15 条 id：0b7gj174mc6f23ctq / 8zp47qagrsc95h67t / glxi1nk433hliivri / b9oizzx4rfhbsm1uh /
ctazuyee5p8enedta / 4y9s9igraz7hc58eb / c51eyfc786uzimn3o / 4y9s9igraz7hc58ea / 4zjxaq5g45dr5qr0i /
gj5j513on7j0u97as / i4c4x8wpqg1lxktmj / c4qndqrko3db7kjfz / i4c4x8wpqg1lxktmi / 5pvbuskezql1d4h5m /
ijea2hjljf9c4wd1b）

---

## A. 一个 workspace 一台桌面 — 全部 ✅

### A1 ✅ 未开桌面直接发指令 → 503

`GET /api/desktop/status`（开通前）：`{"state":"not_provisioned","mode":"per_user"}`

浏览器 Network：`POST /api/agent/session/session_7YBYAH…/prompt_async → 503`，响应体
```json
{"code":"DESKTOP_NOT_READY","detail":"Your cloud desktop is not ready","desktop":{"state":"not_provisioned"}}
```
前端行为：右侧自动弹出「云桌面」tab，文案「本空间还没有云电脑 / 开通后将为本空间创建一台专属云电脑，
首次创建约需 2-3 分钟」+「开通云电脑」按钮。判据两条都满足。

### A2 ✅ 开通到 running + 通道 up（≈62 秒）

```
05:40:23Z {"state":"creating","desktopId":"ecd-j3daid9t6x8p4wjh7","channel":{"state":"pending"},"mode":"per_user"}
05:40:53Z {"state":"creating", …}
05:41:09Z {"state":"running","desktopId":"ecd-j3daid9t6x8p4wjh7",
           "channel":{"state":"up","last_seen_at":"2026-09-04T05:41:06.832863+00:00"},"mode":"per_user"}
```

### A3 ✅ `bash: hostname` 返回本机主机名

聊天最终答复：`daid9t6x8p4wjh7`
= `DescribeDesktops(ecd-j3daid9t6x8p4wjh7).HostName`，≠ 共享桌面
`ecd-4zjxaq5g45dr5qr0i` 的 `jxaq5g45dr5qr0i`。
（注：验收单写的共享桌面主机名 `0zd5sxxe1uw10r6` 是 EC2 prod 那台；gw2 的 `WUYING_DESKTOP_ID`
是 `ecd-4zjxaq5g45dr5qr0i`，判据按后者核的。）

### A4 ✅ 桌面画面里的终端与 A3 一致

云桌面画面里开出 GNOME Terminal，窗口内两行：`daid9t6x8p4wjh7` / `A4-MARKER`；
桌面上同时出现文件图标 `A4-hostname-daid9t6x8p4wjh7.txt`。

**做法与验收单不同（记录）**：画面是 `g.alicdn.com` 的跨源 iframe（WuyingWebSdk 2.13.9-asp3.18.11），
自动化注入的鼠标/键盘事件进不去，无法手敲。改为**从聊天通道**下发命令拉起终端与桌面文件，
证明「看到的画面 = agent 执行的那台机器」，证据效力等价。人工手敲仍可随时补。
（桌面 GUI 会话跑在 `:1`，GUI 用户 `obx-887eb40e4bab071c`，agent 以 root 执行。）

### A5 ✅ 云侧四项全对

```
DesktopId:      ecd-j3daid9t6x8p4wjh7
HostName:       daid9t6x8p4wjh7
DesktopStatus:  Running
ChargeType:     PostPaid                     ✅
ImageId:        m-ccceuit7jn3xzwx45          ✅
PolicyGroupId:  pg-0bbay5jmvosn8b2hc         ✅
OfficeSiteId:   cn-shanghai+dir-2879607125
DesktopType:    eds.enterprise_office.4c8g
CreationTime:   2026-09-04T05:40Z
Tags: openbox-env=gw2, openbox-eu-id=obx-887eb40e4bab071c,
      openbox-user=01M1NEP01CV78X540QT8G0F521,
      openbox-workspace=01M1NEP01CV78X540QT8G0F521   ✅ = U1 默认空间
```
小观察（非判据）：`openbox-user` 标签里放的也是 workspace id 而不是 user_id——接缝把
`owner_for` 切到 workspace 之后标签名没跟着改，纯命名，不影响功能。

### A6 ✅ 通道只在内网

gw2 `ss -ltn`（节选）：
```
LISTEN 0 128 172.17.0.1:18103  0.0.0.0:*     ← 池机 ecd-8zp47qagrsc95h67t
LISTEN 0 128 172.17.0.1:18104  0.0.0.0:*     ← 本次 ecd-j3daid9t6x8p4wjh7
LISTEN 0 128 172.17.0.1:18001  0.0.0.0:*     ← 共享桌面
LISTEN 0 4096   0.0.0.0:2222   0.0.0.0:*     ← 反向 SSH 入口
```
绑的是 docker 网桥 `172.17.0.1`，不是 `0.0.0.0`。

公网连通性**从 EC2 新加坡（i-067e9ac5ca63d5585）实测**：
```
port 18104 CLOSED/FILTERED   ← 判据
port 2222  OPEN              ← 正对照
port 80    CLOSED/FILTERED   ← 安全组只放 lighthouse 回源 IP，符合预期
port 45678 CLOSED/FILTERED   ← 负对照
```
安全组 `sg-uf6gg28fkz5rsv9vknhf` 入方向只有 22(EC2)/80(lighthouse)/2222(全网)，无 18xxx。

**注意（工具坑，非缺陷）**：本机 `nc` 因为 TUN 代理会对任何地址端口都返回 succeeded
（`nc 1.2.3.4 9999` 也「成功」），本机不能用来做这条负向测试。

### A7 ✅ 重启后 31 秒通道回来

```
05:48:57Z 重启前 channel up (last_seen 05:48:30)
05:48:57Z aliyun ecd reboot-desktops --desktop-id ecd-j3daid9t6x8p4wjh7 → RequestId 01A06AF6-…
+5s  05:49:03Z channel down  "All connection attempts failed"
+18s 05:49:16Z channel down
+30s 05:49:28Z channel up    last_seen 2026-09-04T05:49:22.863100+00:00   ← ≈31s，判据 ≤180s
```
重启确实发生（不是抖动）：重启后 `bash: hostname; uptime -s; uptime -p` 返回
```
daid9t6x8p4wjh7
2026-09-04 13:49:11     ← 本地时间，= 05:49:11 UTC，正是 reboot 之后
up 5 minutes
```
整个过程 ECD 侧 `DesktopStatus` 一直是 `Running`（软重启）。

---

## B. 团队空间 — B1 半通过，B2–B8 ✅

### B1 ⚠️ 半通过：链接出了，但邀请方的「待接受邀请」永远是空的

UI 邀请 `m1u2-0904` 后 toast + 输入框给出链接
`https://ai.bossipai.com.cn/invite/-YgRTgxZJBPRGLYHJLR_E3BVWJ48lJYHHFmWDSne9HI`，
库里 `workspace_invitations` 也有行。**但同页「待接受邀请」区显示「暂无待接受邀请」，刷新后依旧。**

根因（只读代码确认，未改）：
- `backend/api/workspaces.py:103` `GET /api/workspaces/invitations/pending`
  → `workspace_repo.list_pending_for_user(current_user["user_id"])`
- `backend/db/repository/workspace_repo.py:110` 按 **`target in (自己的用户名, 自己的邮箱)`** 过滤，
  返回的是「**发给我的**邀请」，不是「**本空间发出的**邀请」。

实测两边：
```
U1（邀请人）GET /api/workspaces/invitations/pending → {"items":[]}
U2（被邀请）GET /api/workspaces/invitations/pending →
  {"items":[{"id":"01M1NFVGR7BW67B269EZDQMXKM","workspace_id":"01M1NEP01CV78X540QT8G0F521",
             "workspace_name":"m1u1-0904","target":"m1u2-0904","role":"member",
             "expires_at":"2026-09-11T05:56:50.567569+00:00"}]}
```
即：后端根本没有「列出某 workspace 已发出、未接受的邀请」这个接口，团队页那块 UI 接错了数据源。
影响：管理员看不到自己发出去的邀请、不能撤销、重复邀请无提示。归到 B1 工作项修。

### B2 ✅ U2 接受邀请后出现空间切换器
接受页 →「邀请已接受，并已切换到该工作空间」；侧栏顶部出现切换器，当前选中 `m1u1-0904`，
下拉两项：
```
m1u1-0904 → 01M1NEP01CV78X540QT8G0F521
m1u2-0904 → 01M1NEP0N9W2VPE9XGFZH7065N
```
会话列表里出现 U1 的会话 `M1-A1 · m1u1-0904`。

### B3 ✅ 只读
UI：能看完整消息，输入框被替换为「此会话由其他工作空间成员发起，当前为只读模式。」
接口：
```
POST /api/agent/session/session_7YBYAH…/message   (Bearer U2, X-Workspace-Id: U1空间) → 403
{"detail":{"code":"SESSION_READ_ONLY","message":"Only the session owner can change this session"}}
```

### B4 ✅ 桌面按空间隔离
U2 在**自己**空间发 `bash: hostname`：
```
HTTP 503 {"code":"DESKTOP_NOT_READY","detail":"Your cloud desktop is not ready",
          "desktop":{"state":"not_provisioned"}}
GET /api/desktop/status (U2 自己空间) → {"state":"not_provisioned","mode":"per_user"}
```

### B5 ✅ 移除后 403
UI 团队页点「移除」，U2 行状态从「正常」变「已移除」。之后 U2 带 U1 空间访问：
```
GET /api/agent/session              → 403 {"detail":{"code":"WORKSPACE_FORBIDDEN","message":"Workspace membership is required"}}
GET /api/agent/session/{U1的会话}   → 403 同上
GET /api/desktop/status             → 403 同上
GET /api/workspaces                 → 只剩自己那一个空间
```
（UI 小观察：被移除的成员仍留在成员列表里显示「已移除」，还带一个「移除」按钮，软删除的展示没收干净。）

### B6 ✅ 审计四类齐全
`GET /api/admin/audit?workspace_id=01M1NEP01CV78X540QT8G0F521&limit=100`（admin token）→ 200，7 条：
```
auth.register: 1   desktop.provision: 1   workspace.invite: 3   workspace.accept: 1   workspace.remove_member: 1
```
样例行（字段完整，含 resource / details / ip / ua）：
```json
{"id":"01M1NG5DCQ1CGMC2NS8FHK21G5","user_id":"01M1NENZQ65XK6NZ43A057R14M",
 "workspace_id":"01M1NEP01CV78X540QT8G0F521","action":"workspace.invite",
 "resource_type":"workspace_invitation","resource_id":"01M1NG5DCHVMW7K2868BK6K446",
 "details":{"role":"member","target":"m1u2-0904"},
 "ip_address":"172.18.0.5","user_agent":"curl/8.7.1","created_at":"2026-09-04T06:02:14.807579+00:00"}
```

### B7 ✅ 普通用户打管理接口 403
```
GET /api/admin/users  (U1) → 403 {"detail":"Admin access required"}
GET /api/admin/audit  (U2) → 403 {"detail":"Admin access required"}
```

### B8 ✅ 过期邀请 410
新建邀请 `01M1NG5DCHVMW7K2868BK6K446` → 库里 `update … set expires_at = now() - interval '1 day'` →
U2 接受：
```
POST /api/workspaces/invitations/{token}/accept → 410
{"detail":{"code":"INVITATION_EXPIRED","message":"Invitation has expired"}}
```

---

## C. 跳过
用户决定暂缓接缝 AC-2（同空间两成员共用一台桌面），本轮未测。

---

## D. 包月参数与询价 — 全部 ✅

### D1 ✅ 询价
`docker exec -w /app openbox-backend-1 python scripts/wuying_provision_smoke.py price`，退出码 0：
```
== PostPaid ==  TradePrice 0.744086 CNY/h   (企业办公型-4核8G 72.6736 + 50G 高效云盘 1.735)
   parsed: currency=CNY trade_price=0.744086 original_price=0.744086
== PrePaid  ==  TradePrice 105.75 CNY/月    OriginalPrice 211.5（合同优惠_整单_5.0折 RuleId 2000043445483）
   parsed: currency=CNY trade_price=105.75 original_price=211.5
limit=300.00 CNY/month [OK]
```
与参考值 ¥0.744/h、¥105.75/月一致。
（注：验收单写 `uv run python`，容器里 `uv run` 会重建 venv 下载 ~100MB 依赖、300s 超时跑不完；
依赖已装在系统 python，直接 `python` 即可。建议把验收单这条命令改掉。）

### D2 ✅ `grep -c '^WUYING_CHARGE_TYPE' /opt/openbox/config/backend.env` → `0`

### D3 ✅ 只多出一台，且是 PostPaid
```
baseline count: 15   now count: 16
ADDED:   ecd-j3daid9t6x8p4wjh7  obx-887eb40e4bab071c  Running  PostPaid  m-ccceuit7jn3xzwx45
REMOVED: none
```

### D4 ✅ 库里的行
```
      desktop_id       | status  | tunnel_state | charge_type |        workspace_id        |          user_id           | tunnel_port
-----------------------+---------+--------------+-------------+----------------------------+----------------------------+------------
 ecd-8zp47qagrsc95h67t | running | up           | PrePaid     | ws_e5044160ec46…（旧池机） | 01M1G8HRK1WST2RA4Y58PQHHG1 |       18103
 ecd-j3daid9t6x8p4wjh7 | running | up           | PostPaid    | 01M1NEP01CV78X540QT8G0F521 | 01M1NENZQ65XK6NZ43A057R14M |       18104
```
`charge_type=PostPaid` ✅，`workspace_id` = U1 默认空间 ✅。

---

## E. 旧路径不受影响 — ✅

EC2 gw-1 openbox 栈在 `i-0eaae88c8b67d9bb5`：五容器全在
（frontend/backend/postgres/redis + caddy/new-api/reqlog-proxy），
`WUYING_MODE=shared`、`WUYING_ENV_TAG=prod`、**无 `WUYING_ROUTING`**（默认 shared）。

真实回合（经 SSM 在实例内打 `http://127.0.0.1:18081`，用户授权写生产库）：

```
GET /api/desktop/status  (回合前) → {"state":"running","mode":"shared"}   ✅
POST /api/agent/session/session_7YBYA8F40TK132ADCVYVBM5MX0/message  "bash: hostname" → HTTP 200
  user      > bash: hostname
  assistant > [tool] {"command":"hostname","timeout":120,…}
  assistant > `jxaq5g45dr5qr0i`
GET /api/desktop/status  (回合后) → {"state":"running","mode":"shared"}   ✅
```

`jxaq5g45dr5qr0i` = 共享桌面 `ecd-4zjxaq5g45dr5qr0i` 的 HostName，正是 shared 路径该有的结果；
与 gw2 per_user 那台的 `daid9t6x8p4wjh7` 明确不同，两条路径互不串台。

**过程中一次失败（已排除，非本次改动引起）**：第一次回合（用户 `e1-09040745`）只写到 step-start 就停了，
后端日志是上游 LLM 报错，不是桌面/路由问题：
```
2026-09-04 07:45:34 [openbox.agent.llm] ERROR Responses API error 503:
  {"error":{"message":"Upstream server temporarily unavailable","type":"upstream_error",…}}
2026-09-04 07:45:34 [openbox.agent.processor] ERROR LLM error in session session_7YBYA9XH02FMHQDSCC655QCJ1C
```
面上统计：EC2 栈近 6 小时 14 次回合请求里 2 次 LLM 报错（另一次 `Responses API error:` 空消息，
session_7YBYA92F1WH00ZCAZMDFVVGBCG，08:07），new-api 本身 `/api/status` 200。
属上游偶发抖动（约 14%），**但失败时前端只会看到一条没有答复的空回合，没有可见报错**——
这条建议单独立项跟进，与里程碑一无关。

生产库里留下的测试痕迹：账号 `e1-09040745`（失败那条）与 `e1r-09040810`（成功那条），各一个会话。

## F. 收尾

### F1 — 用户已定：**留作 A3 第三台池机**
`ecd-j3daid9t6x8p4wjh7` 保持 PostPaid 运行，本轮不删、不转包月。
需要记住三件事：
1. **在烧钱**：¥0.744/h ≈ ¥17.9/天；A3 时用 `ModifyDesktopChargeType` 转包月（¥105.75/月）才划算，
   按量跑满 6 天就超过一个月包月价。
2. **它绑在一次性测试账号上**：`cloud_desktops.workspace_id = 01M1NEP01CV78X540QT8G0F521`
   （= `m1u1-0904` 的默认空间），云侧标签同值。A3 要拿它当池机，得先把这行的归属解开或改掉，
   否则池机会被当成某个用户的专属机。
3. 池机现共三台：`ecd-8zp47qagrsc95h67t`（v1 镜像，10-01 到期）、
   `ecd-0b7gj174mc6f23ctq`（v2，10-04 到期）、`ecd-j3daid9t6x8p4wjh7`（v2，按量）。

### F2 — 测试账号保留
`m1u1-0904` / `m1u2-0904` / `m1adm-0904`（admin）留在 gw2 库里供后续验收用。
口令在本次会话工作目录的 `creds.env`，未入库、未提交。

### F3 — 交回
本文即 A/B/D/E 四组证据。验收人核对后可在 `DETAILED_PLAN_M1_M2.md` 进度表标「里程碑一收口」。

---

## 待决 / 建议
1. **B1 缺陷归属**：邀请方看不到自己发出的邀请（后端缺「列出本空间未接受邀请」的接口，
   团队页接了 invitee-scoped 的 `/invitations/pending`）。归 B1 工作项。
2. **A3 前解绑 `ecd-j3daid9t6x8p4wjh7` 的 workspace 归属**（见 F1 第 2 条）。
3. **验收单 D1 的命令要改**：`uv run python` 在容器里会重建 venv、下载上百 MB 依赖并超时；
   改成 `docker exec -w /app openbox-backend-1 python scripts/wuying_provision_smoke.py price`。
4. **验收单 A3 的共享桌面主机名 `0zd5sxxe1uw10r6` 已过时**，现在两栈共用的共享桌面是
   `ecd-4zjxaq5g45dr5qr0i` → `jxaq5g45dr5qr0i`。
5. **EC2 栈上游 LLM 偶发 503 时前端静默**（近 6h 约 14% 回合），建议单独立项。
6. **A4 无法用自动化手敲**：云桌面画面是 `g.alicdn.com` 跨源 iframe，注入事件进不去；
   以后要自动化验收桌面交互，得走桌面内 agent 或阿里云 SDK，不能靠浏览器自动化。
