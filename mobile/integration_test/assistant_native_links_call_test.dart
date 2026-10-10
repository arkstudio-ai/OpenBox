import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/features/voice/platform/system_voice_call.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/voice_call_page.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';

/// Actual local HTTP/WS, app bootstrap, call audio and native OS surfaces.
/// No provider overrides or lifecycle simulation. The operator uses Simulator/
/// Emulator's Home control, then the system call return UI during the hold.
/// Credentials are supplied only through an ignored --dart-define-from-file.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const username = String.fromEnvironment('PA_QA_USERNAME');
  const password = String.fromEnvironment('PA_QA_PASSWORD');
  const session = String.fromEnvironment('PA_QA_LINK_SESSION');
  const fakeMic = bool.fromEnvironment('VOICE_FAKE_MIC');
  const permissionOnly = bool.fromEnvironment('PA_QA_PERMISSION_ONLY');

  testWidgets(
    'assistant conversation link and OS background call',
    (tester) async {
      final semantics = tester.ensureSemantics();
      addTearDown(semantics.dispose);
      expect(Env.apiBase, 'http://127.0.0.1:8081');
      expect(Env.webBase, 'http://127.0.0.1:3001');
      expect(
        username.isNotEmpty && password.isNotEmpty && session.isNotEmpty,
        isTrue,
      );
      final network = <Map<String, Object?>>[];
      final evidence = <String, Object?>{
        'platform': Platform.operatingSystem,
        'api': Env.apiBase,
        'web': Env.webBase,
        'fake_mic': fakeMic,
      };
      late ProviderContainer container;
      var convertedSurface = false;
      Future<Map<String, dynamic>> nativeState() async =>
          Map<String, dynamic>.from(
            await DeviceSystemVoiceCall.channel
                    .invokeMapMethod<String, dynamic>('state') ??
                {},
          );
      Future<void> save(String name, {bool screenshot = true}) async {
        final dir = await getApplicationSupportDirectory();
        final root = Directory('${dir.path}/native-call-qa');
        await root.create(recursive: true);
        if (screenshot) {
          if (Platform.isAndroid && !convertedSurface) {
            await binding.convertFlutterSurfaceToImage();
            convertedSurface = true;
            await tester.pump();
          }
          final bytes = await binding.takeScreenshot(name);
          await File('${root.path}/$name.png').writeAsBytes(bytes);
        }
        await File(
          '${root.path}/evidence.json',
        ).writeAsString(jsonEncode({...evidence, 'network': network}));
        debugPrint('NATIVE_CALL_EVIDENCE ${root.path}');
      }

      Future<void> waitFor(
        bool Function() ready,
        String stage, {
        int seconds = 50,
      }) async {
        final deadline = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(deadline)) {
          await tester.pump(const Duration(milliseconds: 200));
        }
        if (!ready()) {
          final call = container.read(voiceCallControllerProvider);
          evidence['failure_stage'] = stage;
          evidence['failure_call_status'] = call.status.name;
          evidence['failure_end'] = call.end?.reason.wire;
          evidence['failure_detail'] = call.end?.detailKey;
          await save('failure-$stage');
          debugPrint(
            'NATIVE_CALL_FAILURE $stage ${call.status.name} ${call.end?.reason.wire} ${call.end?.detailKey}',
          );
        }
        expect(ready(), isTrue, reason: 'Native stage $stage');
        debugPrint('NATIVE_CALL_STAGE $stage');
      }

      await app.main();
      await tester.pump();
      container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      container
          .read(apiDioProvider)
          .interceptors
          .add(
            InterceptorsWrapper(
              onResponse: (response, handler) {
                network.add({
                  'path': response.requestOptions.path,
                  'status': response.statusCode,
                });
                handler.next(response);
              },
            ),
          );
      // Authenticate an existing, already registered local QA account through
      // the real auth flow. This test does not register users or change consent.
      if (!container.read(authProvider).isAuthenticated ||
          container.read(authProvider).user?.username != username) {
        await container.read(authFlowProvider).login(username, password);
      }
      final router = container.read(routerProvider);
      router.go(Paths.assistant);
      await waitFor(
        () =>
            find.byType(AssistantScreen).evaluate().isNotEmpty &&
            container.read(assistantScopeProvider) != null,
        'assistant',
      );
      final scope = container.read(assistantScopeProvider)!;
      await waitFor(
        () =>
            !container.read(assistantControllerProvider(scope)).loading &&
            container
                .read(assistantControllerProvider(scope))
                .messages
                .isNotEmpty,
        'history',
      );
      await tester.pump(const Duration(milliseconds: 500));
      final link = find.text('打开会话记录', findRichText: true);
      // The older video report is virtualized above the latest image report.
      // Scroll the actual transcript until that row is laid out.
      for (var i = 0; i < 12 && link.evaluate().isEmpty; i++) {
        await tester.drag(find.byType(ChatFlow).last, const Offset(0, 550));
        await tester.pump(const Duration(milliseconds: 500));
      }
      await waitFor(() => link.evaluate().isNotEmpty, 'blue-link');
      await tester.ensureVisible(link.last);
      await tester.pump(const Duration(milliseconds: 300));
      await save('assistant-link');
      await tester.tap(link.last);
      await waitFor(
        () => find
            .byType(ChatScreen)
            .evaluate()
            .any(
              (element) => (element.widget as ChatScreen).sessionId == session,
            ),
        'destination-conversation',
      );
      await waitFor(
        () =>
            container.read(chatSessionProvider(session)).session?.id == session,
        'destination-http',
      );
      evidence['link_session'] = session;
      evidence['link_route'] = router.routeInformationProvider.value.uri
          .toString();
      await save('conversation-opened');
      router.pop();
      await waitFor(
        () => find.byType(AssistantScreen).evaluate().isNotEmpty,
        'assistant-return',
      );
      await tester.pumpAndSettle();

      // Use the entry button so permission explanation and system settings are
      // exercised too. OS permission dialogs are answered by the operator.
      final voiceButton = find
          .byKey(const Key('voice-call-button'))
          .hitTestable();
      await waitFor(
        () => voiceButton.evaluate().isNotEmpty,
        'voice-entry-ready',
      );
      await tester.tap(voiceButton);
      await tester.pump(const Duration(milliseconds: 400));
      final allow = find.byKey(const Key('voice-permission-allow'));
      if (allow.evaluate().isNotEmpty) {
        await tester.tap(allow);
        await tester.pump(const Duration(milliseconds: 400));
      }
      await save('voice-permission-or-start');
      if (permissionOnly) {
        debugPrint('NATIVE_CALL_STAGE permission-dialog');
        await Future<void>.delayed(const Duration(seconds: 15));
        await save('overlay-permission-dialog');
        return;
      }
      await waitFor(
        () =>
            container.read(voiceCallControllerProvider).status ==
            VoiceCallStatus.connected,
        'connected',
        seconds: 90,
      );
      final controller = container.read(voiceCallControllerProvider.notifier);
      VoiceCallState call() => container.read(voiceCallControllerProvider);
      final callID = call().callId;
      expect(callID, isNotNull);
      evidence['call_id'] = callID;
      evidence['native_foreground'] = await nativeState();
      await save('call-connected');
      // Collapse first: the system return entry must reopen the same call page.
      router.pop();
      await waitFor(() => !call().expanded, 'collapsed-ready-for-home');
      await tester.pump(const Duration(milliseconds: 800));
      await tester.runAsync(() async {
        final deadline = DateTime.now().add(const Duration(seconds: 90));
        while (binding.lifecycleState != AppLifecycleState.paused &&
            binding.lifecycleState != AppLifecycleState.hidden &&
            DateTime.now().isBefore(deadline)) {
          await Future<void>.delayed(const Duration(milliseconds: 250));
        }
        expect(
          const {
            AppLifecycleState.paused,
            AppLifecycleState.hidden,
          }.contains(binding.lifecycleState),
          isTrue,
          reason:
              'Use the native Home button, never fake a Flutter lifecycle event',
        );
        final began = DateTime.now();
        debugPrint('NATIVE_CALL_STAGE background-hold');
        final observations = <Map<String, Object?>>[];
        while (DateTime.now().difference(began).inSeconds < 130) {
          await Future<void>.delayed(const Duration(seconds: 5));
          expect(call().status, VoiceCallStatus.connected);
          expect(call().callId, callID);
          final native = await nativeState();
          expect(native['active'], true);
          if (Platform.isAndroid) {
            expect(native['overlayGranted'], true);
            expect(native['overlayVisible'], true);
          } else {
            expect(native['surface'], 'callkit');
            expect(native['audioActive'], true);
          }
          observations.add({
            'elapsed': DateTime.now().difference(began).inSeconds,
            'native': native,
          });
        }
        evidence['background_seconds'] = DateTime.now()
            .difference(began)
            .inSeconds;
        evidence['background_observations'] = observations;
        await save('background-held', screenshot: false);
        debugPrint('NATIVE_CALL_STAGE use-system-return');
        final returnDeadline = DateTime.now().add(const Duration(seconds: 90));
        while (binding.lifecycleState != AppLifecycleState.resumed &&
            DateTime.now().isBefore(returnDeadline)) {
          await Future<void>.delayed(const Duration(milliseconds: 250));
        }
        expect(binding.lifecycleState, AppLifecycleState.resumed);
      });
      await waitFor(
        () => find.byType(VoiceCallPage).evaluate().isNotEmpty,
        'returned-same-call',
      );
      expect(call().callId, callID);
      expect(call().status, VoiceCallStatus.connected);
      evidence['native_returned'] = await nativeState();
      await save('call-returned');
      await controller.hangUp();
      await waitFor(() => call().status == VoiceCallStatus.ended, 'ended');
      final ended = await nativeState();
      expect(ended['active'], false);
      if (Platform.isAndroid) expect(ended['overlayVisible'], false);
      evidence['native_ended'] = ended;
      evidence['end_reason'] = call().end?.reason.wire;
      await save('call-ended');
    },
    timeout: const Timeout(Duration(minutes: 8)),
  );
}
