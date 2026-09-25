---
name: agent-team
description: 当用户明确要求创建 Agent Team、组队、多智能体团队，或“使用 agent team 分析/开发/审查”时使用。指导 Bossip 提交合法阵容、等待用户确认并启动团队；处理 team_propose 参数错误、模型能力不支持和团队执行阻塞。普通独立子任务不必使用此技能。
---

# 创建 Agent Team

在当前 Bossip 主对话中完成组队。用户明确指定 Agent Team 时，使用 `team_propose`；普通 `task` 子代理不等于团队，不能静默替代。不要为了本次协作先创建可复用 Agent 库条目，除非用户要求保存。

## 提案前

1. 从用户需求提取目标、输入位置、交付物和各成员职责。沿用已明确的信息与授权，仅询问真正缺失且阻塞执行的信息。用户要求下载仓库时，可先在主对话完成已授权的下载，取得所有成员可访问的实际路径。
2. 加载本技能后，检查当前可调用工具；工具未显示时可用 `capability_search` 查询 `team_propose`。若仍不可用，说明团队功能未开启或当前会话不允许组队，不声称已创建团队。
3. 若 `agent_manage` 可用，调用 `{"action":"list"}` 查看现有 Agent 与模型目录。只有用户要求复用 Agent 时才需要进一步 `get`。新任务可以直接用临时 `inline` 成员。用户指定的模型必须来自目录，且未标记 `unavailable_for_team`。所有成员及协调者都需要 `persona` 能力。
4. 未指定模型时省略模型字段，沿用服务配置的团队默认模型。用户指定全队模型时同时设置 `team.coordinator.model` 和每个成员的 `model_override`；只指定某成员则只覆盖该成员。不要猜模型 ID。

## 提交最小合法提案

如果消息带有用户选择的模板 `user_team_selection.template_id`，只提交顶层 `title` 与 `goal`，服务端会解析该模板。不要另外传 `template_id`、`use_template` 或重建阵容。

未选模板时，按任务制定少量互补成员。下面是已经取得代码路径后的双成员分析示例；提交前把目标、路径与职责替换为本次实际内容。

```json
{
  "title": "代码分析团队",
  "goal": "分析 /workspace/project 的架构和测试风险，交付有文件位置依据的结论。",
  "team": {
    "name": "代码分析",
    "preset_members": [
      {
        "alias": "architecture",
        "responsibility": "梳理架构与主执行流程。",
        "inline": {
          "name": "架构分析员",
          "description": "分析模块结构与调用关系。",
          "when_to_use": "需要理解代码架构和主要数据流时。",
          "instruction": "读取指定仓库，梳理模块边界和主执行流程；给出文件位置与证据，不修改代码。",
          "tool_allowlist": ["read", "glob", "grep"]
        }
      },
      {
        "alias": "reviewer",
        "responsibility": "核查测试覆盖、错误处理与可靠性风险。",
        "inline": {
          "name": "测试审查员",
          "description": "核查测试和潜在故障。",
          "when_to_use": "需要审查测试覆盖及错误路径时。",
          "instruction": "读取指定仓库的实现和测试，列出有证据的风险、影响及建议；标明未验证的推断，不修改代码。",
          "tool_allowlist": ["read", "glob", "grep"]
        }
      }
    ],
    "policy": {
      "member_selection": "explicit_only",
      "member_creation": "run_scoped",
      "max_members": 3,
      "max_concurrent_members": 2
    }
  }
}
```

- `title`、`goal` 必须在顶层；`team.name` 不能替代 `title`。
- 人数、工具范围及授权放在 `team.policy` 内；`max_members` 包含协调者。固定临时阵容使用 `explicit_only` + `run_scoped`。
- 每个成员只选 `inline` 或 `agent_ref` 之一。复用成员时使用目录返回的真实 ID，不能自造。
- 不需要的字段直接省略。尤其不要写 `reasoning: "default"/"auto"/"standard"`；需要覆盖时只能使用该模型目录列出的精确值。普通文字报告无需 `input_schema`、`output_schema`、`result_schema`。
- 发送一个完整 JSON 对象，检查括号和逗号，不要拼接多个对象或把 Markdown 围栏作为参数。
- 工具列表不等于操作授权。平台会保留基础文件工具；实际执行仍受权限约束。需要 shell、文件修改、桌面、付费或 MCP 操作时，按用户任务提供明确且最小的范围，交由平台确认，不能用通配授权掩盖权限错误。

调用 `team_propose` 会展示“组队方案”并暂停，等待用户确认。不要另发一个重复确认问题，不要在确认前调用 `team_member_start` 或宣称团队已运行。用户拒绝组队后按其选择继续，不再反复提案。

## 确认后执行

沿用服务端切换后的协调者协议：先用 `team_view` 读取真实状态，再用 `team_task_create` 建立有验收标准的交付任务，通过 `team_member_start` 启动已批准成员。任务必须包含实际输入路径、职责和预期结果，成员不会自动获得主对话的全部上下文。用 `team_wait` 等待结果，验收交付后才调用 `team_finish`。新增成员或扩大权限使用 `team_propose(mode="amend")`，不要越过原授权。

## 错误处理

- `invalid_json_arguments`：工具没有执行。根据行列提示重新提交完整合法对象；不要把错误理解为用户没提供标题或目标。
- 字段校验失败：仅修改错误指出的字段或层级，再提案；不要把整份 Schema 的默认值填进请求。
- `CAPABILITY_UNSUPPORTED`：读取 `current.model`、`missing_capabilities` 或 `reasoning_variants`。不需要的可选输出格式/推理覆盖可以省略；缺少 `persona` 属于模型渠道配置问题，缩短指令、删成员或换角色都不能修好。若没有符合用户模型要求的可用渠道，停止重试，报告未创建团队与具体缺失项，由用户或运维修正配置。不要自行改密钥、开关或模型能力声明。
- `TEAM_ADMISSION_DISABLED`、临时成员未开放或 `TOOL_NOT_TEAM_READY`：指出实际阻塞项；只有存在符合用户目标的已发布成员或获准工具时才调整方案。
- `PERMISSION_REQUIRES_USER`：汇总工具返回的具体操作范围，走团队调整确认；不要悄悄改用主对话或 `task` 绕过。

只有工具返回的团队状态能够证明创建与完成。失败时说清“团队尚未创建/尚未完成”，并保留用户明确要求的执行方式。
