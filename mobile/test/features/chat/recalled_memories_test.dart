import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/widgets/recalled_memories.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';
import 'assistant_fixture.dart';

void main() {
  testWidgets('finishing a reply safely refreshes all memory badges in the chat', (
    tester,
  ) async {
    final api = TestApi()
      ..recalls = {
        'old': [const RecalledMemory(id: 'old-memory', summary: '喜欢安静')],
      };
    final streaming = ValueNotifier(true);
    addTearDown(streaming.dispose);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          assistantApiProvider(scope).overrideWithValue(api),
        ],
        child: MaterialApp(
          theme: testTheme(),
          home: Scaffold(
            body: ValueListenableBuilder(
              valueListenable: streaming,
              builder: (context, active, _) => Column(
                children: [
                  const RecalledMemories(
                    key: ValueKey('old-reply'),
                    scope: scope,
                    sessionId: 'main',
                    messageId: 'old',
                    streaming: false,
                  ),
                  RecalledMemories(
                    key: const ValueKey('new-reply'),
                    scope: scope,
                    sessionId: 'main',
                    messageId: 'new',
                    streaming: active,
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(api.recalledReads, 1);
    expect(find.byKey(const ValueKey('recalled-memories')), findsOneWidget);
    api.recalls = {
      ...api.recalls,
      'new': [const RecalledMemory(id: 'new-memory', summary: '周末学木雕')],
    };
    streaming.value = false;
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(api.recalledReads, 2);
    expect(find.byKey(const ValueKey('recalled-memories')), findsNWidgets(2));
    await tester.tap(find.byKey(const ValueKey('recalled-memories')).last);
    await tester.pumpAndSettle();
    expect(find.text('周末学木雕'), findsOneWidget);
    // Another streamed reply refreshes once again; a plain rebuild does not.
    streaming.value = true;
    await tester.pump();
    streaming.value = false;
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(api.recalledReads, 3);
  });
}
