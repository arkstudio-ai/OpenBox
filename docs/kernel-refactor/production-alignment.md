# 线上和主干对齐：本地部分（第 0 步 0-6）

日期 2026-11-28。对应 `docs/KERNEL_DSH_COMPARISON_20261128.md` 第 24 节第 3 条和 25.2 的 0-6。只做了本地能做的部分：读线上底版提交的改动，核对它们在当前代码里是否都在，把没带回主干的测试改动补上。没有登录任何服务器。证据标记沿用主文档。

## 结论

- **三个源文件里，线上的修复全部已在主干，没有需要补的代码。**【已核对，逐个改动块】
- **线上底版里的测试改动原来都不在主干，现在补上了**，对当前代码全部通过。改动只有一处是为了适应代码的变化（见下文"测试"）。
- 剩下的只能由有 gw2 权限的人做：比对 11 月 19 日热修的四个文件。命令在最后一节。

两个线上底版提交挂在本地分支上：`prod-base/20261121-c1185d18`（`7fbdc87f` 上的 `41968899`、`7c959a86`、`c1185d18`）和 `prod-base/20261122-5e6afbd4`（`81105ced` 上的 `5e6afbd4`）。主干在 `8dffa516` 把这些源码改动记了回来。核对用的是分支 `kernel-refactor/round1`（`a9da02c8`），这三个文件在它和 `main@b18b3a47` 上相同。

## 三个源文件

方法：`git show 41968899 7c959a86 c1185d18 5e6afbd4 -- <文件>` 逐个改动块读，再到当前文件里找对应的代码；另外用 `git diff c1185d18 HEAD -- <文件>` 看当前版本相对线上底版删掉了什么，确认删掉的行都与这些修复无关。

### `backend/light/silk_road.py`

| 提交 | 改动 | 当前位置 | 状态 |
|---|---|---|---|
| `41968899` | `SEEDANCE_FRAME_SIZE` 改名 `BOUNDED_FRAME_SIZE`（1536×624） | `:31` | 在，逐字相同 |
| `41968899` | 新增 `frame_size(model)`：Turbo 和 Seedance 用 2.5:1 以内的尺寸 | `:39-40` | 在，逐字相同 |
| `41968899` | `render` 里按模型取 `input_size` | `:318` | 在 |
| `41968899` | 已存的帧：非 Turbo 直接复用；Turbo 先重新规整再用，规整结果等于原帧就不再发布 | `:391-404` | 在，逐字相同 |
| `c1185d18` | `image_size` 按 `input_size` 生成，替代写死的 `1536x608` | `:319`、`:402` | 在 |
| `c1185d18` | Turbo 复用旧帧时 `preserve_edges=True`（补边，不裁） | `:320`、`:397` | 在 |
| `c1185d18` | 起止帧提示词追加"超宽银幕构图……画幅校正时仍能完整看到主体" | `:377-381` | 在，逐字相同 |

`c1185d18..HEAD` 在这个文件里删掉的 11 行都属于之后的功能（多语言标题和片尾、配乐判定、`_REVEAL_ANCHOR` 改名 `MUSIC_REVEAL_SEC`），`render` 的帧处理部分一行没变。

### `backend/tool/video_runninghub.py`

当前文件 = `c1185d18` 的版本 + `5e6afbd4` 的改动块，逐字相同（`git diff c1185d18 HEAD` 只有 `5e6afbd4` 那 17 行）。所以三处都在：

| 提交 | 改动 | 当前位置 |
|---|---|---|
| `7c959a86` | 提交前校验帧素材（`FrameValidationError`、`MAX_FRAME_BYTES`） | `:16`、`:20-21` |
| `c1185d18` | 校验改为准备：`_adapt_frame` 补边/压缩出一份只给模型的副本，原图不动；`prepare_frame_assets` 存到 `analysis/<user>/runninghub-frames/` | `:24-82` |
| `5e6afbd4` | 识别"下载输入素材失败"（1007）：`INPUT_DOWNLOAD_FAILED`、`input_download_failed`、`terminal_input_download_failure`，错误文案 | `:17`、`:104-116`、`:125-126` |

### `backend/tool/video_production.py`

主干之后改动最多的一个（`c1185d18..HEAD` 有 444 行差异），逐块找：

| 提交 | 改动 | 当前位置 | 状态 |
|---|---|---|---|
| `7c959a86` | RunningHub 能力说明改写 | `:2353-2358` | 在，内容已被 `c1185d18` 的版本取代（同线上） |
| `7c959a86` | 抽出 `_validate_provider_inputs`，validate 和 submit 共用；RunningHub 有输入时校验帧素材 | `:2416-2436` | 在，已按 `c1185d18` 改名 `_prepare_provider_inputs`；主干另加了 ComfyUI 分支 |
| `7c959a86` | validate 失败时元数据带 `frame_validation: True` | `:2464` | 在 |
| `7c959a86` | `guard_paid_step` 挪到输入校验之后（校验不过就不占预算） | `:2960-2964` | 在 |
| `c1185d18` | `_presigned_provider_refs` 接受 `provider_image_keys`，给模型的签名链接指向副本 | `:551`、`:569`、`:2970` | 在 |
| `c1185d18` | submit 时 `save_copies=True` 存副本 | `:2960-2962` | 在 |
| `c1185d18` | 轨迹记录把副本链接归到原素材（`retain_derived_media_inputs`） | `:3203-3213` | 在 |
| `5e6afbd4` | 新增 `_provider_failure`：失败文案统一；RunningHub 终态的下载失败记 `failure_code` | `:862-871` | 在，逐字相同 |
| `5e6afbd4` | 首次 POST 就返回失败/取消时，用 `_provider_failure` 写入 | `:3241-3249` | 在 |
| `5e6afbd4` | 去掉 submit 后立即等待分支里重复的 `_update_job(error=…)` | `:3270-3272` | 在 |
| `5e6afbd4` | 轮询拿到失败时用 `_provider_failure` | `:3577-3580` | 在 |
| `5e6afbd4` | 返回元数据带 `failure_code` | `:3687-3688` | 在 |

顺带核对：`7c959a86` 对 `light/remake.py` 的 2 行（帧校验不过时说"未提交"并带上原因）也在主干，`light/remake.py:406-408`，主干改写成了三语。第 24 节说主干的 `remake.py` 与 `5e6afbd4` 逐字节一致，而 `5e6afbd4` 不含 `7c959a86`；实际上 `5e6afbd4` 的 `remake.py` 已经带着这一处（`5e6afbd4:backend/light/remake.py:349`），所以不缺。

## 测试

### 带过来了什么

| 来源 | 文件 | 内容 | 是否改动 |
|---|---|---|---|
| `5e6afbd4` | `tests/unit/test_reference_film.py` | 只在确认是下载失败时换签名重提（3 组参数）；同一轮重提过就不再重试；续跑旧的 1007 作业时去查原始失败原因 | 改了：三处 `ctx` 从 `SimpleNamespace`/`None` 换成真的 `ToolContext`。主干的 `remake._job` 现在对片段作业做 `dataclasses.replace(ctx, transient_output=True)`，假对象会直接 `TypeError` |
| `5e6afbd4` | `tests/unit/test_video_runninghub.py` | 只有终态的下载失败才带 `failure_code`，错误文案不回显签名（4 组参数） | 原样，补丁能干净打上 |
| `41968899` | `tests/unit/test_light_video_tiers.py` | 快速档也检查丝路模板的帧宽高比；新增 `test_turbo_reuses_corrected_silk_road_frames` | 原样 |
| `7c959a86` + `c1185d18` | `tests/unit/test_video_runninghub.py` | `_frame` 辅助函数；`test_other_media_cannot_be_used_as_frames`；`test_wide_and_tall_frames_are_prepared_without_restricting_user_images`（4 组参数）；`test_preparation_compresses_large_files_and_preserves_edges` | 原样（只把文件头已有的 import 去重）。补丁本身打不上，因为主干 `6e927a0d` 重写了同一段 |

没有带的：线上对 `test_validate_checks_explicit_intent_and_reports_asset_bindings` 的修改（给假素材补 `size`、模拟 OSS）。主干 `6e927a0d` 已经按自己的方式改过这个测试并覆盖了同一件事；`7c959a86` 的 `test_real_frame_dimensions_are_checked_before_any_paid_request` 在线上的 `c1185d18` 里就被上面的"wide and tall"测试取代了，线上最终也没有它。线上删掉的 `test_unsupported_image_formats_are_not_sent_as_png` 主干也已删掉（GIF 现在会被转换）。

没有发现主干缺的修复：带过来的测试都是直接对当前代码通过的。

### 跑了什么

【已核对】本机，`/Volumes/fanxiang/workspace2/OpenBox/backend/.venv/bin/python -m pytest -c pyproject.toml --rootdir . … -p no:cacheprovider`，`import agent` 解析到本工作树。

- `test_reference_film.py`、`test_video_runninghub.py`、`test_light_video_tiers.py` 一起跑：80 条全部通过。补丁打上、`ctx` 还没改时，`test_reference_film.py` 新增的 5 条因上面说的 `TypeError` 失败，改完通过。
- 碰到这几个模块的回归：`test_silk_road.py`、`test_light_shot_editor.py`、`test_light_production.py`、`test_video_submit_regression.py`、`test_image_runninghub.py`、`test_soda_story.py`，130 条全部通过。

## 要有 gw2 权限的人做的：四个热修文件

背景见主文档第 24 节第 3 条：11 月 19 日的补丁镜像 `20261119-qwh-space-512c9d9b` 在 `20261117-streamreplay-43bc4bce` 上替换了四个文件，提交 `512c9d9b` 本机没有。之后 21 日、22 日的线上镜像是不是叠在它上面，部署记录没写。

本地已经确定的【已核对】：这四个文件在 `43bc4bce`、`c1185d18`、`5e6afbd4` 和 `main@b18b3a47` 上**完全相同**，也就是任何一次从这些提交全量构建得到的都是同一份。它们的 sha256：

| 镜像里的路径 | 仓库路径 | main 的 sha256 |
|---|---|---|
| `/app/sandbox/runtime_operation.py` | `backend/sandbox/runtime_operation.py` | `8d0572401090a1ea84a4b1b672f24771114afeff129e55ca82282570014e4edc` |
| `/app/tool/bash.py` | `backend/tool/bash.py` | `c06e0c80fa3356d621b763f2aae7c02ebaece17ee001bcbc7217286f3c81ac00` |
| `/app/permission/permission.py` | `backend/permission/permission.py` | `bba774b1f4b361c3a8a2e04d8961b67930a45965c4301e40ed5e69556d62e403` |
| `/container/cache_api.py` | `container/cache_api.py` | `77c9fbff5a8ae30f53af28d8ff5854bbb7c819d0a173736ebfd5fc0795d07158` |

镜像路径来自 `backend/Dockerfile`：`WORKDIR /app` + `COPY backend/ .`，`COPY container/*.py /container/`。

在 gw2 上执行（都只读；`docker run --rm` 只从镜像起一个临时容器算哈希，不碰运行中的服务）：

```sh
cd /opt/openbox
F="/app/sandbox/runtime_operation.py /app/tool/bash.py /app/permission/permission.py /container/cache_api.py"

# 1. 现在线上跑的是哪个镜像
docker compose ps --format '{{.Service}}  {{.Image}}  {{.Status}}'

# 2. 运行中的 backend 和 trajectory-worker 里这四个文件的哈希
docker compose exec -T backend sha256sum $F
docker compose exec -T trajectory-worker sha256sum $F

# 3. 热修镜像和它的底镜像里的哈希：认出哪一份是热修
for tag in 20261119-qwh-space-512c9d9b 20261117-streamreplay-43bc4bce; do
  echo "== $tag"
  docker run --rm --network none --entrypoint sha256sum "openbox-backend:$tag" $F
done

# 4. 线上镜像是不是叠在热修镜像上（看构建层）
docker image history --no-trunc "$(docker compose ps --format '{{.Image}}' backend)" | head -40

# 5. 运行中的容器有没有被手工改过源码
docker diff "$(docker compose ps -q backend)" | grep -E '\.py$' || echo "no .py changes"

# 6. 热修的发布目录里有没有留下文件本身
ls -la /opt/openbox/releases/20261119-qwh-space-512c9d9b/ 2>/dev/null
find /opt/openbox/releases/20261119-qwh-space-512c9d9b -name '*.py' -exec sha256sum {} + 2>/dev/null

# 7. 有差异时把线上版本取出来比对（只写 /tmp）
mkdir -p /tmp/align-0-6
for f in $F; do docker compose exec -T backend cat "$f" > "/tmp/align-0-6/$(basename "$f")"; done
```

怎么判断：

- 第 2 步四个哈希都等于上表：热修已经被冲掉（21 日、22 日是全量构建，或热修被撤回）。要确认热修的内容是不是已经不需要，而不是丢了，然后之后的发布可以从主干全量构建。
- 和上表不同、和第 3 步里 `512c9d9b` 的相同：热修还在线上，而且只在线上。用第 7 步取出的文件和主干逐行比，把修复合回主干。合回之前，后端不能从主干全量构建；第 1 步"下发连接保活修复"也要等这一步，因为下发脚本会连 `cache_api.py` 一起推（主文档第 24 节）。
- 和两者都不同：线上还有别的改动，同样取出来比。

拿回来的文件在本地这样比（在任何一个主干的检出里）：

```sh
git diff --no-index backend/tool/bash.py /path/to/align-0-6/bash.py
```

`container/cache_api.py` 还会被下发到云桌面上运行；桌面上的版本不在这次比对里，按 `docs/WUYING_SANDBOX.md` 的发布流程另行盘点。
