# 语音通话移动端（Flutter）：通话页与顶部通话条

状态：**实施文档**。日期：2026-10-07。契约见 [总规格](VOICE_CALL_SPEC.md)；后端见 [后端文档](VOICE_CALL_BACKEND.md)；PC 端见 [悬浮窗](VOICE_CALL_WEB.md)。本文讲手机端怎么做：交互模型、画面、系统行为、音频、文件、测试、顺序。所有“现有”引用均已核对源码。

## 1. 原则

1. **不阻挡**：通话进行时用户能用 App 的任何页面。全屏通话页随时可收起；收起后只占顶部一条 44 pt 的通话条，内容下移而不是被覆盖。
2. **像电话**：接通音、触感、计时、大球体、大挂断键；来电中断时暂停、回来时恢复。
3. **一个入口、一处状态**：通话状态由一个 Riverpod 控制器持有，页面和通话条只是它的两种视图；杀掉通话页不会杀掉通话。
4. **通话独立于页面**：退到后台继续通话；Android 使用麦克风前台服务，iOS 使用 CallKit + 音频后台模式。
5. **只听不看字**：通话页不显示对话文字，用户不用盯着屏幕；说过的话（交给助理的轮次）在对话页里。

## 2. 交互模型

- **入口**：助理页顶栏 actions（现有 `mobile/lib/app/router.dart` 的 `_AssistantRoute.actions`，与 `AssistantTasksButton` 并排、放在它前面）：图标 `Icons.phone_outlined`，`tooltip` 用 `voice:button.startLabel`。通话中图标变 `Icons.phone_in_talk`（`t.s700`），点它 = 打开通话页。`voice_enabled=false` 时不显示。
- **通话页** `VoiceCallPage`：新路由 `Paths.voice = '/app/voice'`（`GoRoute`，`parentNavigatorKey` 为根导航器，全屏 push）。入口点下去先 push 通话页，页面内发起连接；系统返回/下滑手势/左上角“收起” = pop 页面，通话继续；挂断 = 结束通话并 pop。
- **通话条** `VoiceCallBanner`：在 `mobile/lib/app/app.dart` 的 `MaterialApp.router(builder: (context, child) => VoiceCallHost(child: child))` 里统一插入：`Column([VoiceCallBanner(), Expanded(child)])`。只在通话存在且通话页不在最上层时显示；点它 push 通话页；右侧有独立挂断键。所有路由（聊天、知识库、设置、账单）都自然下移 44 pt，不遮任何控件。
- **再次点入口**：通话中 = 打开通话页，不会第二通。
- **后台返回入口**：Android 授权后显示可拖动电话悬浮按钮，并保持常驻通话通知；iOS 使用 CallKit 系统通话标识和返回入口。

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
│  直接说话就好，随时可以打断。           │   ← 接通后 10 秒的提示 voice:hint.start（t.n500），之后留白
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
| 平台声明 | `Info.plist`：`NSMicrophoneUsageDescription`（文案同 `voice:permission.body`）、`UIBackgroundModes: [audio, voip]` | `AndroidManifest.xml`：`RECORD_AUDIO`、`MODIFY_AUDIO_SETTINGS`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MICROPHONE`、`FOREGROUND_SERVICE_MEDIA_PLAYBACK`、`SYSTEM_ALERT_WINDOW`、`WAKE_LOCK` |
| 音频会话 | `audio_session`：`playAndRecord` + `defaultToSpeaker`（无耳机时）+ `allowBluetooth` + `voiceChat` 模式（启用系统回声消除） | `audio_session` 的 `AndroidAudioAttributes(usage: voiceCommunication)`，`AudioManager` 通信模式 |
| 锁屏/切后台 | CallKit 激活音频后继续通话，系统通话入口返回同一通话 | 在前台启动麦克风/播放前台服务，后台持续采集；授权悬浮窗时显示电话图标；返回或隐藏悬浮窗不挂断 |
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
5. 通话页、球体、控件、费用 sheet。
6. `VoiceCallHost` + 通话条 + 入口按钮 + 路由。
7. 预权限页、提示音、触感、常亮。
8. 模拟器跑 §10，iPhone 真机跑系统行为清单；记录到方案文档 §7。

## 10. 实现与本文的差异（2026-10-07 实施记录）

- **依赖**：`record` 7.1.1、`audio_session` 0.2.4、`wakelock_plus` 1.8.0（1.8.1 与 `file_picker` 12 的 `dbus` 冲突）、`permission_handler` 12.0.3（Android 区分“未问过/永久拒绝”与打开设置）、`flutter_pcm_sound` 3.3.3（vendored，原包 Android compileSdk 33 不被 AGP 9 接受）、`record_ios` 2.1.1（vendored，见下）。
- **iOS 麦克风权限**：`audio_session` 的权限接口在未定义 `AUDIO_SESSION_MICROPHONE` 时被编译掉（Swift Package 从构建环境读取），会永远判为拒绝；改为经 `record`（AVCaptureDevice）询问，并在本地记住是否弹过系统对话框。被拒绝时仍会再请求一次（系统直接返回，不弹框），以便权限被重置后能重新询问。
- **无输入设备不闪退**：`record_ios` 在输入格式为 0 Hz 时安装 tap 会抛出 Swift 捕获不了的 Objective-C 异常（模拟器在无麦克风的 Mac 上必现，真机路由切换中也可能出现）。vendored 版加入格式检查，改为普通启动错误；开始采集前也会检查输入设备与当前路由。
- **提示音**混在通话自己的音频输出里（独立一层），不用 `video_player`/`audioplayers`，它们会改 iOS 音频会话或抢占 Android 音频焦点；`playback.clear` 不影响提示音。
- **停止播放**：`flutter_pcm_sound` 没有 `clear()`，设备侧最多排队约 170 ms，其余在 Dart 侧缓冲，清空即时生效。
- **回声消除启动失败**（模拟器常见）时去掉语音处理重试一次，而不是直接按“麦克风被占用”结束。
- **扬声器**：默认路由为听筒，无耳机时用覆盖切到扬声器；耳机插拔自动切换，用户手动选过后不再自动切。
- **断网**：后端尚无续接，断线即按 `network` 结束。
- **挂断**：页面立即关闭，摘要以 toast + 3 秒灰色通话条显示；在页面上结束（非挂断）时显示页内结束面板，麦克风失败带“重试”。拨号中取消直接关闭，无摘要。
- **后台通话（2026-10-09）**：移除后台一分钟暂停计时器。Android 在启动采集前启动 `VoiceCallService`（microphone + mediaPlayback），通过 `TYPE_APPLICATION_OVERLAY` 显示 56dp 电话返回按钮；拒绝悬浮窗权限仍继续通话，常驻通知提供返回及挂断。服务不自动重启，结束/失败/取消时移除悬浮窗、通知和 CPU 唤醒锁。iOS 由 `VoiceCallBridge` 等待 CallKit `didActivate` 后才启动音频，提供系统通话入口和静音/挂断回调。`audio_session` 与 PCM 输出不再自行激活 CallKit 管理的会话。iOS 没有跨应用悬浮窗授权设置，音频通话使用系统通话标识；没有使用视频 PiP 伪装音频通话。
- **调试用文件麦克风**：`--dart-define=VOICE_FAKE_MIC=true` 构建时，通话从 App Documents 下的 `voice-fake-mic.pcm`（16 kHz 单声道 PCM16）读取“麦克风”，播一遍后为静音；发布构建不定义该开关。用于在无麦克风的 Mac 上用模拟器跑完整通话：`xcrun simctl get_app_container booted com.bossip.bipmobile data` 找到容器，把文件放进 `Documents/`。
- **真机待验**：iOS 外放时采集与播放在不同音频引擎，回声消除可能无效导致自我打断（若出现，改为单引擎的小型原生模块，音频接口已隔离）；Android 播放走媒体流，听筒/扬声器与回声消除可能不随通话模式；`flutter_pcm_sound` 暂不支持 Swift Package Manager（Flutter 目前仅警告）。

## 11. 设置 → 语音通话（2026-10-07）

- `SettingsScreen` 新增 `voice` 标签，只在 `AppConfig.voiceEnabled` 时出现；`widgets/voice_section.dart` 与网页同样分组、标记、点选即存（`SettingsApi.setAssistantVoice`），试听用 `video_player` 播放 `/api/assistant/voice/samples/<id>`（与聊天里的音频预览同一播放器）。
- 文案在 `settings` 命名空间（`voice.*`、`nav.voice`、`hint.voice`），与网页逐字节一致（`scripts/check_locales.sh` 通过）。
- 测试：`test/features/settings/voice_section_test.dart`。

## 12. 原生会话链接（2026-10-09）

个人助手 Markdown 中的 `/app/s/:id` 和第一方绝对链接进入原生 `GoRouter`，保留消息查询与锚点，并可返回个人助手。外部站点交给浏览器，知识库自定义链接处理保留。只将当前 API/Web origin 和 `https://ai.bossipai.com.cn` 上的合法会话链接转换为应用路由。

平台参考：[Apple CallKit](https://developer.apple.com/documentation/callkit/making-and-receiving-voip-calls)、[Android microphone/mediaPlayback 前台服务](https://developer.android.com/develop/background-work/services/fgs/service-types)。
