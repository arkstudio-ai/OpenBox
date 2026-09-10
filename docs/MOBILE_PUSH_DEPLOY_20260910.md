# 阿里云移动端通知后端发布记录（2026-09-10）

已发布到 `https://ai.bossipai.com.cn/` 对应的上海 ECS `i-uf66pcsepxpc23v5qsts`。
后端镜像为 `openbox-backend:20260910-mobile-push-0513dcb`，在本地 Docker 构建
`linux/amd64`，经私有 OSS 传输并核对 SHA-256 后装载。Python 固定为线上已有的 3.12.14。

代码基线为 `main@0513dcbe73c6e0b4e23bc4011fe48084149a2cce`，另外保留了现网的三处
登录指引修复：`backend/tool/desktop_login.py`、`backend/sandbox/browser_runtime_repair.py`
和 `container/dev-browser/SKILL.md`。这三个文件与发布前运行容器的哈希一致；
完整补丁随发布包保存，下次从 Git 全量构建仍须包含这些修改。

## 切换与配置

- 用户明确授权立即发布并中断当前任务。17:55:36（北京时间）切换前有 3 个活跃会话及
  3 个执行租约，已保存授权记录和新的数据库备份；重启使用 45 秒停止宽限期。
- 新后端于 17:55:46 启动，17:56:09 完成验收。公网 API 采样在 17:55:41–17:55:56
  观察到 7 次 502，17:55:58 首次恢复 200。探测器还出现过连接超时，不能据此断言
  服务器发生了额外故障。最终四个服务均 healthy，后端重启次数为 0。
- `config/backend.env` 仅追加本地的六项 `BOSSIP_APNS_*` / `BOSSIP_JPUSH_*` 推送配置；
  原有内容保留，文件权限为 0600。苹果私钥在
  `/opt/openbox/secrets/mobile-push/AuthKey_8BUB654RH8.p8`，文件 0600、目录 0700，
  只读挂载到 `/run/secrets/bossip-apns.p8`。镜像内没有 `.env` 或 `.p8`。
- Compose override 仅改变后端镜像并增加上述只读挂载。运行环境变量按键和值核对，
  除六项推送配置外全部一致；`.env`、`config/openbox.json`、主 Compose 文件哈希不变。
  frontend 继续运行 `20260910-nav-3791e77`；frontend、postgres、redis 容器 ID 不变。

## 数据库与验证

正式数据库从 `d2f4a6c8e0b2` 升至唯一 head `e4f6a8b0c2d4`，新增 `mobile_sessions`、
`mobile_presence`、`push_devices`、`push_messages`、`push_deliveries` 五张表。
先用生产备份恢复独立数据库并完成迁移预演，原有 49 张业务表的行数与数据指纹全部一致，
预演数据库随后删除。正式迁移独立运行 Alembic，没有启动第二套业务 worker。

备份目录：
`/opt/openbox/backups/20260910-mobile-push-0513dcb/preflight-20260910T093222Z/`。
其中 `preflight.dump` 和切换前的 `activation.dump` 均通过 `pg_restore --list` 校验，
同时保存旧配置、容器环境快照、迁移日志、授权中断记录和验收报告。

实际发布镜像在禁用网络的容器中加载本地推送凭据，完成 APNs 签名验证和极光请求生成检查。
上线后再次确认 APNs / JPush 均已启用，私钥哈希与本地一致，未出现推送 worker 警告或
Python traceback。公网首页与 `/api/environment` 返回 200，环境接口内容哈希与发布前一致；
匿名 `/api/push/status` 返回预期的 401。后端健康由容器内 8080 端口的健康检查确认，
公网 `/health` 是前端 HTML 回退页面，不能作为后端健康依据。

本次没有进行真机通知送达测试。苹果签名及极光请求检查使用模拟传输，没有向真实设备发送通知。
临时 OSS 镜像对象已删除；发布镜像、数据库备份和补丁保存在服务器。

## 回滚注意

旧后端为 `openbox-backend:20260910-login-guidance-324366c2`。旧镜像的 Alembic 不认识
新 revision，不能直接恢复旧 tag 后执行默认启动迁移。若需回滚，应先备份当前数据库，
恢复本次备份中的后端配置及 override，并暂时将后端启动命令设为直接运行 Uvicorn，
保留新增表与数据；再仅重建 backend，确认健康并 reload 前端 Nginx。不要直接降级删表
或覆盖恢复整个生产数据库。

[结构化发布证据](evidence/mobile-push-deploy-20260910.json)包含镜像、备份、配置校验和服务状态，
不包含密钥值。
