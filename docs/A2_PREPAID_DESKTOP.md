# A2 · 包月开通参数 —— 独立执行单

> 2026-09-03。从 `DETAILED_PLAN_M1_M2.md` v2 的 A2 抽出，给 Codex 目标模式单独执行。规模小：一个会话。
> 原执行边界经产品决策放宽：本项允许为验收新购 **至多一台** 包月桌面，验收完成后立即退订；不改 gw2 的常驻计费配置，现有上海包月机仍留给 A3。
>
> 执行者须知：真机验收限上海、`eds.enterprise_office.4c8g`、50G、1 月、`auto_pay=true`、`auto_renew=false`，询价不得超过 ¥300；使用唯一验收标签，验收后先确认实例唯一性和退款金额，再退订。遇到 §9 情形停下来报告。

---

## 1. 目标

**一句话**：让 `create_desktop` 在 `WUYING_CHARGE_TYPE=PrePaid` 时能正确下单（period / period_unit / auto_pay / auto_renew），提供询价与续期封装，并把鬼桌面处理改成「包月不硬删」。

**完成定义**：
1. PrePaid 时请求带 `period=1, period_unit=Month, auto_pay=true, auto_renew=false`（都可配），PostPaid 时不带这四个参数；PrePaid 且 `auto_pay=false` 启动即拒绝（与策略组、镜像的 fail-fast 先例一致）。
2. `wuying_ecd` 新增 `describe_price()` 与 `renew_desktop()` 封装；`describe_desktop()` 返回体带 `charge_type`、`expired_time`。
3. `cloud_desktops` 记 `charge_type`、`expires_at`（A3 直接复用）。
4. 鬼桌面：PrePaid 桌面不调 `DeleteDesktops`，改为撤销通道 + 行标记 + 审计 + 告警日志；PostPaid 保持现行硬删。
5. 冒烟脚本新增只读 `price` 层；`full` / `channel` 层在 PrePaid 配置下默认拒绝，需 `--allow-prepaid` 才放行。
6. 单测全绿；gw2 配置不变（仍 PostPaid）。

**非目标**：预热池、状态机、`ModifyDesktopChargeType`（A3）；订阅到期巡检与 RenewDesktops 的调用时机（B4）；Expired 真机观察（见 §4.6，等自然到期）。

**前置条件**：`docs/A1B1_MERGE_SEAM.md` 已合入 `main`。本项改 `wuying_ecd.py`、`wuying_desktop_service.py`、冒烟脚本，与接缝任务同文件；接缝未合并前不要开工。

---

## 2. 现状（2026-09-03 实查，行号以 A1 分支为准，合并后会漂移）

| 事实 | 位置 |
|---|---|
| `create_desktop` 只传 `charge_type=config.wuying_charge_type`，其余为 region / office_site / policy_group / desktop_name / amount=1 / end_user_id / 三个标签 / `desktop_attachment(image, desktop_type, disk)`；外面套了 `_retry_throttled`（A1 加的，处理 `Throttling`） | `backend/sandbox/wuying_ecd.py:240-295` |
| `wuying_charge_type` 默认 `"PostPaid"`，无取值校验；`wuying_system_disk_size` 已是 50；`wuying_desktop_type` = `eds.enterprise_office.4c8g` | `backend/core/config.py:374-382, 635-638` |
| `release_ghost`：`wuying_channel.revoke` → `wuying_ecd.delete_desktop`（连带删 `obx-*` EndUser）→ 软删行；对所有桌面一视同仁 | `backend/sandbox/wuying_desktop_service.py:140-154`、`wuying_ecd.py:360+` |
| 没有任何 `DescribePrice` / `RenewDesktops` 调用 | 全库 grep |
| SDK `CreateDesktopsRequest` 字段含 `auto_pay, auto_renew, period, period_unit, charge_type, ...`；`DescribePriceRequest` 含 `amount, instance_type, period, period_unit, region_id, resource_type, root_disk_size_gib, root_disk_category, user_disk_size_gib, os_type, internet_charge_type, bandwidth, group_desktop_count, promotion_id`；`RenewDesktopsRequest` 含 `desktop_id, period, period_unit, auto_pay, auto_renew, region_id, resource_type`；`ModifyDesktopChargeTypeRequest` 含 `desktop_id, charge_type, period, period_unit, auto_pay, use_duration`；`DescribeDesktopsResponseBodyDesktops` 含 `charge_type, expired_time, desktop_status, host_name, network_interface_ip, ...` | `backend/.venv/lib/python3.14/site-packages/alibabacloud_ecd20200930/models.py` |
| bossip 的包月购买命令与踩坑：必须 `--auto-pay true`，否则只生成待支付订单；询价 `describe-price --resource-type Desktop --instance-type eds.enterprise_office.6c12g --charge-type PrePaid --period 1 --period-unit Month --amount 1 --root-disk-size-gib 50 --os-type Linux`；分项价 ≈ SKU 打包价 + 盘差，控制台 SKU 价为准绳；`root-disk-size-gib` 必须 ≥ 镜像 Size | `~/arkstudio/bossip/apps/codex/v1/deploy/wuying/ECD_FLEET_PURCHASE_API.md` §0/§3/§4 |
| `cloud_desktops` 现有列（A1 后）：`id, workspace_id(接缝后), user_id, desktop_id, end_user_id, region_id, status, error, channel_kind, private_ip, tunnel_port, tunnel_bind, tunnel_pubkey, tunnel_fingerprint, action_api_key_hash, action_api_key_ciphertext, tunnel_state, last_seen_at, channel_error, is_deleted, ...` | `backend/db/models/cloud_desktop.py` |
| 冒烟脚本四层 `check / enduser / full / channel`，后两层计费，需 `--yes` | `backend/scripts/wuying_provision_smoke.py:344` |
| gw2：`WUYING_CHARGE_TYPE=PostPaid`（默认），镜像 v2，磁盘 50；上海现存包月机 `ecd-8zp47qagrsc95h67t`（openbox-dev-shanghai，2026-09-01 购，到期约 10-01）留给 A3 入池 | 验收人实查 |
| 阿里云 ECD 包月到期后的保留天数与「Expired 期间 RenewDesktops 能否直接恢复」**未核实**，计划暂按 15 天 | `DETAILED_PLAN_M1_M2_REVIEW.md` §2.2 |
| ECD CLI：`--api-version 2020-09-30`，`--region` 与 `--biz-region-id` 必须同时给；本机 TUN 代理挡 `ecd.cn-shanghai`，上海实调在 gw2 容器里跑 | 既有记录 |

---

## 3. 必要资料
| # | 资料 | 用途 |
|---|---|---|
| 1 | 接缝合并后的 `main` | 分支起点 |
| 2 | 本机 aliyun CLI / AK 能调 `DescribePrice`（只读） | AC-3 |
| 3 | gw2 容器内跑冒烟 `price` 层的方式（同 A1：`docker exec openbox-backend-1 ...`） | AC-3 |
| 4 | 单价上限的暂定值（用于 `price` 层的告警阈值展示，默认 `POOL_MAX_UNIT_PRICE_CNY=300`，A3 再定） | 配置 |

---

## 4. 方案

### 4.1 配置（`core/config.py` + `.env.example`）
- `wuying_charge_type: Literal["PostPaid","PrePaid"] = "PostPaid"`（改为受限取值）。
- 新增 `wuying_period: int = 1`、`wuying_period_unit: Literal["Month","Year"] = "Month"`、`wuying_auto_pay: bool = True`、`wuying_auto_renew: bool = False`，env 名 `WUYING_PERIOD / WUYING_PERIOD_UNIT / WUYING_AUTO_PAY / WUYING_AUTO_RENEW`。
- 校验：`charge_type == PrePaid and not auto_pay` → 启动抛 `ProvisioningConfigError`（同策略组先例）。
- `pool_max_unit_price_cny: float = 300`（env `POOL_MAX_UNIT_PRICE_CNY`），本项只用于 `price` 层比对与日志，不阻断。

### 4.2 `wuying_ecd.py`
- `create_desktop`：`charge_type == "PrePaid"` 时追加 `period / period_unit / auto_pay / auto_renew`；PostPaid 不传（用 kwargs 拼装，单测断言两种形态）。
- 新增 `describe_price(charge_type: str, *, period=None, period_unit=None, amount=1) -> dict{"currency","trade_price","original_price","raw"}`：当前 ECD `DescribePriceRequest` 实际没有 `charge_type` 字段；用 `period / period_unit` 表达包月询价，PostPaid 则不传这两个字段。其余参数为 `region_id, resource_type="Desktop", instance_type, amount, root_disk_size_gib, os_type="Linux"`。响应先保留 raw，再从 `PriceInfo.Price` 解析。
- 新增 `renew_desktop(desktop_id, period, period_unit, auto_pay=True, auto_renew=False)`：`RenewDesktopsRequest(region_id, desktop_id=[...], period, period_unit, auto_pay, auto_renew)`，套 `_retry_throttled`。**本项只封装 + 单测打桩，不实调**。
- `describe_desktop()` 返回体加 `charge_type`、`expired_time`（原样字符串）。

### 4.3 数据
- 迁移（一个修订，`down_revision` = 接缝修订）：`cloud_desktops` 加 `charge_type String(16) nullable`、`expires_at DateTime(tz) nullable`。
- `provision` 成功后写 `charge_type`；`_resync` / 巡检把 `expired_time` 同步进 `expires_at`（解析失败记日志不抛）。

### 4.4 鬼桌面（`wuying_desktop_service.release_ghost`）
```
charge = record.charge_type or (describe_desktop → charge_type) or config.wuying_charge_type
PrePaid:  revoke 通道 → 行 status="reclaimed", error="ghost", 软删 → audit.record(workspace, "desktop.ghost", desktop_id, {charge_type, expired_time}) → log.error 带 desktop_id（告警占位，A4 接看板）
PostPaid: 现行为不变（revoke → delete_desktop → 软删）
```
不动订阅状态（本项也没有订阅表）；下一次 `provision` 会新建（PostPaid）或由 A3 池重分配（以后）。

### 4.5 冒烟脚本
- 新增 `price` 层（只读、免费）：分别询 PostPaid 与 PrePaid(1 月) 价，打印 raw 与解析结果，与 `POOL_MAX_UNIT_PRICE_CNY` 比对给出 `OK / OVER` 标记；退出码 0。
- `full` / `channel` 层：若生效的 `charge_type` 为 PrePaid 且未给 `--allow-prepaid`，打印「包月桌面每次一个月费用，需 --allow-prepaid」并退出 2。
- 文档：`docs/WUYING_SANDBOX.md` 加「包月参数」小节，写清四个配置项、`auto_pay` 的坑、鬼桌面两种分支、Expired 观察待办。

### 4.6 不做但要记录
- 官方文档已确认 Expired 后保留 15 天、期间可续费、第 16 天自动释放；`RenewDesktops` 对现有上海桌面的真实恢复表现仍等 `ecd-8zp47qagrsc95h67t` 10 月初自然到期时观察。

---

## 5. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 参数形态 | 单测：PrePaid → 请求含 `period=1, period_unit="Month", auto_pay=True, auto_renew=False`；PostPaid → 四个字段均为 None/未设置 |
| AC-2 | fail-fast | `WUYING_CHARGE_TYPE=PrePaid WUYING_AUTO_PAY=false` 启动（或 `get_config()` 校验）抛 `ProvisioningConfigError`；`WUYING_CHARGE_TYPE=Foo` 被拒 |
| AC-3 | 询价实调 | gw2 容器内 `wuying_provision_smoke.py price` 成功打印 PostPaid 与 PrePaid 两个价格及 raw；PrePaid 价在合理区间（参考 bossip 6c12g/50G 实测 ¥241.5/月，4c8g 应更低）；账单无任何新增费用 |
| AC-4 | 鬼桌面分支 | 单测：PrePaid 记录 → 不调 `delete_desktop`，行 `status=reclaimed`、软删，`audit` 被调；PostPaid 记录 → 调 `delete_desktop`（现行为） |
| AC-5 | 数据 | 迁移可升降；`provision` 后行有 `charge_type`；`_resync` 后 `expires_at` 有值（用打桩的 `describe_desktop` 返回 `expired_time`） |
| AC-6 | 冒烟保护 | PrePaid 配置下 `full` / `channel` 不带 `--allow-prepaid` 退出 2 且未调 `CreateDesktops`（单测打桩断言） |
| AC-7 | 测试与不越界 | 后端单测除既有 4 个视频配置用例外全绿；`git diff --stat main` 只含 `core/config.py`、`.env.example`、`sandbox/wuying_ecd.py`、`sandbox/wuying_desktop_service.py`、`db/models/cloud_desktop.py`、一个迁移、`scripts/wuying_provision_smoke.py`、测试、`docs/WUYING_SANDBOX.md` |
| AC-8 | 单台闭环 | 前后清单证明只新增一台唯一验收机；创建、Running、所有权、ticket、通道、bash 均通过；随后退订且清单/退款结果证明该机不再占用；gw2 `backend.env` 的 `WUYING_CHARGE_TYPE` 未改 |

---

## 6. 测试方式
```bash
cd backend && uv run pytest tests/unit -q
cd backend && uv run alembic heads
# gw2 容器内，只读：
docker exec openbox-backend-1 uv run python scripts/wuying_provision_smoke.py price
```

## 7. 交付证据
1. `git log --oneline main..<branch>`、`git diff --stat main`、`alembic heads`。
2. pytest 输出。
3. `price` 层完整输出（含 raw JSON）。
4. 执行前后 `describe-desktops` 两地域的桌面 id 列表、唯一验收标签、退订结果（证明至多一台且已回收）。
5. gw2 `grep WUYING_CHARGE_TYPE /opt/openbox/config/backend.env`。
6. §8 填好。

## 8. 执行记录（执行者填写）
- 分支 / 提交 / 迁移修订：
- `DescribePrice` 响应字段实际名称：
- 偏离：

## 9. 停下来报告
- 真机创建将超过一台、询价超过 ¥300、或实例不能由唯一验收标签确认。
- 退款金额不大于 0，或退款 API 参数/目标身份存在歧义。
- 接缝尚未合入 main。
- `DescribePrice` 返回结构与预期不符需要猜字段。
- 需要改通道/路由代码才能过验收。
