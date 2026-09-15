import 'dart:async';

import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

String _id(int n) => 'm${n.toString().padLeft(2, '0')}';

/// Twelve turns, each answer tall enough that eight of them overflow.
List<ChatMessage> _transcript() => [
  for (var i = 0; i < 12; i++) ...[
    ChatMessage(
      id: _id(2 * i),
      sessionId: 's1',
      role: 'user',
      parts: [TextPart(id: 'ask-$i', text: 'Question $i')],
    ),
    answer(
      id: _id(2 * i + 1),
      parts: [
        TextPart(
          id: 'reply-$i',
          text: List.filled(6, 'A line of the answer.').join('\n\n'),
        ),
      ],
    ),
  ],
];

Finder get _list => find.descendant(
  of: find.byType(ChatFlow),
  matching: find.byType(CustomScrollView),
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  testWidgets('reaching the top loads the turns before, in place', (
    tester,
  ) async {
    fixture.api.messages = _transcript();
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    // Opens pinned to the bottom of the newest eight turns, asking for
    // nothing older.
    expect(fixture.api.historyReads, [
      (before: null, after: null, turns: chatHistoryTurns),
    ]);
    expect(find.text('Question 11'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);

    // Up to just short of the top: near enough to ask for the page before,
    // without an overscroll stretch that would move rows by itself.
    final position = tester
        .widget<CustomScrollView>(_list)
        .controller!
        .position;
    fixture.api.historyGate = Completer<void>();
    await tester.drag(_list, Offset(0, position.pixels - 100));
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

    await tester.drag(_list, const Offset(0, 4000));
    await tester.pumpAndSettle();
    expect(find.text('Question 0'), findsOneWidget);
    // That page reached the start: nothing further is asked for.
    expect(fixture.api.historyReads, hasLength(2));
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('a failed older page is asked for once per visit to the top', (
    tester,
  ) async {
    fixture.api.messages = _transcript();
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    final position = tester
        .widget<CustomScrollView>(_list)
        .controller!
        .position;
    int olderReads() =>
        fixture.api.historyReads.where((read) => read.before != null).length;

    fixture.api.failOlder = true;
    await tester.drag(_list, Offset(0, position.pixels - 100));
    await tester.pump();
    expect(olderReads(), 1);

    // Nudging about at the same top asks for nothing more. Every scroll
    // update here used to send the failed page again.
    for (var i = 0; i < 5; i++) {
      await tester.drag(_list, const Offset(0, -30));
      await tester.pump();
      await tester.drag(_list, const Offset(0, 30));
      await tester.pump();
    }
    expect(olderReads(), 1);

    // Leaving the top and coming back is the retry.
    fixture.api.failOlder = false;
    await tester.drag(_list, const Offset(0, -800));
    await tester.pumpAndSettle();
    await tester.drag(_list, Offset(0, position.pixels - 100));
    await tester.pumpAndSettle();
    expect(olderReads(), 2);
    expect(
      fixture.container.read(chatStreamProvider).messagesOf('s1'),
      hasLength(24),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('leaving a live chat stops its catch-up until it is back', (
    tester,
  ) async {
    fixture.api
      ..messages = _transcript()
      ..status = 'busy';
    final showing = ValueNotifier(true);
    addTearDown(showing.dispose);
    await tester.pumpWidget(
      fixture.app(
        ValueListenableBuilder<bool>(
          valueListenable: showing,
          builder: (context, visible, child) => visible
              ? const ChatScreen(sessionId: 's1')
              : const Text('Elsewhere'),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(fixture.api.historyReads.last, (
      before: null,
      after: 'm23',
      turns: null,
    ));

    showing.value = false;
    await tester.pump();
    final reads = fixture.api.historyReads.length;
    final sessionReads = fixture.api.sessionReads;
    await tester.pump(const Duration(seconds: 5));
    expect(fixture.api.historyReads, hasLength(reads));
    expect(fixture.api.sessionReads, sessionReads);

    // Back on it: the newest turns once, then the catch-up again.
    showing.value = true;
    await tester.pump();
    expect(fixture.api.historyReads.last, (
      before: null,
      after: null,
      turns: chatHistoryTurns,
    ));
    await tester.pump(const Duration(seconds: 1));
    expect(fixture.api.historyReads.last, (
      before: null,
      after: 'm23',
      turns: null,
    ));
    // Unmounted here, so the poll timer is gone before the fake clock is
    // checked.
    await tester.pumpWidget(const SizedBox());
  });
}
