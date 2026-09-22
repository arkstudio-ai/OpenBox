# 内置技能包

所有随 OpenBox 后端发布的技能说明、脚本和参考资料统一放在此目录。
[catalog.json](catalog.json) 是唯一登记清单；[builtin.py](../builtin.py) 根据模块位置解析包，
不依赖服务启动目录或无影是否在线。当前为 6 类、8 个技能。

| 分类 | 技能 | 用途 |
|---|---|---|
| `authoring` 技能制作 | [skill-creator](authoring/skill-creator/SKILL.md) | 创建和整理技能包 |
| `automation` 自动化 | [scheduled-tasks](automation/scheduled-tasks/SKILL.md) | 通过对话创建定时任务 |
| `media` 图片与视频 | [imagegen](media/imagegen/SKILL.md)、[video-production](media/video-production/SKILL.md) | 图片生成与编辑、视频制作 |
| `publishing` 内容发布 | [douyin-desktop-publish](publishing/douyin-desktop-publish/SKILL.md)、[douyin-publish](publishing/douyin-publish/SKILL.md) | 云桌面发布、开放平台扫码投稿兜底 |
| `marketing` 营销 | [marketing-autopilot](marketing/marketing-autopilot/SKILL.md) | 自动营销任务流程 |
| `browser` 浏览器 | [dev-browser](browser/dev-browser/SKILL.md) | 浏览器操作与页面数据读取 |

## 新增技能

1. 在已有类别下创建 `<group>/<skill-name>/SKILL.md`。名称保持小写英文与连字符，
   frontmatter 的 `name` 必须与目录和清单一致，并提供非空 `description` 和正文。
2. 附属 `references/`、`scripts/`、`assets/` 随技能包放置；不要复制到工具目录或启动目录。
3. 在 `catalog.json` 的 `skills` 中登记唯一的 `name` 和 `group`。确需新增分类时，
   在 `groups` 中登记稳定 ID 及 `zh-CN` / `en-US` 标签；API 和 Web 直接使用这些标签。
   为技能填写 `display_name`、`display_description` 的中英文文案；它们仅用于界面，
   不能替换稳定 `name`、模型发现使用的 `description` 或技能正文。
4. 工具声明只描述依赖，不授予权限。真实动作继续通过工具与服务执行，并处理依赖不可用。
5. 更新[技能参考](../../../docs/reference/SKILLS.md)及相关行为测试。在 `backend/` 下验证：

   ```bash
   uv run python -m skill.builtin
   uv run pytest tests/unit/test_builtin_skills.py -q
   ```

校验会拒绝重复名称、缺失包、越界路径、身份不一致和未登记的 `SKILL.md`。
新增或修改登记清单后重新发布/重启后端，以更新已有运行时 Provider。

## 运行与打包

- `BuiltinSkillProvider` 把本目录作为 `builtin-package` 全局来源；旧版宿主调用和管理 API
  也从同一清单发现技能。已知同名云镜像内置副本不覆盖当前后端版本。
- 用户项目技能、个人库、商店安装仍通过各自 Provider 管理；不能为方便分类把用户技能
  或商店候选写入本目录。`.openbox/skills/` 保留为项目自定义技能的约定路径。
- 用户技能可在 `SKILL.md` frontmatter 声明相同双语字段；添加表单的覆盖文案随安装目录
  的 `openbox-display.json` 保存。包级名称和多技能包内各成员的调用标识独立。
- 后端发布包包含技能正文和资源。Docker 沙箱按清单安装至 `/opt/openbox/skills/<name>/`，
  保持执行环境中的路径兼容；仓库分类目录不会改变技能名称或远程安装路径。
- 浏览器运行时代码继续位于 [container/dev-browser](../../../container/dev-browser/README.md)。
  后端恢复装配与无影安装脚本将该运行时代码和本目录的浏览器技能包组合交付。
- 页面能列出或模型能加载说明，不代表依赖服务已连接；读取宿主资源、执行脚本和调用外部
  服务仍须通过对应资源接口和允许的执行工具。

完整的来源、覆盖和冻结规则见[技能参考](../../../docs/reference/SKILLS.md)和
[Provider 生命周期](../../../docs/architecture/SKILL_PROVIDER_LIFECYCLE.md)。
