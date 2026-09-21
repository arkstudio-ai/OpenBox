# Agent Team API 接入说明

2026-09-21。移动端界面设计和优化已按用户要求暂停；现有 Flutter 接入代码保留，后续界面可复用同一套接口。实际请求字段以运行服务的 `/openapi.json` 为准。

## 发起与确认

沿用普通聊天的账号认证、`X-Workspace-Id`、会话和 Inbox 提交。`POST /api/agent/session/{session_id}/prompt_async` 示例：

```json
{
  "text": "请协作完成这个任务，并给出完整结论。",
  "agent": "team",
  "model": "openai/qwen3.8-flash",
  "delivery": "followup",
  "client_message_id": "本次提交的稳定唯一标识",
  "team_request": {
    "template_id": "auto",
    "allow_supplement": true,
    "requested_agent_ids": []
  }
}
```

`template_id` 可替换为已启用模板 ID。`requested_agent_ids` 为用户点名的已保存 Agent；`allow_supplement` 控制协调者能否在已确认授权内补充成员。普通聊天不传 `team_request`。先展示既有问题接口返回的 `team_lineup` 详情，由用户确认；客户端不直接创建已运行团队。

## 查询与控制

以下路径均以 `/api` 开头。

| 用途 | 接口 | 关键约定 |
|---|---|---|
| 模型目录 | `GET /agent-capabilities/models` | 使用返回的模型 ID 和 `reasoning_variants`，不猜测推理档位 |
| Agent / 团队模板 | `GET /agent-definitions`、`GET /team-definitions` | `status=active`、`search`、`cursor`；响应 `items`、`next_cursor` |
| 历史版本 | `GET /{agent,team}-definitions/{id}/versions` | 已发布版本不可变；临时 inline 成员没有库版本 ID |
| 运行记录 | `GET /team-runs` | 可按 `session_id`、`project_id`、`template_id`、`status` 筛选和分页 |
| 快照 | `GET /team-runs/{id}` | `seq`、`run`、`policy`、`members`、简化 `tasks`、计数、连接和通知 |
| 详情 | `GET /team-runs/{id}/{collection}` | collection 为 members / tasks / attempts / messages / artifacts；`offset`、`limit`，响应 `next_offset`、`seq` |
| 事件补齐 | `GET /team-runs/{id}/events?after_seq=N` | 顺序处理 `events`；按 `seq`、`last_seq`、`has_more` 补齐 |
| 用量 / 文件变化 | `GET /team-runs/{id}/usage`、`GET /team-runs/{id}/diff` | 汇总实际账本；未知费用独立显示 |
| 暂停 / 继续 / 取消 | `POST /team-runs/{id}/{pause,resume,cancel}` | `Idempotency-Key` 头，正文 `expected_revision`，可带 `reason`；202 不是最终状态 |
| 授权和运行限制 | `POST /team-runs/{id}/grant` | 同样使用幂等键和当前版本；只提交用户明确修改的字段 |

创建/修改定义、发布版本、另存模板等写操作同样要求 `Idempotency-Key`；编辑已有记录必须提交其当前 `expected_revision`。查看版本、改草稿、发布新版本是独立操作。

## 状态和异常

- 团队状态包括 provisioning、running、waiting、pausing、paused、canceling、completing、completed、failed、canceled。等待或暂停保留已有成果；完成以服务端状态为准。
- `team.run.updated` 用于提示重新取数。保留最后成功快照，断线后补事件或刷新快照；旧响应不能覆盖较新的 `seq`。成员聊天增量仅订阅当前查看的成员，离开后解除订阅。
- `409 STALE_REVISION` 应刷新当前快照，再由用户按新状态重试；网络响应丢失时使用原幂等键重试，不能生成新的重复动作。
- `409 INVALID_EVENT_WATERMARK` 应重新获取快照，不能无限重试无效序号。
- HTTP 业务错误位于 `detail`：包含 `code`、`message`，以及可能存在的 `current`。成员遇到模型 429/503，由后端有界退避；连续协调者失败会暂停受影响团队，其他团队可继续。
- 用户或工作空间切换后，旧请求和旧 WS 事件不得写入新作用域。不可访问资源按 403/404 处理。

## 积分与最终总结

没有独立团队预算。`TeamPolicy` 不再返回 `budget_credits`；历史入参会被忽略。付费工具授权只保留 `authorized`，消费统一由账户账本结算；未知费用不能当作零。

团队完成后，完整 `team_finish.summary` 写入普通聊天的最终正文，刷新和服务恢复后仍可读取。进度卡展示任务状态，完整答案展示在聊天正文。成员会话只读，用户补充任务通过根会话发送。

已有接入可参考 `frontend-v2/src/features/agent-team/api/teams.ts`、`mobile/lib/features/teams/api/teams_api.dart` 和 `mobile/lib/shared/models/team.dart`。移动接口专项测试覆盖版本/幂等、作用域切换隔离和团队请求参数传递。
