import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'an execution conversation loads from its history like any chat, with no source re-check',
    (tester) async {
      final fixture = await SuggestionFixture.create(language: 'en-US');
      addTearDown(fixture.dispose);
      final api = fixture.api..assistantManaged = true;
      final paths = <String>[];
      api.dio.interceptors.insert(
        0,
        InterceptorsWrapper(
          onRequest: (options, handler) {
            paths.add(options.path);
            handler.next(options);
          },
        ),
      );
      api.messages = [
        const ChatMessage(
          id: 'm01',
          sessionId: 's1',
          role: 'user',
          parts: [TextPart(id: 'ask', text: 'Make the page dark')],
        ),
        answer(
          id: 'm02',
          parts: [const TextPart(id: 'reply', text: 'The page is dark now.')],
        ),
      ];
      final writes = <Object?>[];
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') writes.add(call.arguments);
          return null;
        },
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        ),
      );
      await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
      await tester.pumpAndSettle();
      expect(find.text('Make the page dark'), findsOneWidget);
      expect(find.text('The page is dark now.'), findsOneWidget);
      expect(api.historyReads, [
        (before: null, after: null, turns: chatHistoryTurns),
      ]);
      // Nothing is hidden behind a source check, and nothing else is read.
      expect(find.text('Unavailable'), findsNothing);
      expect(paths, isEmpty);
      // The history is kept as evidence: copying works, rewriting does not.
      await tester.longPress(find.text('The page is dark now.'));
      await tester.pumpAndSettle();
      expect(find.text('Copy'), findsOneWidget);
      expect(find.text('Regenerate'), findsNothing);
      expect(find.text('Fork to a new chat'), findsNothing);
      await tester.tap(find.text('Copy'));
      await tester.pumpAndSettle();
      // A copied answer keeps its AI-generated label.
      expect(writes.single, {'text': 'The page is dark now.\n\nAI-generated'});
      expect(paths, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  test('the stream store merges an execution conversation like any chat', () {
    final ws = SuggestionWs();
    final container = ProviderContainer(
      overrides: [wsClientProvider.overrideWithValue(ws)],
    );
    addTearDown(() async {
      container.dispose();
      await ws.close();
    });
    final stream = container.read(chatStreamProvider.notifier);
    // Fields from the old source projection are simply not read.
    stream.mergeHistory('execution', [
      ChatMessage.fromJson({
        'id': 'm02',
        'session_id': 'execution',
        'role': 'assistant',
        'source_status': 'unavailable',
        'source_checked_at': '2026-10-04T00:00:02Z',
        'parts': [
          {'id': 'p', 'type': 'text', 'text': 'Working'},
        ],
      }),
    ]);
    stream.appendPartDelta('execution', 'm02', 'p', ' on it');
    stream.updateMessage('execution', {'id': 'm02', 'finish': 'stop'});
    final held = container.read(chatStreamProvider).messagesOf('execution');
    expect((held.single.parts.single as TextPart).text, 'Working on it');
    expect(held.single.finish, 'stop');
  });
}
