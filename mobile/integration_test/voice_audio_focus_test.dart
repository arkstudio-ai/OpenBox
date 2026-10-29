import 'dart:async';
import 'dart:io';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/voice/audio/call_audio.dart';
import 'package:bossip_mobile/features/voice/audio/mic_permission.dart';
import 'package:bossip_mobile/features/voice/platform/system_voice_call.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_controls.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_copy.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import '../test/features/voice/voice_fakes.dart';

/// Opt-in Android native audio-focus acceptance. The websocket is isolated;
/// microphone, audio_session, PCM output and foreground service are real.
/// An external QA app must request Android audio focus when FOCUS_QA_* is
/// printed: CAN_DUCK (3 s), TRANSIENT (70 s), then GAIN (3 s). This deliberately
/// tests system events from another process, not injected Dart notifications.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets(
    'native focus duck, long interruption and manual recovery',
    (tester) async {
      expect(Platform.isAndroid, isTrue);
      final rig = VoiceRig();
      final system = DeviceSystemVoiceCall(
        () => {
          'name': '音频恢复验收',
          'connecting': '正在接通',
          'ongoing': '通话中',
          'returnLabel': '返回通话',
          'endLabel': '挂断',
        },
      );
      final i18n = await zhI18n(tester);
      final container = ProviderContainer(
        overrides: [
          assistantScopeProvider.overrideWith((ref) => testScope),
          i18n,
          voiceCallDepsProvider.overrideWithValue(
            VoiceCallDeps(
              connector: rig.connector,
              audio: DeviceCallAudio.new,
              permission: const DeviceMicPermission(),
              ensureAssistant: (_) async {},
              systemCall: system,
            ),
          ),
        ],
      );
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: MaterialApp(
            theme: testTheme(),
            home: Scaffold(
              body: Consumer(
                builder: (context, ref, _) {
                  final call = ref.watch(voiceCallControllerProvider);
                  final strings = ref.watch(i18nProvider);
                  return Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Text(VoiceCopy.status(strings, call)),
                      if (VoiceCopy.detail(strings, call)
                          case final String detail)
                        Text(detail),
                      VoiceCallControls(call: call),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
      );
      final controller = container.read(voiceCallControllerProvider.notifier);
      VoiceCallState state() => container.read(voiceCallControllerProvider);
      Future<void> until(bool Function() ready, Duration timeout) async {
        final end = DateTime.now().add(timeout);
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 100));
        }
        expect(
          ready(),
          isTrue,
          reason: 'state=${state().status}, end=${state().end?.reason}',
        );
      }

      Timer? heartbeat;
      try {
        await controller.start();
        expect(state().status, VoiceCallStatus.connecting);
        rig.connector.last.sendReady();
        await tester.pump();
        heartbeat = Timer.periodic(
          const Duration(seconds: 5),
          (_) => rig.connector.last.event({
            'type': 'heartbeat',
            'elapsed_seconds': 1,
          }),
        );
        rig.connector.last.event({
          'type': 'phase',
          'value': 'listening',
          'hearing': false,
        });
        await tester.pump();
        final transitions = <VoiceCallStatus>[];
        final subscription = container.listen(
          voiceCallControllerProvider,
          (_, next) => transitions.add(next.status),
        );
        // ignore: avoid_print
        print('FOCUS_QA_DUCK_READY');
        for (var i = 0; i < 80; i++) {
          await tester.pump(const Duration(milliseconds: 100));
          expect(state().status, VoiceCallStatus.connected);
        }
        expect(transitions, isNot(contains(VoiceCallStatus.paused)));
        // ignore: avoid_print
        print('FOCUS_QA_TRANSIENT_READY');
        await until(
          () => state().status == VoiceCallStatus.paused,
          const Duration(seconds: 12),
        );
        final began = DateTime.now();
        await until(
          () => DateTime.now().difference(began).inSeconds >= 62,
          const Duration(seconds: 65),
        );
        expect(state().status, VoiceCallStatus.paused);
        expect(state().end, isNull);
        expect(find.byKey(const Key('voice-resume')), findsOneWidget);
        await until(
          () => state().status == VoiceCallStatus.connected,
          const Duration(seconds: 15),
        );
        // ignore: avoid_print
        print('FOCUS_QA_PERMANENT_READY');
        await until(
          () => state().status == VoiceCallStatus.paused,
          const Duration(seconds: 12),
        );
        await tester.pump(const Duration(seconds: 5));
        expect(state().status, VoiceCallStatus.paused);
        await tester.tap(find.byKey(const Key('voice-resume')));
        await until(
          () => state().status == VoiceCallStatus.connected,
          const Duration(seconds: 10),
        );
        final diagnostics = rig.connector.last.jsonSent
            .where((e) => e['type'] == 'client.audio')
            .toList();
        expect(diagnostics.where((e) => e['state'] == 'paused').length, 2);
        expect(diagnostics.where((e) => e['state'] == 'resumed').length, 2);
        expect(rig.connector.last.audioSent, isNotEmpty);
        subscription.close();
        // ignore: avoid_print
        print(
          'FOCUS_QA_PASSED duck_continues long_pause_survives regain_resumes manual_recovers',
        );
        await controller.hangUp();
      } finally {
        heartbeat?.cancel();
        await tester.pumpWidget(const SizedBox.shrink());
        container.dispose();
        system.dispose();
      }
    },
    timeout: const Timeout(Duration(minutes: 4)),
  );
}
