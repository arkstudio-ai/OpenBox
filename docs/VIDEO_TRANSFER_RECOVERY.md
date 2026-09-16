# 视频转存失败的恢复策略

## 2026-09-16 问题定位

gw2 的一个 2026-09-11 历史视频任务，上游状态已是 `completed`，但任务查询接口仍返回
2026-09-12 06:42:31 UTC 过期的 TOS 签名下载链接。2026-09-16 再次查询并发起只读下载探测，
返回 HTTP 403 / `AccessDenied`，响应明确指出过期。失败发生在下载供应商视频，尚未上传 OSS。

原逻辑每次失败都刷新 `updated_at`，使任务持续满足按更新时间筛选的 7 天恢复窗口，
每 2～3 分钟重复同一失败。Trace 无积压；视频恢复问题与 Trace 状态映射问题分属不同模块。

## 修复行为

- 每次恢复仍先按任务保存的供应商路由查询状态，使用这次查询得到的结果链接。
- 下载返回 401/403 且签名字段明确已过期时，任务结束为 `failed`，记录
  `source_url_expired` 和可读原因，停止自动重试。无签名或签名仍有效的 403 不直接判为过期。
- 临时转存故障保留 `transfer_failed`，依次等待 2、5、15、30、60 分钟，后续间隔为 60 分钟。
  每个任务最多尝试 8 次，或自首次转存尝试起 24 小时；到期后停止自动恢复。后台扫描的调度粒度仍为分钟。
- 次数、首次尝试时间和下次重试时间存入现有 `video_jobs.result_data.transfer`；进程重启不重置。
  后台恢复、聊天内轮询及事务内最终领取均检查退避，防止不同入口绕过限制。
- 任务领取时仅持有短数据库行锁，网络传输在事务外进行。已付费任务 ID 与生成次数保持原语义；
  自动恢复不会再次提交生成请求，停止后的工具结果也明确要求人工处理，不自动重新付费生成。
- 已确认过期的旧链接不能由本地代码重新签名。取回旧视频需要供应商提供有效下载链接；
  将任务明确结束为失败不代表视频已恢复。

无需数据库迁移。已有成功任务、正常下载转存和供应商路由校验沿用原行为。

## 生产验证

PR [#46](https://github.com/arkstudio-ai/OpenBox/pull/46) 已合并并于 2026-09-16 09:57（北京时间）
发布 gw2，backend / worker 镜像为 `20260916-video-transfer-908c1c4`。09:58:47 的正常恢复扫描
将该历史任务结束为 `failed`，记录 `source_url_expired`、`stopped=true`、`next_retry_at=null`，
原生成次数仍为 1。Trace job 同步失败状态，投影积压 0；服务健康、无新增 ERROR 或 traceback。
备份与回滚信息见 [部署记录](DEPLOY.md)。

## 验证

覆盖签名有效期识别、下载与上传 403 区分、错误信息不泄露签名、持久化退避、过期终止、
尝试次数/时间上限、聊天轮询停止以及跨进程恢复不重复生成：

```bash
cd backend
.venv/bin/python -m pytest -q \
  tests/unit/test_video_transfer.py \
  tests/unit/test_video_job_recovery.py \
  tests/unit/test_video_production.py \
  tests/unit/test_video_error_text.py \
  tests/unit/test_video_spend_guards.py \
  tests/integration/test_video_restart_recovery_e2e.py
```
