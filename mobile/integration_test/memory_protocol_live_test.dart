import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/chat_location.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/memory/knowledge_screen.dart';
import 'package:bossip_mobile/features/settings/widgets/assistant_section.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';

/// Explicit opt-in against the isolated local QA backend, real HTTP/WS and
/// native storage. No provider overrides. Secrets arrive in an ignored file.
/// Both phases require `flutter test --no-uninstall`; Flutter otherwise
/// removes the app and its saved sign-in/bookmark before the resume phase.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const username = String.fromEnvironment('MEMORY_QA_USERNAME');
  const password = String.fromEnvironment('MEMORY_QA_PASSWORD');
  const session = String.fromEnvironment('MEMORY_QA_SESSION');
  const resumeOnly = bool.fromEnvironment('MEMORY_QA_RESUME_ONLY');
  testWidgets(
    'native profile save, live revision, knowledge and restore',
    (tester) async {
      expect(
        Env.apiBase,
        Platform.isAndroid ? 'http://10.0.2.2:8082' : 'http://127.0.0.1:8082',
      );
      expect(
        username.isNotEmpty && password.isNotEmpty && session.isNotEmpty,
        isTrue,
      );
      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      final evidence = <String, Object?>{
        'platform': Platform.operatingSystem,
        'api': Env.apiBase,
        'resume_only': resumeOnly,
      };
      var converted = false;
      Future<void> save(String name) async {
        if (Platform.isAndroid && !converted) {
          await binding.convertFlutterSurfaceToImage();
          converted = true;
          await tester.pump();
        }
        final root = Directory(
          '${(await getApplicationSupportDirectory()).path}/memory-qa',
        );
        await root.create(recursive: true);
        await File(
          '${root.path}/$name.png',
        ).writeAsBytes(await binding.takeScreenshot(name));
        await File(
          '${root.path}/evidence.json',
        ).writeAsString(jsonEncode(evidence));
        debugPrint('MEMORY_NATIVE_EVIDENCE ${root.path}/$name.png');
      }

      Future<void> until(bool Function() ready, String stage) async {
        final end = DateTime.now().add(const Duration(seconds: 45));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 100));
        }
        if (!ready()) await save('failure-$stage');
        expect(ready(), isTrue, reason: stage);
        debugPrint('MEMORY_NATIVE_STAGE $stage');
      }

      final router = container.read(routerProvider);
      if (resumeOnly) {
        expect(container.read(authProvider).isAuthenticated, isTrue);
        await until(
          () => find.byType(ChatScreen).evaluate().isNotEmpty,
          'cold-chat-restored',
        );
        expect(
          router.routeInformationProvider.value.uri.path,
          Paths.chat(session),
        );
        await until(
          () => find
              .textContaining('古筝', findRichText: true)
              .evaluate()
              .isNotEmpty,
          'cold-history-visible',
        );
        evidence['cold_restore'] = true;
        await save('cold-restore');
        return;
      }
      if (container.read(authProvider).user?.username != username) {
        await container.read(authFlowProvider).login(username, password);
      }
      expect(container.read(authProvider).user?.username, username);
      router.go(Paths.settings('assistant'));
      await until(
        () =>
            find.byType(AssistantSection).evaluate().isNotEmpty &&
            container.read(assistantProfileProvider).valueOrNull != null,
        'settings-ready',
      );
      final original = container.read(assistantProfileProvider).requireValue;
      await until(
        () => container.read(wsClientProvider).connected,
        'settings-connected',
      );
      final dio = container.read(apiDioProvider);
      try {
        final address = find.byKey(const ValueKey('assistant-address'));
        await tester.enterText(address, '移动端林姐');
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 300));
        final button = find.byKey(const ValueKey('assistant-save'));
        await tester.scrollUntilVisible(
          button,
          350,
          scrollable: find
              .descendant(
                of: find.byType(AssistantSection),
                matching: find.byType(Scrollable),
              )
              .first,
        );
        await tester.tap(button);
        await until(
          () =>
              container.read(assistantProfileProvider).valueOrNull?.address ==
              '移动端林姐',
          'ui-save',
        );
        final saved = (await dio.get<Map<String, dynamic>>(
          '/api/assistant/profile',
        )).data!;
        expect(saved['address'], '移动端林姐');
        evidence['saved_revision'] = saved['revision'];
        await save('settings-saved');
        // The normal HTTP command represents a second client. The first client
        // must receive it through its actual websocket, without a manual reload.
        await dio.put<Map<String, dynamic>>(
          '/api/assistant/profile',
          data: {
            'address': '跨端林姐',
            'expected_revision': saved['revision'],
            'operation_id':
                'native-live-${DateTime.now().microsecondsSinceEpoch}',
          },
        );
        await until(
          () =>
              container.read(assistantProfileProvider).valueOrNull?.address ==
              '跨端林姐',
          'live-revision',
        );
        await tester.scrollUntilVisible(
          address,
          -350,
          scrollable: find
              .descendant(
                of: find.byType(AssistantSection),
                matching: find.byType(Scrollable),
              )
              .first,
        );
        expect(tester.widget<TextField>(address).controller?.text, '跨端林姐');
        evidence['live_revision'] = container
            .read(assistantProfileProvider)
            .requireValue
            .revision;
        await save('live-update');
        router.go(Paths.wiki(view: 'files'));
        await until(
          () => find.byType(KnowledgeScreen).evaluate().isNotEmpty,
          'knowledge',
        );
        await until(
          () => find.textContaining('青禾').evaluate().isNotEmpty,
          'shared-document',
        );
        evidence['knowledge_file_visible'] = true;
        await save('knowledge-file');
      } finally {
        // Restore using the server's latest version, including after a failed
        // live update, so cleanup cannot hide the original assertion failure.
        final current = (await dio.get<Map<String, dynamic>>(
          '/api/assistant/profile',
        )).data!;
        await dio.put<Map<String, dynamic>>(
          '/api/assistant/profile',
          data: {
            'address': original.address,
            'expected_revision': current['revision'],
            'operation_id':
                'native-restore-${DateTime.now().microsecondsSinceEpoch}',
          },
        );
      }
      router.go(Paths.chat(session));
      await until(() => find.byType(ChatScreen).evaluate().isNotEmpty, 'chat');
      await tester.pump(const Duration(seconds: 1));
      await until(
        () =>
            container
                .read(chatLocationStoreProvider)
                .read(container.read(assistantScopeProvider)!) ==
            Paths.chat(session),
        'chat-persisted',
      );
      evidence['last_chat'] = router.routeInformationProvider.value.uri.path;
      expect(evidence['last_chat'], Paths.chat(session));
      await save('chat-before-restart');
    },
    timeout: const Timeout(Duration(minutes: 5)),
  );
}
