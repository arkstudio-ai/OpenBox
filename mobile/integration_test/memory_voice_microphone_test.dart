import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/assistant_scope.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/voice/api/voice_socket.dart';
import 'package:bossip_mobile/features/voice/audio/call_audio.dart';
import 'package:bossip_mobile/features/voice/audio/mic_permission.dart';
import 'package:bossip_mobile/features/voice/audio/pcm_capture.dart';
import 'package:bossip_mobile/features/voice/platform/system_voice_call.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Opt-in operator-driven acceptance. UI remains operated normally; the only
/// dependency replacement is microphone PCM. ASR, routing, persistence,
/// websocket and native playback use the running local application services.
///
/// Documents/memory-live-inputs/commands.json is a list of {id, file} entries;
/// each file is 16 kHz mono PCM16. Append {id, finish: true} after acceptance.
/// Results prove audio transport only: review the actual server transcripts
/// and memory revisions before signing off semantic behavior.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  binding.shouldPropagateDevicePointerEvents = true;
  testWidgets(
    'live microphone and cross-client memory acceptance',
    (tester) async {
      expect(Platform.isIOS, isTrue);
      expect(Env.apiBase, 'http://127.0.0.1:8082');
      expect(const bool.fromEnvironment('VOICE_FAKE_MIC'), isTrue);
      expect(const bool.fromEnvironment('VOICE_SIMULATOR_AUDIO_ONLY'), isTrue);
      await SystemChrome.setPreferredOrientations([
        DeviceOrientation.portraitUp,
      ]);
      final prefs = await SharedPreferences.getInstance();
      final support = await getApplicationSupportDirectory();
      final bundle = await I18nBundle.load();
      final mic = _QueuedMicrophone();
      final container = ProviderContainer(
        overrides: [
          assistantScopeOverride,
          prefsProvider.overrideWithValue(prefs),
          cookieJarProvider.overrideWithValue(
            PersistCookieJar(storage: FileStorage('${support.path}/cookies')),
          ),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          voiceCallDepsProvider.overrideWith((ref) {
            final dio = ref.watch(apiDioProvider);
            return VoiceCallDeps(
              connector: VoiceConnector(dio),
              audio: () => DeviceCallAudio(capture: mic),
              permission: const DeviceMicPermission(),
              ensureAssistant: (scope) => AssistantApi(dio, scope).ensure(),
              systemCall: ref.watch(systemVoiceCallProvider),
            );
          }),
        ],
      );
      await container.read(authProvider.notifier).bootstrap();
      expect(container.read(authProvider).user, isNotNull);
      const testWorkspace = String.fromEnvironment('MEMORY_TEST_WORKSPACE_ID');
      if (testWorkspace.isNotEmpty) {
        final available = await container.read(activeWorkspaceProvider.future);
        expect(available.items.any((item) => item.id == testWorkspace), isTrue);
        await container
            .read(activeWorkspaceProvider.notifier)
            .select(testWorkspace);
      }
      runApp(
        UncontrolledProviderScope(
          container: container,
          child: const BossipApp(),
        ),
      );
      await tester.pump();
      final docs = await getApplicationDocumentsDirectory();
      final root = Directory('${docs.path}/memory-live-inputs');
      await root.create(recursive: true);
      final commands = File('${root.path}/commands.json');
      final results = <Map<String, Object?>>[];
      final evidence = File('${root.path}/results.json');
      final seen = <String>{};
      // A reinstall with --no-uninstall preserves Documents. Never replay
      // commands, including the finish marker, from an earlier test run.
      if (commands.existsSync()) {
        for (final old in jsonDecode(commands.readAsStringSync()) as List) {
          seen.add((old as Map<String, dynamic>)['id'] as String);
        }
      }
      final controller = container.read(voiceCallControllerProvider.notifier);
      VoiceCallState state() => container.read(voiceCallControllerProvider);
      Future<void> save() => evidence.writeAsString(
        jsonEncode({
          'audio_input': 'queued_pcm_16khz_mono_realtime',
          'only_dependency_replacement': 'microphone_capture',
          'callkit_exercised': false,
          'results': results,
        }),
      );
      Future<void> tick() => tester.pump(const Duration(milliseconds: 100));
      Future<void> startCall() async {
        container.read(routerProvider).go(Paths.assistant);
        final by = DateTime.now().add(const Duration(seconds: 60));
        final button = find.byKey(const Key('voice-call-button')).hitTestable();
        while (button.evaluate().isEmpty && DateTime.now().isBefore(by)) {
          final activation = find.byKey(const Key('desktop-activation-dialog'));
          if (activation.evaluate().isNotEmpty) {
            final close = find.descendant(
              of: activation,
              matching: find.byTooltip(
                container.read(i18nProvider).t('workbench:activation.close'),
              ),
            );
            if (close.evaluate().isNotEmpty) await tester.tap(close);
          }
          await tick();
        }
        expect(button, findsOneWidget);
        await tester.tap(button);
        await tick();
        final allow = find.byKey(const Key('voice-permission-allow'));
        if (allow.evaluate().isNotEmpty) await tester.tap(allow);
        while (state().status != VoiceCallStatus.connected &&
            DateTime.now().isBefore(by)) {
          await tick();
        }
        expect(state().status, VoiceCallStatus.connected);
      }

      await startCall();
      await save();
      debugPrint('MEMORY_MIC_READY ${root.path}');
      final deadline = DateTime.now().add(const Duration(minutes: 40));
      var finished = false;
      try {
        while (!finished && DateTime.now().isBefore(deadline)) {
          await tick();
          if (!commands.existsSync()) continue;
          final pending = (jsonDecode(commands.readAsStringSync()) as List)
              .cast<Map<String, dynamic>>();
          for (final command in pending) {
            final id = command['id'] as String;
            if (!seen.add(id)) continue;
            if (command['finish'] == true) {
              finished = true;
              break;
            }
            if (command['new_call'] == true) {
              await controller.hangUp();
              final by = DateTime.now().add(const Duration(seconds: 15));
              while (state().status != VoiceCallStatus.ended &&
                  DateTime.now().isBefore(by)) {
                await tick();
              }
              controller.dismissEnded();
              await tick();
              await startCall();
              debugPrint('MEMORY_MIC_NEW_CALL ${state().callId}');
              continue;
            }
            final filename = command['file'] as String;
            expect(RegExp(r'^[a-z0-9_-]+\.pcm$').hasMatch(filename), isTrue);
            expect(state().status, VoiceCallStatus.connected);
            // Let any greeting or prior reply finish; never cut it off merely
            // to fit a fixed fixture timeline.
            final readyBy = DateTime.now().add(const Duration(seconds: 90));
            while (state().phase != VoicePhase.listening &&
                DateTime.now().isBefore(readyBy)) {
              await tick();
            }
            expect(state().phase, VoicePhase.listening);
            final record = <String, Object?>{
              'id': id,
              'call_id': state().callId,
              'started_at': DateTime.now().toIso8601String(),
              'input_file': filename,
            };
            results.add(record);
            await save();
            mic.enqueue(await File('${root.path}/$filename').readAsBytes());
            var micPeak = 0.0;
            var outputPeak = 0.0;
            var responded = false;
            DateTime? listeningSince;
            final replyBy = DateTime.now().add(const Duration(seconds: 110));
            while (DateTime.now().isBefore(replyBy)) {
              await tick();
              micPeak = math.max(micPeak, controller.micLevel.value);
              outputPeak = math.max(outputPeak, controller.outputLevel.value);
              if (state().phase == VoicePhase.speaking) responded = true;
              if (responded &&
                  mic.drained &&
                  state().phase == VoicePhase.listening) {
                listeningSince ??= DateTime.now();
                if (DateTime.now().difference(listeningSince).inMilliseconds >=
                    2000) {
                  break;
                }
              } else {
                listeningSince = null;
              }
              if (state().status != VoiceCallStatus.connected) break;
            }
            record.addAll({
              'ended_at': DateTime.now().toIso8601String(),
              'status': state().status.name,
              'phase': state().phase.name,
              'mic_peak': micPeak,
              'output_peak': outputPeak,
              'responded': responded,
              'drained': mic.drained,
            });
            await save();
            await File(
              '${root.path}/$id.png',
            ).writeAsBytes(await binding.takeScreenshot(id));
            debugPrint('MEMORY_MIC_RESULT ${jsonEncode(record)}');
            expect(state().status, VoiceCallStatus.connected);
            expect(mic.drained && responded, isTrue);
            expect(micPeak, greaterThan(0));
            expect(outputPeak, greaterThan(0));
          }
        }
        expect(
          finished,
          isTrue,
          reason: 'Operator must close the acceptance run',
        );
        expect(results, isNotEmpty);
      } finally {
        await controller.hangUp();
        await mic.stop();
        await save();
      }
    },
    timeout: const Timeout(Duration(minutes: 45)),
  );
}

class _QueuedMicrophone extends PcmCapture {
  Timer? _timer;
  Uint8List _bytes = Uint8List(0);
  int _offset = 0;
  bool _paused = false;
  bool get drained => _offset >= _bytes.length;

  void enqueue(Uint8List bytes) {
    if (!drained) throw StateError('Previous utterance is still playing');
    _bytes = bytes;
    _offset = 0;
  }

  @override
  bool get running => _timer != null && !_paused;
  @override
  Future<bool> hasMicrophone() async => true;
  @override
  Future<void> start({
    required void Function(Uint8List) onPacket,
    required void Function() onLost,
  }) async {
    _timer?.cancel();
    _paused = false;
    lastAudioAt = DateTime.now();
    _timer = Timer.periodic(const Duration(milliseconds: 100), (_) {
      if (_paused) return;
      lastAudioAt = DateTime.now();
      final packet = Uint8List(3200);
      if (!drained) {
        final end = math.min(_offset + packet.length, _bytes.length);
        packet.setRange(0, end - _offset, _bytes, _offset);
        _offset = end;
      }
      onPacket(packet);
    });
  }

  @override
  Future<void> pause() async => _paused = true;
  @override
  Future<void> resume() async {
    _paused = false;
    lastAudioAt = DateTime.now();
  }

  @override
  Future<void> stop() async {
    _timer?.cancel();
    _timer = null;
    _bytes = Uint8List(0);
    _offset = 0;
  }

  @override
  Future<void> dispose() => stop();
}
