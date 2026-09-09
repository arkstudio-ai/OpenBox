# 超管技能管理 CRUD 与虚拟机安装管理

本次实现补齐技能商店管理能力。用户确认发布后，已于 2026-09-09 14:26（北京时间）
部署到阿里云正式环境，源码 `d445b9f`，迁移至 `e1f3a5b7c9d2`；没有在真实用户
桌面执行测试性安装/卸载。发布过程与备份见 [部署记录](DEPLOY.md)。

## 功能与业务边界

- 商店条目支持新增 Skill / MCP、编辑展示名称/描述/图标及 SKILL.md 或 MCP 配置、删除和回收站恢复。
- 展示图标支持 Emoji 或 HTTPS 图片地址，图片加载失败有回退；不接收可执行 HTML / data URL。
- 批量上传支持多选 ZIP、连续追加文件、移除待上传项、逐文件结果、仅重试失败项。普通用户的原有上传窗口也支持多选压缩包。
- 每个管理端 ZIP 对应一个技能，SKILL.md 在根目录或一个包装目录内；保留脚本与附件。拒绝路径穿越、链接、重复路径、加密/损坏文件；清理 Finder 元数据，不执行导入包内的脚本。
- 每批最多 20 个文件、单文件 32 MiB、整批 128 MiB；解压后 64 MiB、最多 2,000 个条目。新条目默认下架，同名不覆盖。
- 编辑使用 revision 校验，过期表单返回 409；替换 SKILL.md 保留原包其他文件，公开版本不覆盖作者个人归档和已安装副本。
- 删除是可恢复的商店删除，不自动卸载用户副本；恢复默认下架。批量删除按项报告结果，写入操作原因和审计。
- 「用户安装」默认展示「虚拟机实际安装」，保留「平台安装记录」作为历史视图。安装流水不能作为当前文件存在的证据。
- 先选择具体桌面及空间成员，再手动扫描 Skill / MCP，包括手动安装和技能合集。合集以实际安装目录为单位卸载，并显示影响范围。
- 扫描和卸载不经过 sandbox 自动分配入口，不新建/启动桌面。只使用已分配桌面的既有连接，绑定 workspace 与 user scope；离线、超时或部分扫描失败明确显示未知状态。
- 卸载前重新确认目标与来源；内置/未知来源 Skill 不允许删除。卸载后重新读取确认，失败或结果不确定不报告成功；保留个人归档并暂停该归档的自动恢复，避免删除后重新出现。

## 数据迁移与发布

新增迁移 `e1f3a5b7c9d2`，前置 `d9e1f3a5b7c2`：

- 创建 `skill_catalog_packages`，保存管理端包、展示覆写、revision 与删除标记；列表查询不读取 ZIP 二进制。
- `audit_logs.resource_id` 从 64 扩为 128，容纳完整 catalog / desktop 标识。降级不缩窄审计字段，避免丢失历史标识。

正式发布需先备份数据库，再执行 `alembic upgrade head`，部署配套后端和前端。回退到不理解删除标记的旧版本可能重新展示代码内置条目，应优先回滚应用流量而不是删除新表。新表的 DROP 降级会丢失管理端包，不能当作无损回滚。

## 可重复验证

2026-09-09 本地验证结果：前端完整检查与 402 项单测通过、生产构建通过；12 个浏览器流程通过；相关后端测试 166 项及 sandbox scope / catalogue 补充回归 20 项通过；其中新增管理接口的 57 项在独立 PostgreSQL 测试库再次通过。ESLint 有 30 条既有警告，无错误。

后端（在 `backend/`）：

```sh
uv run --with psutil pytest tests/unit/test_admin_skill_management.py tests/unit/test_admin_skills_api.py tests/unit/test_user_skill_api.py tests/unit/test_user_skill_library.py tests/unit/test_skill_install_safety.py -q
uv run --with psutil pytest tests/unit/test_sandbox_user_scope.py tests/unit/test_sandbox_shared_desktop.py tests/unit/test_sandbox_client_skill_packages.py tests/unit/test_catalogue_projection.py -q
```

默认使用 SQLite 测试库。`psutil` 是加载实际虚拟机 action_server 源码的测试依赖；安装、扫描、卸载的闭环只在 pytest 临时目录执行。

PostgreSQL 的独立验证入口：`tests/run_admin_skill_postgres.py`。仅接受 `ADMIN_TEST_DATABASE_URL` 且库名必须以 `openbox_admin_skill_` 开头；不会使用普通 `DATABASE_URL`。本地 Docker 独立库验证过迁移 SQL 和完整管理接口测试。

前端（在 `frontend-v2/`）：

```sh
npm run check
npm run build
npx playwright test --config playwright.admin-skills.config.ts
```

浏览器使用真实组件和隔离 HTTP 桩，不登录真实用户、不请求生产接口。覆盖 320 / 390 / 768 / 1440 像素布局，CRUD、图标编辑、回收站、ZIP 多选和部分失败重试、网络中断、过期编辑、批量删除部分失败、指定用户扫描与卸载、内置技能保护、离线/部分扫描与卸载超时反馈。

虚拟机离线、超时、权限隔离和读回失败等场景通过受控故障注入验证；没有在真实用户桌面上做破坏性验证。线上迁移、服务健康、配置隔离、目录 ORM 只读读取、管理接口未登录鉴权与 101 个前端文件一致性已验证；本轮没有对真实桌面执行扫描/卸载端到端验收。
