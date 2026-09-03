# OpenBox 切换到自有 new-api 操作单

> 状态：操作单 v1（2026-09-02），按节顺序执行。<br>
> 决策：OpenBox 全部模型流量（LLM / 图片 / 视频 / 可选 ASR）走我们自己部署的 new-api
> （bossip-gw-1 上的 `bossip-newapi`），后续多上游渠道、计费、日志都在自己手里。
> 初期开发和部署麻烦一点可以接受。<br>
> 姊妹文档：`docs/BILLING_PLAN.md`（计费方案，§5 网关侧准备指向本文）。

---

## 0. 目标拓扑

> 2026-09-02 更新：gw2 **重建在上海**，与 new-api 同 VPC。理由：ECD 包月桌面都固定在上海
> （bossip 舰队 14 台、openbox 共享桌面、上海版金镜像都在那里），网关也在上海；
> 同城同 VPC 后跨地域对等连接和带宽费都不需要了。杭州那台 gw2 是 9 月 1 日建的测试机
> （1 个用户、1 个会话、1 台桌面、没有域名），直接重建不迁移。

```
openbox-gw2  上海  bossip-gw-vpc vpc-uf6pfjyysdwybkq8m0hzk  10.100.0.0/16
             vswitch vsw-uf6urpiz14jtl0ii58hpj（cn-shanghai-b，10.100.1.0/24）
   │  同 VPC 内网，直连
   ▼
bossip-gw-1  上海  10.100.1.76  :3000 bossip-newapi（分支 fork-minimaxv2-20260810d，PG，QuotaPerUnit 500000 = ¥1）
        ├─ LLM   ch116/117 → api.ueejavelin.org（补 Claude 5 / DeepSeek V4 / Qwen3.8 渠道）
        ├─ 图片  ch115 gpt-image-2 → ueejavelin
        ├─ 视频  ch123 wan3 · ch120/106 seedance · ch113 sd2 · ch114 MiniMax-H3
        └─ ASR   ch111 seed-asr（可选；openbox 现直连百炼 fun-asr，可暂不动）

ECD 上海  office site cn-shanghai+dir-2879607125，金镜像 m-feb7j85i4d4qo09ux（openbox-image-v1-shanghai）
EC2 新加坡 54.254.36.226：切换后只作备用，走公网门面 openapi.bossipai.com.cn（附录 B）
```

为什么不走公网门面：门面 nginx 只放行 `/v1/videos`，每 IP 每秒 10 次限流，
请求体 4MB 上限，`/api/*` 管理接口全部 404。同 VPC 内网这些问题都不存在，
每用户 token 的管理 API 也直接可达。

已核实：bossip-newapi 在 compose 里绑的是 `0.0.0.0:3000`，不是仅回环；
安全组是唯一防线，现在只放腾讯门面 `106.52.167.53/32` 回源。新加规则源必须写 `/32`。

---

## 1. 网络与主机（阿里云，一次性）

> **状态 2026-09-02：本节已执行。** 上海 gw2 = `i-uf66pcsepxpc23v5qsts`，内网 10.100.1.83，
> 公网 106.15.105.236，安全组 sg-uf6gg28fkz5rsv9vknhf；new-api 安全组已放行；栈已起并验收
> （聊天回合、共享桌面票、上海 per_user 开通冒烟均通过）；杭州实例、桌面、安全组已删。
> 目前先按量付费，转包月待跑稳后做。LLM/视频仍指向 ueejavelin 与公网门面，切换看 §2/§3。
> 对外域名 `https://ai.bossipai.com.cn`（腾讯 lighthouse TLS 终结后反代到 106.15.105.236，
> 配置在 bossip 仓库 lighthouse/ai.conf），后端 Logto 回调已改为该域名。

1. **在上海新建 gw2**：地域 cn-shanghai，可用区 b，VPC `vpc-uf6pfjyysdwybkq8m0hzk`，
   交换机 `vsw-uf6urpiz14jtl0ii58hpj`。规格建议不低于 4 vCPU 8 GiB（杭州那台是
   2 vCPU 4 GiB 按量，跑 docker 构建吃力），系统盘 100 GiB ESSD（镜像层多），
   按月包年包月，绑一个 EIP。安全组新建 `openbox-gw2-sg`：入站只放 22（限办公网）、
   80/443（对外）。
2. **new-api 安全组** `sg-uf61yit0irjcov15390u` 加入站规则：TCP 3000，
   源 = 新 gw2 的内网 IP `/32`，描述「openbox-gw2 内网直连 new-api」。
3. **不需要** VPC 对等连接、路由、跨地域带宽。
4. **验证**（在新 gw2 上）：

```bash
curl -s -m 5 http://10.100.1.76:3000/api/status | head -c 200
curl -s -m 5 -o /dev/null -w '%{http_code}\n' -X POST http://10.100.1.76:3000/v1/chat/completions
```

第一条 200，第二条 401（没带 key）即通。

5. **部署 openbox**：照 `/opt/openbox` 的既有做法（clone 公开仓库到 `/opt/openbox/src`、
   本机 build 两个镜像、compose up、后端启动自动 `alembic upgrade head`）。
   backend.env 从杭州 gw2 拷过来，按 §3A 和 §5 改值。
6. **清理杭州**：确认上海跑通后，删杭州 gw2 实例 `i-bp19ffdtk2dvpjfdwmg9`，
   删它名下的 per_user 桌面 `ecd-age8dpka5uabwhbul`（标签 openbox-env=gw2）。
   杭州另两台 prod 标签桌面属于 EC2 生产栈，切换完成前不动。

---

## 2. new-api 侧（bossip-gw-1，全是配置，不改代码）

> **状态 2026-09-02：2A / 2B 已执行，2C 部分执行。** 分组 openbox、用户 openbox(id 3)、
> token openbox-shared(id 20)、九条渠道加组、新渠道 124 deepseek-v4 / 125 qwen38 / 126 claude5，
> ch116 追加 gpt-5.5/5.4/5.4-mini。倍率只加了新模型条目，bossip 原有条目未动。
> 凭据在 bossip-gw-1 `/root/openbox-newapi-secrets.txt`。Claude 全系当前在 ueejavelin 上游
> 需重新认证，待上游修。视频价格（2C 视频段、fork 改动）未做。

### 2A. 用户、分组、token

| 项 | 做法 | 为什么 |
|---|---|---|
| 用户 `openbox` | 普通用户，不是管理员；用户级 quota 设无限；生成 access token | 每用户 token 的增删改和日志拉取用的是「本用户自己的」接口，不需要管理员权限，也就不用把管理凭证给 openbox |
| 分组 `openbox` | GroupRatio 1；把渠道 116、117、115、123、120、106、113、114、111 的 group 加上 `openbox` | 不复用 `vip`：那是 bossip-center 的计费分组，两边价格要能独立调 |
| 共享 token `openbox-shared` | 属 `openbox` 用户，group `openbox`，无限额 | M0/M1 切换期用；M2 起换成每用户 token |
| 每用户 token | openbox 后端调 `POST /api/token/`，头 `Authorization: Bearer <openbox access token>` + `New-Api-User: <openbox 用户 id>`；name = openbox user id，group `openbox`，model_limits 按套餐 | 与 `docs/BILLING_PLAN.md` §4.4 影子上限一致 |

存到 openbox 的 backend.env：`NEWAPI_BASE_URL=http://10.100.1.76:3000`、
`NEWAPI_ACCESS_TOKEN`、`NEWAPI_USER_ID`。

### 2B. 补 LLM 渠道

现状：LLM 只有 gpt-5.6 一族（ch116/117 转 ueejavelin）加几个国产模型。openbox 菜单里的
Claude 5、DeepSeek V4、Qwen3.8 没有渠道。全部新建 type 1（OpenAI 兼容），
base `https://api.ueejavelin.org`，key 复用 ch116 的那把，group `openbox`：

| 新渠道名 | 模型 |
|---|---|
| uee-claude5 | claude-opus-5, claude-sonnet-5, claude-fable-5, claude-opus-4-8, claude-opus-4-7, claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5 |
| uee-deepseek-v4 | deepseek-v4-pro, deepseek-v4-flash, deepseek-reasoner, deepseek-chat |
| uee-qwen38 | qwen3.8-max, qwen3.8-flash |
| ch116 追加 | gpt-5.5, gpt-5.4, gpt-5.4-mini |

ueejavelin 上这些模型分别由渠道 1/4/8（Claude）、3（DeepSeek 直连）、13（Qwen3.8）服务，已验证可用。

### 2C. 价格（成本价，CNY；这台实例 ModelPrice / ModelRatio 都按人民币）

**LLM**：ueejavelin 的 ModelRatio 按美元（ratio × 2 = $/M 输入）；bossip 实例同一约定但按人民币。
换算：`bossip ratio = ueejavelin ratio × 汇率(取 7.2)`，CompletionRatio 与 CacheRatio 照抄。

| 模型 | ueejavelin ratio | 换算 ratio | CompletionRatio | CacheRatio |
|---|---|---|---|---|
| gpt-5.6-luna | 0.1 | 0.72（现值 0.4，运营确认哪个是实际结算价） | 6 | 0.1 |
| gpt-5.6-sol | 2.5 | 18 | 6 | 0.1 |
| gpt-5.6-terra | 1.0 | 7.2 | 6 | 0.1 |
| gpt-5.5 / 5.4 / 5.4-mini | 2.5 / 1.25 / 0.375 | 18 / 9 / 2.7 | 6 | 0.1 |
| claude-opus-5 / opus-4.x | 2.5 | 18 | 5 | 0.1 |
| claude-fable-5 | 5.0 | 36 | 5 | 0.1 |
| claude-sonnet-5 / 4.6 | 1.5 | 10.8 | 5 | 0.1 |
| claude-haiku-4-5 | 0.5 | 3.6 | 5 | 0.1 |
| deepseek-v4-pro | 0.2055 | 1.48 | 2 | 0.00833 |
| deepseek-v4-flash / chat / reasoner | 0.0685 | 0.49 | 2 | 0.02 |
| qwen3.8-max / flash | 未配 | 按百炼刊例填 | — | — |

**视频**：现状 ch123/120/106 固定每次 ¥0.5，ch113 每秒 ¥0.5，MiniMax 按 token。
目标是「每秒 × 分辨率倍率」，分两步：

1. 过渡期先把 ModelPrice 改成 720P 档的每秒成本价（单位元）：
   wan3.0-video 0.60、wan3.0-video-prime 0.90、doubao-seedance-2-0-260128 0.95、
   doubao-seedance-2-0-fast-260128 0.60（官方价未查到，先按 mini 档估）、
   sd2 三模型 0.50、MiniMax-H3 0.50。
2. fork 改动（bossip 侧 codex 团队）：type 54 / 58 渠道的 `/v1/videos` 计费改成
   `ModelPrice × duration × 分辨率倍率`，与 ch113（type 55）现有逻辑对齐。
   在此之前视频硬闸只对 sd2 有效，其余靠 openbox 账本约束。

**图片**：gpt-image-2 现 ModelPrice 0.05（每张 5 分，明显占位），按 ueejavelin 实际结算价改。

### 2D. 日志与对账

openbox 用自己的 access token 拉 `GET /api/log/self?token_name=<user id>&...`，
只看本用户名下的 token 日志，不需要管理员日志接口。对账口径见 BILLING_PLAN §4.6。

---

## 3. openbox 侧改动

### 3A. 配置（gw2 的 `/opt/openbox/config/backend.env` 与 `openbox.json`）

> **状态 2026-09-02：LLM 与图片已切内网**（OPENBOX_BASE_URL=http://10.100.1.76:3000/v1，
> OPENBOX_API_KEY=openbox-shared）；域名下真实回合验证通过，网关日志落在 openbox-shared 名下。
> **视频也已切内网**（commit b8b68e0，镜像 20260902-b8b68e0）：BOSSIP_BASE_URL=http://10.100.1.76:3000，
> openbox.json 里 provider.bossip.options.wire_format=bossip_videos；wan3 2 秒 480p 实测经
> /v1/videos 提交成功、日志记在 openbox-shared 名下。EC2 生产栈未动，仍是旧镜像 + 公网门面。

| 键 | 现值 | 新值 |
|---|---|---|
| OPENBOX_BASE_URL | https://api.ueejavelin.org/v1 | http://10.100.1.76:3000/v1 |
| OPENBOX_API_KEY | ueejavelin token | `openbox-shared` token |
| BOSSIP_BASE_URL | https://openapi.bossipai.com.cn | http://10.100.1.76:3000 |
| BOSSIP_API_KEY | center-media-vip（与 bossip 共用） | `openbox-shared` token |
| video_generation.channel_providers | {"sd2":"bossip"} | {"sd2":"bossip","task":"bossip"} |
| image_generation.provider | openai | 不变（同一网关） |
| provider.newapi | 指向 ueejavelin | 删除 |

### 3B. 代码（两处小改，都在 `backend/tool/video_providers.py`）

> **状态：已完成**（b8b68e0）。私网 http 放行 + options.wire_format 显式声明，单测 4 条新增。

1. `_ark_route` 现在强制 `https://` 且按域名是否等于 `openapi.bossipai.com.cn` 推断
   `wire_format`。内网 `http://10.100.1.76:3000` 会被拒绝，且 wire_format 会误判成
   tokenspace 格式。改法：允许 http 的私网地址；`wire_format` 改为由 provider 的
   `options.wire_format` 显式指定，域名推断只作缺省。
2. `channel_providers` 里的 `task` 通道走 `/v1/video/generations`，门面 404 但内网直连可用，
   不需要改；只要 provider 指到内网地址即可。

M2 时再改 `agent/llm.py`：按 session 的用户解出 gateway token 覆盖 `api_key`（BILLING_PLAN §4.3）。

### 3C. 验收

```bash
# 1. 流式对话（看首 token 延迟与 SSE 不被缓冲）
curl -N -s http://10.100.1.76:3000/v1/chat/completions -H "Authorization: Bearer $OPENBOX_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.6-luna","stream":true,"messages":[{"role":"user","content":"ping"}]}' | head -5
```

然后在 openbox 里：一次 Claude 5 对话（验证新渠道）、一张 gpt-image-2、
一次 `video_generate action=estimate`、一条 2 秒 480P 的 wan3（约 ¥0.6），
最后在 new-api 日志里确认四条记录都落在 `openbox-shared` 名下、扣费与预期一致。

---

## 4. 切换顺序与回滚

| 步 | 内容 | 门槛 |
|---|---|---|
| 1 | §1 上海建 gw2 + 安全组 | gw2 到 3000 通 |
| 2 | §2A 用户/分组/共享 token，§2B 补渠道 | 新渠道 curl 通 |
| 3 | §3B 代码改动合 main，出镜像 | 单测通过 |
| 4 | 上海 gw2 按 §3A 与 §5 配 env 与 openbox.json，启动后端，跑 §3C，再开一台包月桌面走通开通→连接 | 四条日志核对；桌面 Running 且出票成功 |
| 5 | §2C 价格 | 与 BILLING_PLAN M1 shadow 记账并行 |
| 6 | EC2 改为备用：env 指向公网门面（附录 B）或直接停 | — |

回滚：把 §3A 的三个 env 改回原值重启即可，new-api 侧新增的用户、分组、渠道不影响 bossip 现有流量。

---

## 5. ECD 改到上海（配合 gw2 迁移）

| 键 | 杭州现值 | 上海新值 |
|---|---|---|
| WUYING_REGION_ID | cn-hangzhou | cn-shanghai |
| WUYING_IMAGE_ID | m-5j3k26iwy99nrxtv0（openbox-image-v1） | m-feb7j85i4d4qo09ux（openbox-image-v1-shanghai，已存在） |
| WUYING_OFFICE_SITE_ID | cn-hangzhou+dir-6394706301 | cn-shanghai+dir-2879607125 |
| WUYING_POLICY_GROUP_ID | pg-0bbay5jmvosn8b2hc | 不变（中心策略组，上海共享桌面已在用） |
| WUYING_ENV_TAG | gw2 | prod（EC2 退役后）或 gw2-sh（并行期） |
| WUYING_CHARGE_TYPE | PostPaid | **PrePaid**（套餐 = 包月桌面） |
| WUYING_DESKTOP_TYPE | eds.enterprise_office.4c8g | 不变，或与 bossip 舰队对齐 6c12g（价格差要进套餐成本） |

**包月需要一处代码改动**：`backend/sandbox/wuying_ecd.py` 的 CreateDesktops 现在只传
`charge_type`，PrePaid 还必须传 `period=1`、`period_unit=Month`、`auto_renew`。
加三个配置项 `WUYING_PERIOD` / `WUYING_PERIOD_UNIT` / `WUYING_AUTO_RENEW`，
仅当 charge_type 为 PrePaid 时下发。

**包月改变了到期语义**（同步进 BILLING_PLAN §4.2）：

- 开通套餐 = 创建包月桌面并开自动续费；套餐续费成功 = 保持自动续费。
- 用户不续费 = 不再为该桌面调 RenewDesktops（2020-09-30 版 API 没有单独的开关自动续费接口，
  自动续费只是 CreateDesktops 的 AutoRenew 参数；建议创建时 AutoRenew=false，续期完全由
  openbox 的订阅巡检按月调 RenewDesktops 驱动）。桌面到期进入 Expired，阿里云保留约
  15 天后释放。这一段天然就是宽限期，不用自己停机。
- 宽限期内续费 = RenewDesktops 续一期，Expired 桌面续费后恢复 Running。
- 包月桌面**不能**随时 DeleteDesktops（退订走另一条流程）；鬼桌面清理逻辑对 PrePaid 要改成
  「关自动续费 + 标记」，不是硬删。

**金镜像同步**：上海版 `openbox-image-v1-shanghai` 与杭州版内容是否一致要核一次
（obx-display 1080p、action server、bossip-chrome 三项），不一致就从杭州 CopyImage 到上海
后再切。

---

## 附录 A：openbox.json 模型名与 new-api 模型名

openbox 的模型 id 带 `openai/` 前缀（LiteLLM 路由用），发给网关时去掉前缀。
网关侧模型名与 ueejavelin 一致，上表 2B 已按 openbox 20 个模型逐一覆盖。
视频 8 个模型名与 `video_generation.models` 里的 id 完全一致，不需要映射。

## 附录 B：公网门面放行（仅备用，给 EC2 或本地开发）

在腾讯 lighthouse `/opt/nginx/conf.d/openapi.conf` 增加：

```nginx
limit_req_zone $binary_remote_addr zone=openbox_rl:10m rate=100r/s;

location ~ ^/v1/(chat/completions|responses|images/generations|video/generations|models)$ {
    allow 54.254.36.226;   # openbox EC2
    deny all;
    limit_req zone=openbox_rl burst=200 nodelay;
    client_max_body_size 32m;
    proxy_pass http://47.116.181.123:3000;
    # 其余 proxy_* 与 /v1/videos 段完全相同（Host 写 IP、XFF 用 $remote_addr、buffering off、600s 超时）
}
```

`/api/*` 管理接口不开公网；EC2 上如需建 token，走内网机器代办。
