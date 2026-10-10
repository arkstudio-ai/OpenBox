import 'dart:async';
import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/voice/api/voice_socket.dart';
import 'package:bossip_mobile/features/voice/audio/call_audio.dart';
import 'package:bossip_mobile/features/voice/audio/mic_permission.dart';
import 'package:bossip_mobile/features/voice/audio/tones.dart';
import 'package:bossip_mobile/features/voice/platform/system_voice_call.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// A voice socket with no network: the test speaks for the server.
class FakeVoiceChannel implements WebSocketChannel {
  FakeVoiceChannel(this.uri);

  final Uri uri;
  final _incoming = StreamController<dynamic>();
  final sent = <Object?>[];
  int? _closeCode;
  bool sinkClosed = false;

  void event(Map<String, Object?> json) => _incoming.add(jsonEncode(json));

  void sendReady({int maxSeconds = 1800}) => event({
    'type': 'ready',
    'call_id': 'call-1',
    'model': 'qwen3.8-omni-flash-realtime',
    'input_sample_rate': 16000,
    'output_sample_rate': 24000,
    'max_seconds': maxSeconds,
    'price_date': '2026-10-07',
  });

  void audio(List<int> bytes) => _incoming.add(Uint8List.fromList(bytes));

  /// The server closes with [code]. Not awaited: the done event is delivered
  /// on the next pump.
  void serverClose(int code) {
    _closeCode = code;
    unawaited(_incoming.close());
  }

  List<Uint8List> get audioSent => sent.whereType<Uint8List>().toList();

  List<Map<String, dynamic>> get jsonSent => [
    for (final frame in sent.whereType<String>())
      jsonDecode(frame) as Map<String, dynamic>,
  ];

  @override
  Future<void> get ready => Future.value();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _FakeSink(this);

  @override
  int? get closeCode => _closeCode;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeSink implements WebSocketSink {
  _FakeSink(this._channel);

  final FakeVoiceChannel _channel;

  @override
  void add(dynamic data) => _channel.sent.add(data);

  @override
  Future<void> close([int? closeCode, String? closeReason]) async {
    _channel.sinkClosed = true;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

/// Real [VoiceConnector.open] over fake channels, with a canned ticket.
class FakeConnector extends VoiceConnector {
  FakeConnector() : super(Dio(), openChannel: (_) => throw StateError('x'));

  final channels = <FakeVoiceChannel>[];
  final scopes = <AssistantScope>[];

  /// Throw instead of opening (ticket refused, server down).
  Object? failWith;

  FakeVoiceChannel get last => channels.last;

  @override
  Future<String> fetchTicket(AssistantScope scope) async {
    scopes.add(scope);
    final failure = failWith;
    if (failure != null) throw failure;
    return 'ticket-${scopes.length}';
  }

  @override
  Future<VoiceSocket> open(AssistantScope scope) async {
    final ticket = await fetchTicket(scope);
    final channel = FakeVoiceChannel(VoiceConnector.socketUri(ticket));
    channels.add(channel);
    await channel.ready;
    return VoiceSocket(channel);
  }
}

class FakeCallAudio implements CallAudio {
  final _events = StreamController<CallAudioEvent>.broadcast();
  final level = ValueNotifier<double>(0);
  void Function(Uint8List packet)? _onPacket;
  final tones = <CallTone>[];
  final played = <Uint8List>[];
  final speaker = <bool>[];
  int clears = 0;
  bool opened = false;
  bool paused = false;
  bool closed = false;
  CallTone? closeTone;
  bool external = false;
  bool resumes = true;
  Object? openError;

  void emit(CallAudioEvent event) => _events.add(event);

  /// One 100 ms microphone packet filled with [sample].
  Uint8List packet([int sample = 1200]) {
    final data = ByteData(3200);
    for (var i = 0; i < 1600; i++) {
      data.setInt16(i * 2, sample, Endian.little);
    }
    final bytes = data.buffer.asUint8List();
    _onPacket!(bytes);
    return bytes;
  }

  @override
  Stream<CallAudioEvent> get events => _events.stream;

  @override
  ValueListenable<double> get outputLevel => level;

  @override
  Future<void> open({required void Function(Uint8List packet) onPacket}) async {
    final failure = openError;
    if (failure != null) throw failure;
    _onPacket = onPacket;
    opened = true;
  }

  @override
  void playTone(CallTone tone) => tones.add(tone);

  /// The ringback is on now (it rings until the call is answered).
  bool ringing = false;

  @override
  void startRinging() => ringing = true;

  @override
  void stopRinging() => ringing = false;

  @override
  void play(Uint8List pcm) => played.add(pcm);

  @override
  void clearPlayback() => clears++;

  @override
  Future<void> pause() async => paused = true;

  @override
  Future<bool> resume() async {
    if (resumes) paused = false;
    return resumes;
  }

  @override
  Future<bool> externalOutput() async => external;

  @override
  Future<void> setSpeaker(bool on) async => speaker.add(on);

  @override
  Future<void> close({CallTone? tone}) async {
    closed = true;
    closeTone = tone;
  }
}

class FakeMicPermission implements MicPermission {
  FakeMicPermission([this.access = MicAccess.granted]);

  MicAccess access;
  bool grant = true;
  int requests = 0;
  int settings = 0;

  @override
  Future<MicAccess> status() async => access;

  @override
  Future<bool> request() async {
    requests++;
    if (grant) access = MicAccess.granted;
    return grant;
  }

  @override
  Future<void> openSettings() async => settings++;
}

/// A clock the test moves by hand, next to `tester.pump`.
class FakeClock {
  DateTime now = DateTime(2026, 10, 7, 9);

  DateTime call() => now;

  void advance(Duration by) => now = now.add(by);
}

class FakeSystemVoiceCall extends NoopSystemVoiceCall {
  final events = StreamController<SystemVoiceAction>.broadcast(sync: true);
  int starts = 0;
  int ends = 0;
  bool needsOverlay = false;
  int overlayRequests = 0;
  Object? startError;
  Completer<void>? startGate;
  final connections = <DateTime>[];
  final muteChanges = <bool>[];

  void dispose() => unawaited(events.close());

  @override
  Stream<SystemVoiceAction> get actions => events.stream;
  @override
  Future<void> start() async {
    starts++;
    if (startGate != null) await startGate!.future;
    if (startError != null) throw startError!;
  }

  @override
  Future<void> end() async => ends++;
  @override
  Future<void> connected(DateTime at) async => connections.add(at);
  @override
  Future<void> setMuted(bool muted) async => muteChanges.add(muted);
  @override
  Future<bool> needsOverlayPermission() async => needsOverlay;
  @override
  Future<bool> requestOverlayPermission() async {
    overlayRequests++;
    return false;
  }
}

const testScope = (userId: 'user-1', workspaceId: 'ws-1');

/// The call's user and workspace; tests change it to sign out or switch.
final testScopeProvider = StateProvider<AssistantScope?>((ref) => testScope);

/// Everything a controller test needs, wired into one container.
class VoiceRig {
  VoiceRig({MicAccess access = MicAccess.granted})
    : permission = FakeMicPermission(access);

  final connector = FakeConnector();
  final audios = <FakeCallAudio>[];
  final FakeMicPermission permission;
  final clock = FakeClock();
  final haptics = <bool>[];
  final screenOn = <bool>[];
  final systemCall = FakeSystemVoiceCall();
  int ensured = 0;

  /// The next [CallAudio] the controller creates; replaced after each call.
  FakeCallAudio nextAudio = FakeCallAudio();

  FakeCallAudio get audio => audios.last;

  List<Override> get overrides => [
    systemVoiceCallProvider.overrideWith((ref) {
      ref.onDispose(systemCall.dispose);
      return systemCall;
    }),
    assistantScopeProvider.overrideWith((ref) => ref.watch(testScopeProvider)),
    voiceCallDepsProvider.overrideWithValue(
      VoiceCallDeps(
        connector: connector,
        audio: () {
          final audio = nextAudio;
          audios.add(audio);
          nextAudio = FakeCallAudio();
          return audio;
        },
        permission: permission,
        ensureAssistant: (_) async => ensured++,
        now: clock.call,
        haptic: ({required strong}) => haptics.add(strong),
        keepScreenOn: (on) async => screenOn.add(on),
        systemCall: systemCall,
      ),
    ),
  ];
}

/// The real locale bundle in zh-CN and the default light tokens.
Future<Override> zhI18n(WidgetTester tester) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  return i18nProvider.overrideWith(() => I18nController(bundle, prefs));
}

ThemeData testTheme() => ThemeData(
  extensions: [
    BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
  ],
);

/// A connected call as the page and the bar would see it.
VoiceCallState connectedCall({
  required DateTime at,
  VoicePhase phase = VoicePhase.listening,
  bool expanded = false,
  VoiceCost? cost,
}) => VoiceCallState(
  status: VoiceCallStatus.connected,
  phase: phase,
  callId: 'call-1',
  maxSeconds: 1800,
  connectedAt: at,
  cost: cost,
  expanded: expanded,
);

/// A controller frozen in [initial] that records what it was asked to do.
class StubVoiceController extends VoiceCallController {
  StubVoiceController(this.initial, this.clock);

  final VoiceCallState initial;
  final FakeClock clock;
  int starts = 0;
  int hangUps = 0;
  int mutes = 0;
  int speakers = 0;

  @override
  VoiceCallState build() => initial;

  void set(VoiceCallState next) => state = next;

  @override
  Future<void> start() async => starts++;

  @override
  Future<void> hangUp() async => hangUps++;

  @override
  void toggleMute() => mutes++;

  @override
  Future<void> toggleSpeaker() async => speakers++;

  @override
  DateTime now() => clock.now;

  @override
  Future<void> keepScreenOn(bool on) async {}
}
