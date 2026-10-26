import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/memory/knowledge_screen.dart';
import 'package:bossip_mobile/features/voice/platform/system_voice_call.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';

/// Opt-in simulator acceptance using the real app, native audio output,
/// HTTP, websocket, ASR and voice model. Only microphone input comes
/// from a prerecorded natural utterance through the existing QA capture path.
/// Review server transcripts/view snapshots for the emitted call IDs as well;
/// transport success alone does not certify the semantic response.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const username = String.fromEnvironment('MEMORY_QA_USERNAME');
  const password = String.fromEnvironment('MEMORY_QA_PASSWORD');
  const firstChat = String.fromEnvironment('MEMORY_QA_VOICE_FIRST');
  const secondChat = String.fromEnvironment('MEMORY_QA_VOICE_SECOND');
  const background = bool.fromEnvironment('MEMORY_QA_VOICE_BACKGROUND');
  const audioOnly = bool.fromEnvironment('VOICE_SIMULATOR_AUDIO_ONLY');
  const expectStartFailure = bool.fromEnvironment(
    'MEMORY_QA_EXPECT_START_FAILURE',
  );

  testWidgets(
    'native voice follows the visible chat and clears it on exit',
    (tester) async {
      expect(Platform.isIOS, isTrue);
      expect(Env.apiBase, 'http://127.0.0.1:8082');
      expect(const bool.fromEnvironment('VOICE_FAKE_MIC'), isTrue);
      expect(
        [
          username,
          password,
          firstChat,
          secondChat,
        ].every((value) => value.isNotEmpty),
        isTrue,
      );
      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      final router = container.read(routerProvider);
      final controller = container.read(voiceCallControllerProvider.notifier);
      VoiceCallState call() => container.read(voiceCallControllerProvider);
      final root = Directory(
        '${(await getApplicationSupportDirectory()).path}/memory-voice-qa',
      );
      await root.create(recursive: true);
      final evidence = <String, Object?>{
        'platform': Platform.operatingSystem,
        'api': Env.apiBase,
        'audio_input': 'prerecorded_pcm_real_time',
        'provider_overrides': false,
        'callkit_exercised': !audioOnly,
        'calls': <Map<String, Object?>>[],
      };
      final timeline = <Map<String, Object?>>[];
      evidence['timeline'] = timeline;
      final stateObserver = container.listen(voiceCallControllerProvider, (
        previous,
        next,
      ) {
        final event = {
          'at': DateTime.now().toIso8601String(),
          'status': next.status.name,
          'expanded': next.expanded,
          'reason': next.end?.reason.wire,
          'detail': next.end?.detailKey,
        };
        timeline.add(event);
        debugPrint('MEMORY_VOICE_STATE ${jsonEncode(event)}');
      });
      final scopeObserver = container.listen(assistantScopeProvider, (
        old,
        next,
      ) {
        timeline.add({
          'scope_changed': old != next,
          'scope_present': next != null,
        });
        debugPrint(
          'MEMORY_VOICE_SCOPE changed=${old != next} present=${next != null}',
        );
      });
      final systemObserver = container
          .read(systemVoiceCallProvider)
          .actions
          .listen((action) {
            timeline.add({'system_action': action.name});
            debugPrint('MEMORY_VOICE_SYSTEM ${action.name}');
          });
      addTearDown(() async {
        stateObserver.close();
        scopeObserver.close();
        await systemObserver.cancel();
      });
      Future<void> save(String stage) async {
        await File(
          '${root.path}/evidence.json',
        ).writeAsString(jsonEncode(evidence));
        await File(
          '${root.path}/$stage.png',
        ).writeAsBytes(await binding.takeScreenshot(stage));
        debugPrint('MEMORY_VOICE_EVIDENCE ${root.path}/$stage.png');
      }

      Future<void> until(bool Function() ready, String stage) async {
        final deadline = DateTime.now().add(const Duration(seconds: 90));
        while (!ready() && DateTime.now().isBefore(deadline)) {
          await tester.pump(const Duration(milliseconds: 100));
        }
        if (!ready()) {
          evidence['failure'] = {
            'stage': stage,
            'status': call().status.name,
            'reason': call().end?.reason.wire,
            'detail': call().end?.detailKey,
          };
          await save('failure-$stage');
        }
        expect(ready(), isTrue, reason: stage);
        debugPrint('MEMORY_VOICE_STAGE $stage');
      }

      Future<Map<String, dynamic>> nativeState() async =>
          Map<String, dynamic>.from(
            await DeviceSystemVoiceCall.channel
                    .invokeMapMethod<String, dynamic>('state') ??
                {},
          );

      if (container.read(authProvider).user?.username != username) {
        await container.read(authFlowProvider).login(username, password);
      }
      final documents = (await getApplicationDocumentsDirectory()).path;
      final mic = File('$documents/voice-fake-mic.pcm');
      // Seed the app Documents fixture through simctl before this run. Keeping
      // PCM out of dart-defines avoids the OS argument/environment size limit.
      final input = File('$documents/memory-voice-input.pcm');
      expect(
        input.existsSync(),
        isTrue,
        reason: 'Seed the QA PCM input first',
      );
      final original = mic.existsSync() ? await mic.readAsBytes() : null;
      await mic.writeAsBytes(await input.readAsBytes());
      try {
        for (final scenario in ['current-chat', 'switched-chat', 'left-chat']) {
          router.go(Paths.assistant);
          await until(
            () => find.byType(AssistantScreen).evaluate().isNotEmpty,
            '$scenario-assistant-ready',
          );
          await tester.pump(const Duration(milliseconds: 500));
          final entry = find
              .byKey(const Key('voice-call-button'))
              .hitTestable();
          await until(() => entry.evaluate().isNotEmpty, '$scenario-entry');
          await tester.tap(entry);
          await tester.pump(const Duration(milliseconds: 400));
          final allow = find.byKey(const Key('voice-permission-allow'));
          if (allow.evaluate().isNotEmpty) {
            await tester.tap(allow);
          }
          if (expectStartFailure) {
            expect(audioOnly, false);
            await until(
              () => call().status == VoiceCallStatus.ended,
              'native-start-failure-visible',
            );
            expect(call().end?.detailKey, 'voice:errors.backgroundStart');
            evidence['native_start_failure_visible'] = true;
            await save('native-start-failure-visible');
            return;
          }
          await until(
            () => call().status == VoiceCallStatus.connected,
            '$scenario-connected',
          );
          final id = call().callId!;
          final record = <String, Object?>{
            'scenario': scenario,
            'call_id': id,
            'expected_view': scenario == 'left-chat'
                ? null
                : scenario == 'switched-chat'
                ? secondChat
                : firstChat,
          };
          (evidence['calls']! as List<Map<String, Object?>>).add(record);
          // The shipped mobile entry is on the personal assistant page.
          // Collapse that call and open a normal conversation, as a user does.
          router.pop();
          router.go(Paths.chat(firstChat));
          await until(
            () => find
                .byType(ChatScreen)
                .evaluate()
                .any(
                  (element) =>
                      (element.widget as ChatScreen).sessionId == firstChat,
                ),
            '$scenario-first-chat-visible',
          );
          await tester.pump(const Duration(milliseconds: 500));
          if (scenario == 'switched-chat') {
            router.go(Paths.chat(secondChat));
            await until(
              () => find
                  .byType(ChatScreen)
                  .evaluate()
                  .any(
                    (element) =>
                        (element.widget as ChatScreen).sessionId == secondChat,
                  ),
              'switched-chat-visible',
            );
          } else if (scenario == 'left-chat') {
            router.go(Paths.wiki(view: 'files'));
            await until(
              () => find.byType(KnowledgeScreen).evaluate().isNotEmpty,
              'knowledge-visible',
            );
          }
          // The file starts with silence so these normal navigation events
          // reach the server before the user asks about the visible conversation.
          final began = DateTime.now();
          var micPeak = 0.0;
          var outputPeak = 0.0;
          final phases = <String>{};
          while (DateTime.now().difference(began).inSeconds < 48) {
            await tester.pump(const Duration(milliseconds: 100));
            expect(call().callId, id);
            expect(call().status, VoiceCallStatus.connected);
            phases.add(call().phase.name);
            if (controller.micLevel.value > micPeak) {
              micPeak = controller.micLevel.value;
            }
            if (controller.outputLevel.value > outputPeak) {
              outputPeak = controller.outputLevel.value;
            }
          }
          record.addAll({
            'mic_peak': micPeak,
            'output_peak': outputPeak,
            'phases': phases.toList(),
            'forwarded_turns': call().turns.length,
            'native': await nativeState(),
          });
          expect(micPeak, greaterThan(0));
          expect(outputPeak, greaterThan(0));
          await save(scenario);
          if (background && scenario == 'current-chat') {
            await tester.pump(const Duration(milliseconds: 500));
            debugPrint('MEMORY_VOICE_STAGE ready-for-native-home');
            var backgroundReturned = false;
            await tester.runAsync(() async {
              // Manual native Home/return is driven outside Flutter. Give the
              // operator time without changing any product/network deadline.
              final deadline = DateTime.now().add(const Duration(minutes: 10));
              while (![
                    AppLifecycleState.paused,
                    AppLifecycleState.hidden,
                  ].contains(binding.lifecycleState) &&
                  DateTime.now().isBefore(deadline)) {
                await Future<void>.delayed(const Duration(milliseconds: 200));
              }
              expect([
                AppLifecycleState.paused,
                AppLifecycleState.hidden,
              ], contains(binding.lifecycleState));
              record['background_entered_at'] = DateTime.now().toIso8601String();
              final began = DateTime.now();
              final samples = <Map<String, Object?>>[];
              while (DateTime.now().difference(began).inSeconds < 20) {
                await Future<void>.delayed(const Duration(seconds: 2));
                final native = await nativeState();
                if (!audioOnly) {
                  expect(native['active'], true);
                  expect(native['audioActive'], true);
                }
                expect(call().callId, id);
                expect(call().status, VoiceCallStatus.connected);
                samples.add(native);
              }
              record['background'] = samples;
              debugPrint('MEMORY_VOICE_STAGE ready-for-native-return');
              final returnBy = DateTime.now().add(const Duration(minutes: 10));
              while (binding.lifecycleState != AppLifecycleState.resumed &&
                  DateTime.now().isBefore(returnBy)) {
                await Future<void>.delayed(const Duration(milliseconds: 200));
              }
              expect(binding.lifecycleState, AppLifecycleState.resumed);
              record['foreground_returned_at'] = DateTime.now().toIso8601String();
              backgroundReturned = true;
            });
            // runAsync records asynchronous exceptions for the final result.
            // Stop here as well; do not emit later success-stage screenshots
            // when the external Home/return interaction never happened.
            expect(backgroundReturned, isTrue,
                reason: 'Actual native Home and return must both be observed');
            expect(call().callId, id);
            expect(call().status, VoiceCallStatus.connected);
            await save('background-return');
          }
          await controller.hangUp();
          await until(() => !call().active, '$scenario-ended');
          record['native_ended'] = await nativeState();
          expect((record['native_ended'] as Map)['active'], false);
          controller.dismissEnded();
          await save('$scenario-ended');
        }
      } finally {
        await controller.hangUp();
        if (original != null) {
          await mic.writeAsBytes(original);
        } else if (mic.existsSync()) {
          // Only the QA input created by this test is removed.
          await mic.delete();
        }
      }
    },
    timeout: const Timeout(Duration(minutes: 25)),
  );
}
