import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/assistant_scope.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/voice_message_input.dart';
import 'package:bossip_mobile/features/chat/widgets/voice_message_bubble.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'voice_message_live_test.dart' show FixtureRecorder;

/// Opt-in local acceptance. Only microphone bytes are substituted: the real
/// dialogue widgets, OSS upload, ASR, target Agent, history and playback run.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets(
    'ordinary and Light dialogues send voice to their own Agent',
    (tester) async {
      const base = String.fromEnvironment('API_BASE');
      expect(Uri.parse(base).host, anyOf('localhost', '127.0.0.1'));
      final prefs = await SharedPreferences.getInstance();
      final support = await getApplicationSupportDirectory();
      final documents = await getApplicationDocumentsDirectory();
      final fixture = '${documents.path}/dialogue-voice-fixture.m4a';
      final client = HttpClient();
      try {
        final response = await (await client.getUrl(
          Uri.parse(const String.fromEnvironment('VOICE_MESSAGE_FIXTURE_URL')),
        )).close();
        expect(response.statusCode, 200);
        await response.pipe(File(fixture).openWrite());
      } finally {
        client.close();
      }
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
      runApp(
        UncontrolledProviderScope(
          container: container,
          child: const BossipApp(),
        ),
      );
      Future<void> until(bool Function() done, {int seconds = 120}) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!done() && DateTime.now().isBefore(end)) {
          final coach = find.text('知道了').hitTestable();
          if (coach.evaluate().isNotEmpty) await tester.tap(coach.first);
          await tester.pump(const Duration(milliseconds: 150));
          await Future<void>.delayed(const Duration(milliseconds: 150));
        }
        expect(done(), isTrue);
      }

      await until(() => container.read(assistantScopeProvider) != null);
      final session = await container.read(chatApiProvider).createSession();
      for (final light in [false, true]) {
        final kind = light ? 'light' : 'ordinary';
        container
            .read(routerProvider)
            .go(light ? Paths.lightChat(session.id) : Paths.chat(session.id));
        final toggle = Key(light ? 'light-voice-mode' : 'voice-message-mode');
        await until(() => find.byKey(toggle).hitTestable().evaluate().isNotEmpty);
        final before = container
            .read(chatStreamProvider)
            .messagesOf(session.id)
            .map((m) => m.id)
            .toSet();
        await tester.tap(find.byKey(toggle));
        await tester.pump();
        final hold = find.byKey(const Key('voice-message-hold')).hitTestable();
        // During the route transition both composers can remain mounted.
        await until(() => hold.evaluate().length == 1);
        final gesture = await tester.startGesture(tester.getCenter(hold));
        await tester.pump(const Duration(milliseconds: 650));
        await Future<void>.delayed(const Duration(milliseconds: 1300));
        await tester.pump();
        await gesture.up();
        String? sentId, assetId;
        await until(() {
          final messages = container
              .read(chatStreamProvider)
              .messagesOf(session.id);
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
        final history = await container
            .read(chatApiProvider)
            .history(session.id);
        expect(
          history.messages.any(
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
          seconds: 20,
        );
        await File(
          '${documents.path}/dialogue-voice-$kind.png',
        ).writeAsBytes(await binding.takeScreenshot('dialogue-voice-$kind'));
        await tester.tap(
          find.descendant(of: bubble, matching: find.byType(InkWell)).first,
        );
        await container
            .read(chatSessionProvider(session.id).notifier)
            .refreshSession();
      }
    },
    skip: !const bool.fromEnvironment('DIALOGUE_VOICE_LIVE_TEST'),
  );
}
