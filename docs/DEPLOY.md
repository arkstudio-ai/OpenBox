# 部署说明（AWS 开发环境 / 阿里云生产环境）

两套线上环境跑的是**同一份 compose 与同一组配置文件**，只有域名、端口绑定和
少数 env 值不同。本文说明各自的拓扑、需要哪些配置文件、以及怎么发布与回滚。

Logto SSO 的取值另见 [LOGTO_PROD.md](LOGTO_PROD.md)。

## 一、两套环境

| | 开发（AWS） | 生产（阿里云） |
|---|---|---|
| 域名 | https://ai.ueejavelin.org | https://ai.bossipai.com.cn |
| 主机 | EC2 `i-0eaae88c8b67d9bb5` `OpenClaw-NewAPI` | ECS `i-uf66pcsepxpc23v5qsts` `openbox-gw2-sh` |
| 地址 | `54.254.36.226`（ap-southeast-1） | `106.15.105.236` / 内网 `10.100.1.83`（cn-shanghai） |
| 入口 | 本机 caddy（:80/:443，含 TLS） | 腾讯 Lighthouse `106.52.167.53` 回源到 :80 |
| 前端端口 | `127.0.0.1:18081:80`（只给 caddy） | `80:80`（安全组只放行 Lighthouse 回源） |
| 远程执行 | AWS SSM（`AWS-RunShellScript`） | 阿里云云助手（`ecs RunCommand`） |
| 源码 | 有 `/opt/openbox/src`（git checkout），可就地构建 | **无源码**，镜像从外部装载 |
| 沙箱 | `SANDBOX_PROVIDER=wuying` | `SANDBOX_PROVIDER=wuying` |

请求链路（两边一致）：

```
浏览器 → 反向代理 → frontend 容器(nginx)
                      ├── /        静态 SPA
                      ├── /api/    proxy_pass → backend:8080
                      └── /ws/     proxy_pass → backend:8080（WebSocket 升级）
```

`frontend-v2` 不内嵌后端地址：`VITE_API_URL` 留空即走同源 `/api`，由 nginx 反代。

## 二、服务器目录与配置文件

两台机器的 `/opt/openbox/` 布局相同：

```
/opt/openbox/
├── docker-compose.yml            # 编排主文件（★ 目前只存在于服务器，未进仓库）
├── docker-compose.override.yml   # 各环境差异（端口绑定 / 挂载凭证）
├── .env                          # OPENBOX_IMAGE_TAG、OPENBOX_DB_PASSWORD
├── config/
│   ├── backend.env               # 后端环境变量（★ 与本地 backend/.env 同构）
│   └── openbox.json              # 模型/供应商配置（★ 与本地 backend/openbox.json 同构）
├── secrets/
│   ├── aliyun-config.json        # 后端调用阿里云用的 AK（挂到 /run/secrets/）
│   └── wuying_ed25519            # 无影 relay 隧道私钥
└── src/                          # 仅 AWS 开发机有：git checkout，用于就地构建
```

### 各文件与本地的对应关系

| 服务器文件 | 本地对应 | 是否入库 | 说明 |
|---|---|---|---|
| `config/backend.env` | `backend/.env` | ❌ 忽略 | 同构。模板见 `backend/.env.example` |
| `config/openbox.json` | `backend/openbox.json` | ❌ 忽略 | 同构。模板见 `backend/openbox.jsonc.example` |
| `.env` | 无 | ❌ | 只有 `OPENBOX_IMAGE_TAG` 和 `OPENBOX_DB_PASSWORD` |
| `secrets/*` | 无 | ❌ | 密钥，永不入库 |
| `docker-compose.yml` | 无 | ❌ | **仅存在于服务器**，见下方「已知问题」 |

`backend.env` 当前约 49 个键，覆盖：LLM（`OPENBOX_MODEL/API_KEY/BASE_URL`）、无影沙箱
（`WUYING_*`，约 20 个）、Logto（`LOGTO_*`）、OSS、Blob、DB/Redis、JWT、Tavily 等。

### compose 注入的环境变量（覆盖 `backend.env` 同名键）

`docker-compose.yml` 的 `environment:` 优先级高于 `env_file:`，这几个键以 compose 为准：

```yaml
DATABASE_URL: postgresql+asyncpg://openbox:${OPENBOX_DB_PASSWORD}@postgres:5432/openbox
REDIS_URL:    redis://redis:6379/0
WUYING_ENDPOINT: http://host.docker.internal:18001
LOGTO_REDIRECT_URI:             https://ai.ueejavelin.org/callback   # 生产为 ai.bossipai.com.cn
LOGTO_POST_LOGOUT_REDIRECT_URI: https://ai.ueejavelin.org            # 生产为 ai.bossipai.com.cn
```

改 Logto 的 **endpoint / app id / secret** 要动 `config/backend.env`；
改**回调地址**要动 `docker-compose.yml`。

## 三、发布流程

后端容器启动命令是 `alembic upgrade head && uvicorn ...`，**迁移在启动时自动执行**。

### AWS 开发机（有源码，就地构建）

```bash
cd /opt/openbox/src && git pull
docker build -t openbox-backend:<TAG>     backend/
docker build -t openbox-frontend-v2:<TAG> frontend-v2/
cd /opt/openbox && sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<TAG>/" .env
docker compose up -d
```

### 阿里云生产机（无源码，镜像装载）

生产机**没有登录任何镜像仓库**，镜像靠 `docker save` → 传输 → `docker load`。
Tag 规则：`<日期>-<批次>-<git短sha>`，例如 `20260904-a2-d9d7401`。

本地构建须指定 `linux/amd64`（Mac 是 arm64，服务器是 x86_64）：

```bash
docker build --platform linux/amd64 -t openbox-backend:<TAG>     backend/
docker build --platform linux/amd64 -t openbox-frontend-v2:<TAG> frontend-v2/
docker save openbox-backend:<TAG>     | gzip -1 > backend.tgz
docker save openbox-frontend-v2:<TAG> | gzip -1 > frontend.tgz
```

生产机 22 端口只放行构建机 `54.254.36.226`，本地无法直接 scp。可走私有 OSS 中转
（同区 `cn-shanghai` 桶 + 内网端点，快且免流量），服务器端不留任何凭证：

```bash
aliyun ossutil cp -f backend.tgz oss://<私有桶>/_deploy-tmp/<TAG>/backend.tgz --acl private
aliyun ossutil presign oss://<私有桶>/_deploy-tmp/<TAG>/backend.tgz \
  --expires-duration 2h -e https://oss-cn-shanghai-internal.aliyuncs.com
```

然后在生产机（云助手）执行：

```bash
curl -sS -o /tmp/b.tgz '<预签名URL>' && gunzip -c /tmp/b.tgz | docker load && rm -f /tmp/b.tgz
cd /opt/openbox
cp config/backend.env config/backend.env.bak-$(date +%Y%m%d%H%M%S)
cp .env .env.bak-$(date +%Y%m%d%H%M%S)
sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<TAG>/" .env
docker compose up -d
```

**用完请删除 OSS 临时对象。**

### 验证

```bash
docker compose ps                       # backend / frontend 应为 healthy
curl -s -o /dev/null -w "%{http_code}\n" https://ai.bossipai.com.cn/
curl -s https://ai.bossipai.com.cn/api/auth/logto/config | jq
```

### 回滚

旧镜像会保留在机器上，回滚只需改回 tag：

```bash
cd /opt/openbox
cp config/backend.env.bak-<戳> config/backend.env    # 若改过配置
sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<上一个TAG>/" .env
docker compose up -d
```

## 四、已知问题

### 1. 生产 compose 未纳入版本管理

`/opt/openbox/docker-compose.yml` 与 `docker-compose.override.yml` 只存在于两台服务器上。
仓库根目录的 `docker-compose.yml` 是**本地开发用**的另一份（从源码 build、挂
`docker.sock`、用的还是旧版 `frontend/`），**不是线上那份**，不要混用。

### 2. 生产代码没有对应的 git 提交（2026-09-04 发现）

生产库的 alembic 版本停在 `f7b9d1e3a5c8`，而该 revision 及以下 5 个迁移
在本地、GitHub 两个远程、AWS 构建机的 checkout 中**均不存在**：

```
c3e5f7a9b1d4_workspace_core            e6a8c0d2f4b7_workspace_cloud_desktops
d4f6a8b0c2e5_workspace_business_scope  c1d3e5f7a9b2_add_desktop_channels
f7b9d1e3a5c8_prepaid_desktop_metadata
```

镜像 tag 里的 `d9d7401`（以及 `bf568d0`/`e9ec7ff`/`f0b7540`/`a789456`）都不是任何
仓库中的提交。**该版本代码从未入库，唯一副本在镜像内。**

后果：直接用 `main` 构建并部署会导致 `alembic upgrade head` 报
`Can't locate revision identified by 'f7b9d1e3a5c8'` 而无法启动；即使能启动，也会
造成 workspace/云桌面模块的功能回退。

恢复办法（backend 的 Dockerfile 是 `COPY . .`，源码在镜像 `/app` 内）：

```bash
docker create --name recover openbox-backend:20260904-a2-d9d7401
docker cp recover:/app ./recovered-src
docker rm recover
```

**在把这份代码整理入库之前，不要从本地向生产发布。**

> **2026-09-04 更新**：上述 5 个迁移与对应代码已随 `main`（`b1f77ae`）入库并推到 GitHub；生产 gw2 已用 `main` 构建的 `20260904-main-b1f77ae` 部署，库升到 `c3e5a7b9d1f4`。本条已解决，可直接从 `main` 构建发布。构建走 AWS 机的 `/opt/openbox/build-main`（`git worktree`，不动 `src/` 的本地改动），传输用 `docker save | gzip | ssh gw2 docker load`（EC2 的 `/root/.ssh/gw2ship`）。
