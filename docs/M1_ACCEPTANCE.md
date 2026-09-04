# 里程碑一「打通」验收步骤

> 2026-09-04。按 `DETAILED_PLAN_M1_M2.md` v3 口径：一个 workspace 一台桌面可用、团队空间可用、包月参数与询价可用。
> 环境：gw2 生产 `https://ai.bossipai.com.cn`（镜像 `20260904-a2-d9d7401`，`WUYING_ROUTING=per_desktop`，`WUYING_CHARGE_TYPE` 未设 = PostPaid，镜像 v2，50G）。
> 费用：验收只开一台按量桌面，约 ¥0.74/小时；全程不买包月。
> 用法：新开对话把本文交给执行者，逐步做，每步把「证据」贴回来；全部 ✅ 即里程碑一收口。

---

## 0. 准备

| 项 | 内容 |
|---|---|
| 账号 | 两个新注册账号 U1、U2（用户名自取，记下） |
| 管理员 | 一个 `users.role=admin` 的账号。没有就在 gw2 上执行：`docker exec openbox-postgres-1 psql -U openbox -d openbox -c "update users set role='admin' where username='<你的用户名>'"`，重新登录生效 |
| CLI | 本机 `aliyun` 能调 ecd（cn-shanghai）；gw2 可经 `aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf66pcsepxpc23v5qsts` 或 SSH 进 |
| 基线 | 记录开始时上海桌面列表：`aliyun ecd describe-desktops --api-version 2020-09-30 --region cn-shanghai --biz-region-id cn-shanghai --max-results 100`，验收结束后对比，只允许多出你开的那一台 |

---

## A. 一个 workspace 一台桌面（A1 + 接缝）

| 步 | 操作 | 通过判据 | 证据 |
|---|---|---|---|
| A1 | U1 登录，**先不开桌面**，直接在聊天发 `bash: hostname` | 前端提示云桌面未就绪并引导到云桌面 tab；接口返回 503 `DESKTOP_NOT_READY` | 截图 + 浏览器 Network 里的 503 响应体 |
| A2 | U1 到云桌面 tab 点「开通」，等状态到 running 且通道 up（约 1–2 分钟） | `GET /api/desktop/status` 返回 `state=running`、`channel.state=up`、`mode=per_user` | 状态 JSON |
| A3 | U1 发 `bash: hostname` | 返回值 = 这台桌面的 `DescribeDesktops.host_name`，且 ≠ `0zd5sxxe1uw10r6`（共享桌面） | 聊天截图 + `describe-desktops --desktop-id <id>` 输出 |
| A4 | 云桌面 tab 连上画面，在桌面里开终端敲 `hostname` | 与 A3 一致 | 桌面截图 |
| A5 | 云侧核对这台桌面 | `ChargeType=PostPaid`、`ImageId=m-ccceuit7jn3xzwx45`、`PolicyGroupId=pg-0bbay5jmvosn8b2hc`、标签含 `openbox-workspace=<U1 默认空间 id>` | `describe-desktops` 输出 |
| A6 | gw2 上看通道 | `ss -ltn` 有一条 `172.17.0.1:181xx`；公网 `nc -zv 106.15.105.236 <port>` 不通 | 两条命令输出 |
| A7 | 重启桌面：`aliyun ecd reboot-desktops --api-version 2020-09-30 --region cn-shanghai --biz-region-id cn-shanghai --desktop-id <id>`，计时 | ≤ 3 分钟内 `channel.state` 回到 up；再发一次 `bash: hostname` 成功 | 前后两次状态 JSON 带时间 |

## B. 团队空间（B1）

| 步 | 操作 | 通过判据 | 证据 |
|---|---|---|---|
| B1 | U1 设置页「团队」邀请 U2（用户名） | 出现待接受邀请与链接 | 截图 |
| B2 | U2 打开邀请链接接受 | U2 侧栏出现 workspace 切换器，能切到 U1 的空间 | 截图 |
| B3 | U2 在 U1 空间打开 U1 的会话 | 能看消息，输入框隐藏并提示只读；发消息接口 403 `SESSION_READ_ONLY` | 截图 + 403 响应体 |
| B4 | U2 在**自己**空间发 `bash: hostname` | 503 `DESKTOP_NOT_READY`（U2 空间没有桌面）——证明桌面按空间隔离 | 响应体 |
| B5 | U1 在团队页把 U2 移除 | U2 再访问 U1 空间 → 403 `WORKSPACE_FORBIDDEN` | 响应体 |
| B6 | 管理员调 `GET /api/admin/audit?workspace_id=<U1 空间>` | 至少有 `workspace.invite`、`workspace.accept`、`workspace.remove_member`、`desktop.provision` 四类记录 | 响应 JSON |
| B7 | 普通用户调 `GET /api/admin/users` | 403 | 响应体 |
| B8 | 过期邀请 | 在 gw2 库把一条邀请 `expires_at` 改到过去，再接受 → 410 | 响应体 |

## C. 同空间两个成员同一台桌面（接缝 AC-2，**用户决定暂缓，可跳过**）

| 步 | 操作 | 通过判据 |
|---|---|---|
| C1 | 在 B2 之后、B5 之前，U2 切到 U1 空间发 `bash: hostname` | 与 A3 相同主机名 |
| C2 | U1、U2 同时各发一条 | 两条都成功，不报冲突 |

## D. 包月参数与询价（A2）

| 步 | 操作 | 通过判据 | 证据 |
|---|---|---|---|
| D1 | gw2 容器内：`docker exec openbox-backend-1 uv run python scripts/wuying_provision_smoke.py price` | 打印 PostPaid 每小时价与 PrePaid 一月价（参考：¥0.744/h、¥105.75/月）及 raw；退出码 0 | 完整输出 |
| D2 | gw2：`grep -c '^WUYING_CHARGE_TYPE' /opt/openbox/config/backend.env` | 输出 0（未设，即 PostPaid） | 输出 |
| D3 | 对比 §0 的基线桌面列表 | 只多出 A2 开的那一台，且是 PostPaid | 两次列表 |
| D4 | 库里看 A2 那台的行：`select desktop_id,status,tunnel_state,charge_type,workspace_id from cloud_desktops where is_deleted=false` | `charge_type=PostPaid`，`workspace_id` = U1 默认空间 | 查询结果 |

## E. 旧路径不受影响（可选，10 分钟）

| 步 | 操作 | 通过判据 |
|---|---|---|
| E1 | EC2 生产栈（旧镜像、shared）登录发一条回合 | 正常，`/api/desktop/status` 仍 `mode=shared` |

## F. 收尾

| 步 | 操作 |
|---|---|
| F1 | 决定 A2 开的这台按量桌面：删除（`aliyun ecd delete-desktops ... --desktop-id <id>`，约 1 分钟后 EndUser 才能删，后端会重试）或留作 A3 第三台池机（按量转包月要在 A3 里用 `ModifyDesktopChargeType`） |
| F2 | 两个测试账号可留作后续验收用 |
| F3 | 把 A/B/D 三组证据与本文一起交回，验收人核对后在 `DETAILED_PLAN_M1_M2.md` 进度表标「里程碑一收口」 |

---

## 通过标准
A1–A7、B1–B8、D1–D4 全部 ✅；C 组按用户决定可跳过但要注明；E 组可选。任何一条不过，先记录现象与响应体，不要自行修代码——回到主对话决定归哪个工作项修。
