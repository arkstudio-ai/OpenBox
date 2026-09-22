# OpenBox 本地运行命令

命令从仓库根目录执行。主 Web 为 `frontend-v2`，默认端口 `3000`；后端为 `8080`。
目录与能力说明见[文档索引](../README.md)。远程发布按[部署手册](../operations/DEPLOY.md)执行。

## 配置与依赖

```bash
cp -n backend/openbox.jsonc.example backend/openbox.json
cp -n backend/.env.example backend/.env
make deps
cd backend
uv sync --extra test
```

填写自己的模型、数据库、认证与执行环境配置。`scripts/backend_entrypoint.py` 默认加载
`backend/.env`，已设置的进程环境变量优先。它不会自动选择 `.env.wuying-dev` 等其他文件。
需要指定配置时，可在后端目录用 dotenv 明确注入后再调用同一入口，例如：

```bash
uv run python -c 'from dotenv import load_dotenv; import runpy; load_dotenv(".env.wuying-dev", override=True); runpy.run_path("scripts/backend_entrypoint.py", run_name="__main__")' --reload --host 127.0.0.1 --port 8080
```

该示例选择配置文件，不会替你创建无影连接。桌面配置、凭据来源、隧道与健康检查见
[无影指南](../operations/WUYING_SANDBOX.md)。仓库不提交实际环境文件。

## 前台启动

在仓库根目录分别打开两个终端：

```bash
make backend
```

```bash
cd frontend-v2
npm ci
npm run dev -- --port 3000
```

`make backend` 先执行迁移再启动带热重载的服务。`make frontend` 等价于从 Makefile 启动
主 Web；`make dev` 同时启动两端。相关目标还会检查并移除属于本仓库的已退休 SkillJob worker，
详见 Makefile 的 `retire-legacy-worker`。

## 其他 Makefile 入口

| 命令 | 实际用途 |
|---|---|
| `make start` | 停止匹配的旧进程，安装依赖、迁移，后台启动两端并检查健康 |
| `make stop` | 停止匹配的服务进程及默认端口监听进程 |
| `make restart` | `stop` 后执行 `start`，不自动重建沙箱镜像 |
| `make migrate` | 执行数据库迁移 |
| `make sandbox-image` | 构建本地 Docker 沙箱镜像 |
| `make deps` | 启动本地 PostgreSQL、Redis、Azurite |
| `make deps-down` | 停止本地依赖 |
| `make help` | 显示 Makefile 入口 |

`start` / `stop` 使用进程和端口匹配，运行多套开发环境时优先使用各自的前台终端管理进程。
`make clean` 涉及容器和卷删除，不属于普通启动、重启或代码验证步骤。

## 日志与检查

通过 `make start` 启动的服务使用以下日志；前台启动则查看对应终端：

```bash
tail -f /tmp/openbox-backend.log
tail -f /tmp/openbox-frontend.log
```

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail --head http://127.0.0.1:3000/
lsof -nP -iTCP:8080 -sTCP:LISTEN
lsof -nP -iTCP:3000 -sTCP:LISTEN
```

默认依赖端口为 PostgreSQL `5432`、Redis `6379`、Azurite `10000`，以实际配置和
`docker-compose.dev.yml` 为准。本地测试环境可以使用不同端口。

## 工具目录与回归

```bash
cd backend
uv run python -m tool.catalog
uv run pytest tests/unit -q
```

```bash
cd frontend-v2
npm run check
```

集成测试和浏览器 E2E 的环境要求分别查看对应测试与 [Web README](../../frontend-v2/README.md)。
新增工具或技能的放置规则见[开发指南](ADDING_CAPABILITIES.md)。
