import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/assistant_scope.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/voice_message_input.dart';
import 'package:bossip_mobile/features/chat/widgets/voice_message_bubble.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Opt-in local acceptance: only microphone bytes are supplied by a fixture.
/// Upload, ASR, assistant submission/history and iOS playback are real.
class FixtureRecorder implements MessageRecorder {
  FixtureRecorder(this.source);
  final String source;
  @override
  Future<bool> permitted() async => true;
  @override
  Future<void> start(String path) async {
    await File(source).copy(path);
  }

  @override
  Future<void> stop() async {}
  @override
  Future<void> dispose() async {}
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets(
    'record, transcribe, send, replay and reload a voice message',
    (tester) async {
      expect(const bool.fromEnvironment('VOICE_MESSAGE_LIVE_TEST'), isTrue);
      final prefs = await SharedPreferences.getInstance();
      final support = await getApplicationSupportDirectory();
      final documents = await getApplicationDocumentsDirectory();
      final fixture = '${documents.path}/voice-message-fixture.m4a';
      const fixtureUrl = String.fromEnvironment('VOICE_MESSAGE_FIXTURE_URL');
      if (fixtureUrl.isNotEmpty) {
        final client = HttpClient();
        try {
          final response = await (await client.getUrl(
            Uri.parse(fixtureUrl),
          )).close();
          expect(response.statusCode, 200);
          await response.pipe(File(fixture).openWrite());
        } finally {
          client.close();
        }
      }
      expect(File(fixture).existsSync(), isTrue);
      final bundle = await I18nBundle.load();
      final container = ProviderContainer(
        overrides: [
          assistantScopeOverride,
          prefsProvider.overrideWithValue(prefs),
          cookieJarProvider.overrideWithValue(
            PersistCookieJar(storage: FileStorage('${support.path}/cookies')),
          ),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          messageRecorderFactoryProvider.overrideWithValue(
            () => FixtureRecorder(fixture),
          ),
        ],
      );
      await container.read(authProvider.notifier).bootstrap();
      if (container.read(authProvider).user == null) {
        await container
            .read(authFlowProvider)
            .login(
              const String.fromEnvironment('VOICE_MESSAGE_TEST_USER'),
              const String.fromEnvironment('VOICE_MESSAGE_TEST_PASSWORD'),
            );
      }
      expect(container.read(authProvider).user, isNotNull);
      runApp(
        UncontrolledProviderScope(
          container: container,
          child: const BossipApp(),
        ),
      );
      await tester.pump();
      container.read(routerProvider).go(Paths.assistant);

      Future<void> until(bool Function() done, {int seconds = 90}) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!done() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 150));
          await Future<void>.delayed(const Duration(milliseconds: 150));
        }
        expect(done(), isTrue);
      }

      await until(
        () => find.byKey(const Key('voice-message-mode')).evaluate().isNotEmpty,
      );
      final scope = container.read(assistantScopeProvider)!;
      final controller = container.read(
        assistantControllerProvider(scope).notifier,
      );
      await controller.refresh();
      final before = container
          .read(assistantControllerProvider(scope))
          .messages
          .map((m) => m.id)
          .toSet();
      await tester.tap(find.byKey(const Key('voice-message-mode')));
      await tester.pump();
      if (find.byKey(const Key('voice-message-retry')).evaluate().isNotEmpty) {
        final key = prefs.getKeys().singleWhere(
          (key) => key.startsWith(
            'voice-message:${scope.userId}:${scope.workspaceId}:',
          ),
        );
        final draft = jsonDecode(prefs.getString(key)!) as Map;
        expect(
          listEquals(
            await File(draft['path'] as String).readAsBytes(),
            await File(fixture).readAsBytes(),
          ),
          isTrue,
        );
        await tester.tap(find.byKey(const Key('voice-message-retry')));
      } else {
        final gesture = await tester.startGesture(
          tester.getCenter(find.byKey(const Key('voice-message-hold'))),
        );
        await tester.pump(const Duration(milliseconds: 650));
        await Future<void>.delayed(const Duration(milliseconds: 1200));
        await tester.pump();
        await gesture.up();
      }
      await tester.pump();
      String? sentId, assetId;
      await until(() {
        final messages = container
            .read(assistantControllerProvider(scope))
            .messages;
        for (final message in messages) {
          final voice = recordedVoice(message);
          if (voice != null &&
              !before.contains(message.id) &&
              !message.id.startsWith('tmp-')) {
            sentId = message.id;
            assetId = voice['asset_id'] as String;
          }
        }
        return sentId != null &&
            messages.any(
              (m) =>
                  m.isAssistant && m.parentId == sentId && m.finish == 'stop',
            );
      });
      await controller.refresh();
      expect(
        container
            .read(assistantControllerProvider(scope))
            .messages
            .any(
              (m) => m.id == sentId && recordedVoice(m)?['asset_id'] == assetId,
            ),
        isTrue,
      );
      final bubble = find.byWidgetPredicate(
        (w) => w is VoiceMessageBubble && w.assetId == assetId,
      );
      await tester.ensureVisible(bubble);
      await tester.pump();
      await tester.tap(
        find.descendant(of: bubble, matching: find.byType(InkWell)).first,
      );
      await until(
        () => find
            .descendant(of: bubble, matching: find.byIcon(Icons.pause))
            .evaluate()
            .isNotEmpty,
        seconds: 30,
      );
      await tester.tap(
        find.descendant(of: bubble, matching: find.byType(TextButton)).first,
      );
      await tester.pump();
      expect(find.textContaining('这是语音消息功能测试'), findsWidgets);
      final screenshot = await binding.takeScreenshot('voice-message-live');
      await File(
        '${documents.path}/voice-message-live.png',
      ).writeAsBytes(screenshot);
      const screenshotUrl = String.fromEnvironment(
        'VOICE_MESSAGE_SCREENSHOT_URL',
      );
      if (screenshotUrl.isNotEmpty) {
        final client = HttpClient();
        try {
          final request = await client.postUrl(Uri.parse(screenshotUrl));
          request.contentLength = screenshot.length;
          request.add(screenshot);
          expect((await request.close()).statusCode, 200);
        } finally {
          client.close();
        }
      }
      // Leave the actual production history intact for replay after app restart.
    },
    skip: !const bool.fromEnvironment('VOICE_MESSAGE_LIVE_TEST'),
  );
}
