# 语音通话移动端（Flutter）：通话页与顶部通话条

状态：**实施文档**。日期：2026-10-07。契约见 [总规格](VOICE_CALL_SPEC.md)；后端见 [后端文档](VOICE_CALL_BACKEND.md)；PC 端见 [悬浮窗](VOICE_CALL_WEB.md)。本文讲手机端怎么做：交互模型、画面、系统行为、音频、文件、测试、顺序。所有“现有”引用均已核对源码。

## 1. 原则

1. **不阻挡**：通话进行时用户能用 App 的任何页面。全屏通话页随时可收起；收起后只占顶部一条 44 pt 的通话条，内容下移而不是被覆盖。
2. **像电话**：接通音、触感、计时、大球体、大挂断键；来电中断时暂停、回来时恢复。
3. **一个入口、一处状态**：通话状态由一个 Riverpod 控制器持有，页面和通话条只是它的两种视图；杀掉通话页不会杀掉通话。
4. **先前台，再后台**：P2 保证前台和锁屏可用；Android 后台保活（前台服务）放 P3。

## 2. 交互模型

- **入口**：助理页顶栏 actions（现有 `mobile/lib/app/router.dart` 的 `_AssistantRoute.actions`，与 `AssistantTasksButton` 并排、放在它前面）：图标 `Icons.phone_outlined`，`tooltip` 用 `voice:button.startLabel`。通话中图标变 `Icons.phone_in_talk`（`t.s700`），点它 = 打开通话页。`voice_enabled=false` 时不显示。
- **通话页** `VoiceCallPage`：新路由 `Paths.voice = '/app/voice'`（`GoRoute`，`parentNavigatorKey` 为根导航器，全屏 push）。入口点下去先 push 通话页，页面内发起连接；系统返回/下滑手势/左上角“收起” = pop 页面，通话继续；挂断 = 结束通话并 pop。
- **通话条** `VoiceCallBanner`：在 `mobile/lib/app/app.dart` 的 `MaterialApp.router(builder: (context, child) => VoiceCallHost(child: child))` 里统一插入：`Column([VoiceCallBanner(), Expanded(child)])`。只在通话存在且通话页不在最上层时显示；点它 push 通话页；右侧有独立挂断键。所有路由（聊天、知识库、设置、账单）都自然下移 44 pt，不遮任何控件。
- **再次点入口**：通话中 = 打开通话页，不会第二通。
- **通知**（P3）：通话中切到后台时，iOS 以音频后台模式继续，Android 显示“通话中”常驻通知（前台服务）。

## 3. 画面

通话页（SafeArea 内，背景 `t.bg`）：

```
┌────────────────────────────────────┐
│  ⌄                      02:14      │   ← 左上“收起”(chevron_down 28pt)；右上计时 (FontSizes.md, t.n600)
│                                    │
│            个人助理                 │   ← FontSizes.xl2, t.ink
│            我在听                   │   ← 状态行 FontSizes.base, t.n600；working 时第二行小字“在办，稍等…”
│                                    │
│              ╭────╮                 │
│              │ ◉  │  160pt 球体     │   ← 随电平缩放 1.0–1.3，thinking/working 慢呼吸
│              ╰────╯                 │
│                                    │
│  你：帮我看看贪吃蛇项目进展            │   ← 字幕区固定高度 ~96pt，最近两句，t.n600 / t.ink
│  助理：好的，你稍等一下，我去看看。     │
│                                    │
│   (🔇)         (📵)         (🔊)    │   ← 静音 56pt 圆(t.n200) · 挂断 72pt 圆(t.danger, 白图标) · 扬声器 56pt 圆
│   静音          挂断        扬声器    │   ← 标签 FontSizes.sm
│         本次费用 ¥0.0035             │   ← FontSizes.xs, t.n500；点开 bottom sheet 看明细
└────────────────────────────────────┘
```

通话条（44 pt，`t.s700` 背景，白字，顶部贴 `MediaQuery.padding.top` 之下）：

```
┌────────────────────────────────────┐
│ ● 通话中 · 02:14 · 我在听      [📵] │   ← 左侧脉动点；文案 voice:banner.inCall；右侧挂断 32pt
└────────────────────────────────────┘
```

结束面板：通话页内原地切换为结束态（球体变灰、状态行 `voice:ended.<reason>`、时长与费用、`pendingHint`），按钮 `voice:actions.redial`（允许的 reason 同 PC）与 `voice:actions.close`（pop）。若用户在别的页面时结束（通话条可见），通话条变为灰色显示 `voice:ended.title` 3 秒后消失，并 toast `voice:ended.<reason>`（现有 `shared/widgets/toast.dart`）。

状态与控件对照：

| 状态 | 状态行 | 控件 |
| --- | --- | --- |
| `requesting_mic` | `voice:state.requestingMic` | 仅“取消” |
| `connecting` | `voice:state.connecting` | 仅“取消” |
| `connected` | `voice:state.<phase>` (+ working 小字) | 静音、挂断、扬声器 |
| `paused`（来电/后台中断） | `voice:state.paused` + `voice:interruption.*` | 挂断 |
| `ending` | `voice:state.ending` | 禁用 |
| `ended` | 结束面板 | 重拨/关闭 |

## 4. 系统行为

| 事项 | iOS | Android |
| --- | --- | --- |
| 麦克风权限 | 首次点入口先弹自家说明页 `VoicePrepermissionPage`（仿现有 `notify_prepermission_page.dart`：`voice:permission.*`），用户点“允许”再触发系统弹窗；拒绝过 → 结束态 `voice:errors.micDenied` + `voice:permission.openSettings`（`openAppSettings`） | 同左 |
| 平台声明 | `Info.plist`：`NSMicrophoneUsageDescription`（文案同 `voice:permission.body`）、`UIBackgroundModes: [audio]` | `AndroidManifest.xml`：`RECORD_AUDIO`、`MODIFY_AUDIO_SETTINGS`；P3 加 `FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MICROPHONE` |
| 音频会话 | `audio_session`：`playAndRecord` + `defaultToSpeaker`（无耳机时）+ `allowBluetooth` + `voiceChat` 模式（启用系统回声消除） | `audio_session` 的 `AndroidAudioAttributes(usage: voiceCommunication)`，`AudioManager` 通信模式 |
| 锁屏/切后台 | 音频后台模式下继续通话；通话页恢复时刷新计时 | P2：切后台 60 秒内继续（系统允许时）；超过或系统切断麦克风 → 置 `paused`，通知栏/回到应用时提示 `voice:interruption.background`；P3 前台服务保活 |
| 来电/Siri 等中断 | `audio_session.interruptionEventStream`：开始 → 暂停采集与播放、发零帧保持连接、状态 `paused`；结束 → 恢复；暂停超过 60 秒 → 挂断（`error`） | 同左（`AudioFocus` 丢失） |
| 耳机/蓝牙切换 | `audio_session.devicesChangedEventStream`：插入耳机自动关扬声器，拔出自动开；用户手动切过则尊重手动 | 同左 |
| 扬声器/听筒 | 默认：无耳机时扬声器；切换用 `audio_session` 的 `setActive` + 路由覆盖 | 同左 |
| 网络切换 | WS 断 → P2 内 20 秒重连一次（服务端 P2 支持恢复），失败则 `network` 结束 | 同左 |
| 屏幕常亮 | 通话页在前台时 `wakelock_plus` 保持常亮；收起后释放 | 同左 |
| 触感 | 接通/挂断 `HapticFeedback.lightImpact()`，错误 `mediumImpact()` | 同左 |
| 提示音 | `assets/sounds/call_connecting.wav`、`call_connected.wav`、`call_ended.wav`、`call_error.wav`，用 `flutter_pcm_sound` 之外的独立通道播放（`video_player` 已在依赖里，可播本地短音；或 `audioplayers`）。生成命令：`ffmpeg -f lavfi -i "sine=frequency=440:duration=0.08" ...`，具体四条写在 `mobile/assets/sounds/README.md` | 同左 |

## 5. 音频实现与依赖

`pubspec.yaml` 新增：

```yaml
  record: ^6.0.0          # 麦克风流式 PCM16：startStream(RecordConfig(encoder: pcm16bits, sampleRate: 16000, numChannels: 1))
  flutter_pcm_sound: ^3.0.0   # 24 kHz PCM 播放：feed(PcmArrayInt16)；setFeedCallback 填充；clear() 用于打断
  audio_session: ^0.2.0   # 会话配置、中断、路由变化
  wakelock_plus: ^1.2.0
```

（版本以接入当天 pub.dev 最新稳定版为准；`record` 在 iOS 上 `startStream` 需要 `AudioEncoder.pcm16bits`，Android 需 `RECORD_AUDIO` 已授权。）

- `audio/pcm_capture.dart`：`record.startStream` → 把流重组为精确 3,200 字节/包（100 ms；设备可能给不等长块）→ `onPacket(bytes, level)`；静音时把包置零；计算 RMS 电平供球体。
- `audio/pcm_player.dart`：`FlutterPcmSound.setup(sampleRate: 24000, channelCount: 1)`；收到服务端二进制即 `feed`；`clear()` 响应 `playback.clear`；用 `remainingFrames` 判断“播完”触发 `onIdle`；播放电平用最近一包的 RMS 估算。
- `audio/call_audio_session.dart`：封装 `audio_session` 配置、中断/路由事件 → 控制器事件。
- 发送背压：WS `sink` 无法读 bufferedAmount，用“未确认的 heartbeat 超过 30 秒”作为断线判定（总规格 5.6）。

## 6. 文件与 Riverpod 结构

```
mobile/lib/features/voice/
  state/voice_call_state.dart        # 不可变状态类（字段与 PC 的 CallState 一致）+ EndReason/Phase 枚举
  state/voice_call_reducer.dart      # (state, ServerEvent) => state 纯函数（与 PC reducer 行为一致，用同一组用例）
  state/voice_call_controller.dart   # Notifier<VoiceCallState>：start()/hangUp()/toggleMute()/toggleSpeaker()；持有 VoiceCallService
  api/voice_socket.dart              # 票据（POST /api/auth/ticket {audience: voice}，复用 dio）→ WebSocketChannel；二进制/JSON 分流
  audio/pcm_capture.dart  audio/pcm_player.dart  audio/call_audio_session.dart  audio/tones.dart
  voice_call_page.dart               # 全屏页
  voice_call_prepermission_page.dart
  widgets/voice_call_banner.dart     # 顶部通话条
  widgets/voice_call_host.dart       # MaterialApp.builder 包裹层：Column(banner, child)
  widgets/voice_orb.dart
  widgets/voice_captions.dart
  widgets/voice_call_controls.dart   # 三个圆键 + 标签
  widgets/voice_cost_sheet.dart
  widgets/voice_call_button.dart     # 顶栏入口
mobile/assets/sounds/*.wav + README.md
mobile/assets/locales/{zh-CN,en-US}/voice.json      # 与网页字节一致
```

改动的现有文件：`app/app.dart`（builder 包 `VoiceCallHost`）、`app/router.dart`（`Paths.voice` 路由、`_AssistantRoute.actions` 加按钮）、`shared/router/paths.dart`（`voice`）、`shared/i18n/i18n.dart`（`_namespaces` 加 `'voice'`）、`shared/models/app_config.dart`（`voiceEnabled`）、`pubspec.yaml`、`ios/Runner/Info.plist`、`android/app/src/main/AndroidManifest.xml`。

控制器生命周期：`voiceCallControllerProvider` 用 `keepAlive`（通话不随页面销毁）；登出或切换工作区时 `hangUp()`（监听现有 `authProvider` 与 `activeWorkspace`）。

每个 Dart 文件 ≤ 800 行（现有门禁 `mobile/scripts/check_file_size.sh`）。

## 7. i18n

与网页同一份 `voice.json`，复制到 `mobile/assets/locales/<lng>/voice.json`，并在 `i18n.dart` 的 `_namespaces` 登记；门禁 `mobile/scripts/check_locales.sh` 必须通过。用法 `i18n.t('voice:state.listening')`。

## 8. 测试（flutter test）

| 文件 | 用例 |
| --- | --- |
| `test/features/voice/voice_call_reducer_test.dart` | 与 PC `reducer.test.ts` 同一组用例 |
| `test/features/voice/voice_call_controller_test.dart` | 假 socket（仿现有 `ws_client` 测试的 `WsChannelFactory`）+ 假音频：握手顺序、ready 前不发音频、playback.clear 调 player.clear、stop→ended、4009/4029 映射、中断→paused→恢复、暂停超时挂断 |
| `test/features/voice/voice_call_banner_test.dart` | 通话中显示、计时文案、点按打开通话页、挂断键、通话页在最上层时隐藏、结束后 3 秒消失 |
| `test/features/voice/voice_call_page_test.dart` | 各状态的状态行与控件可用性；收起 pop 不挂断；挂断 pop 且控制器 ended |
| `test/features/voice/voice_call_host_test.dart` | 有通话时内容下移 44 pt；无通话时不占空间 |
| 门禁 | `check_locales.sh`、`check_file_size.sh`、`flutter analyze` |
| 真机/模拟器清单（手工） | 总规格 §10 的 14 条 + 来电中断、锁屏 5 分钟、耳机插拔、Wi-Fi↔蜂窝切换、低电量模式 |

## 9. 任务顺序

1. `voice.json` 同步、`_namespaces`、门禁通过。
2. `voice_call_state/reducer` + 测试。
3. 依赖与平台声明；`pcm_capture/pcm_player/call_audio_session` 在真机上单独验证能录能放（这是风险最高的一步，先做）。
4. `voice_socket` + 控制器 + 测试。
5. 通话页、球体、字幕、控件、费用 sheet。
6. `VoiceCallHost` + 通话条 + 入口按钮 + 路由。
7. 预权限页、提示音、触感、常亮。
8. 模拟器跑 §10，iPhone 真机跑系统行为清单；记录到方案文档 §7。
