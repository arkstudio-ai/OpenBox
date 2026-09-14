import 'dart:async';

import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  testWidgets('reaching the top loads the turns before, in place', (
    tester,
  ) async {
    String id(int n) => 'm${n.toString().padLeft(2, '0')}';
    fixture.api.messages = [
      for (var i = 0; i < 12; i++) ...[
        ChatMessage(
          id: id(2 * i),
          sessionId: 's1',
          role: 'user',
          parts: [TextPart(id: 'ask-$i', text: 'Question $i')],
        ),
        answer(
          id: id(2 * i + 1),
          parts: [
            TextPart(
              id: 'reply-$i',
              text: List.filled(6, 'A line of the answer.').join('\n\n'),
            ),
          ],
        ),
      ],
    ];
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    // Opens pinned to the bottom of the newest eight turns, asking for
    // nothing older.
    expect(fixture.api.historyReads, [
      (before: null, after: null, turns: chatHistoryTurns),
    ]);
    expect(find.text('Question 11'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);

    final list = find.descendant(
      of: find.byType(ChatFlow),
      matching: find.byType(CustomScrollView),
    );
    // Up to just short of the top: near enough to ask for the page before,
    // without an overscroll stretch that would move rows by itself.
    final position = tester.widget<CustomScrollView>(list).controller!.position;
    fixture.api.historyGate = Completer<void>();
    await tester.drag(list, Offset(0, position.pixels - 100));
    await tester.pump();
    expect(fixture.api.historyReads.last, (
      before: 'm08',
      after: null,
      turns: chatHistoryTurns,
    ));
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    final onScreen = tester.getTopLeft(find.text('Question 5'));

    fixture.api.historyGate!.complete();
    fixture.api.historyGate = null;
    await tester.pumpAndSettle();
    // The page went in above what was on screen, which did not move.
    expect(tester.getTopLeft(find.text('Question 5')), onScreen);
    expect(find.text('Question 3').hitTestable(), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);

    await tester.drag(list, const Offset(0, 4000));
    await tester.pumpAndSettle();
    expect(find.text('Question 0'), findsOneWidget);
    // That page reached the start: nothing further is asked for.
    expect(fixture.api.historyReads, hasLength(2));
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
