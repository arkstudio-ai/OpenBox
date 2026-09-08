import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'question': {
        'title': 'agent wants to confirm',
        'reject': 'Reject',
        'submit': 'Confirm',
        'answer': 'Write your answer',
        'needAll': 'Answer all {{count}}',
      },
    },
  },
});

class _FakeApi extends ChatApi {
  _FakeApi() : super(Dio());

  final replies = <String, List<List<String>>>{};

  /// Set to hold the POST open, so a test can rebuild the card mid-flight.
  Completer<void>? gate;

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers,
  ) async {
    replies[requestId] = answers;
    await gate?.future;
  }

  @override
  Future<void> rejectQuestion(String requestId) async {}
}

QuestionRequest _request(String id) => QuestionRequest(
  id: id,
  sessionId: 's1',
  questions: const [
    QuestionItem(
      question: 'Pick one',
      header: 'Test dialog',
      options: [
        QuestionOption(label: 'Option A'),
        QuestionOption(label: 'Option B'),
      ],
    ),
  ],
);

/// Stands in for `ChatScreen`: pending questions pinned to the end of a
/// transcript that can grow underneath them.
class _Transcript extends ConsumerStatefulWidget {
  const _Transcript({required this.turns, this.rowHeight = 40});

  final int turns;
  final double rowHeight;

  @override
  ConsumerState<_Transcript> createState() => _TranscriptState();
}

class _TranscriptState extends ConsumerState<_Transcript> {
  late int _turns = widget.turns;

  void grow() => setState(() => _turns += 1);

  @override
  Widget build(BuildContext context) {
    final questions = ref.watch(pendingProvider).questionsOf('s1');
    return Scaffold(
      body: Column(
        children: [
          Expanded(
            child: ChatFlow(
              rows: [
                for (var i = 0; i < _turns; i++)
                  SizedBox(height: widget.rowHeight, child: Text('turn $i')),
                for (final question in questions)
                  QuestionDock(key: ValueKey(question.id), request: question),
              ],
            ),
          ),
          const SizedBox(height: 60),
        ],
      ),
    );
  }
}

Future<(_FakeApi, ProviderContainer)> _mount(
  WidgetTester tester,
  Widget home, {
  List<QuestionRequest> questions = const [],
}) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final api = _FakeApi();
  final container = ProviderContainer(
    overrides: [
      i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
      chatApiProvider.overrideWithValue(api),
      apiDioProvider.overrideWithValue(Dio()),
    ],
  );
  addTearDown(container.dispose);
  container.read(pendingProvider.notifier).seed(const [], questions);

  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: home,
      ),
    ),
  );
  await tester.pumpAndSettle();
  return (api, container);
}

bool _picked(WidgetTester tester) => tester
    .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Option A'))
    .selected;

bool _canSubmit(WidgetTester tester) =>
    tester.widget<FilledButton>(find.byType(FilledButton)).onPressed != null;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('a chosen option survives the transcript growing under it', (
    tester,
  ) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 1),
      questions: [_request('req-1')],
    );

    await tester.tap(find.text('Option A'));
    await tester.pumpAndSettle();
    expect(_picked(tester), isTrue);

    // The agent streams another turn while the question waits, so the dock is
    // rebuilt one slot lower. It used to come back blank, with 确认 disabled
    // again — the card looked alive but no longer answered a tap.
    tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
    await tester.pumpAndSettle();

    expect(_picked(tester), isTrue);
    expect(_canSubmit(tester), isTrue);

    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(api.replies, {
      'req-1': [
        ['Option A'],
      ],
    });
    expect(find.byType(QuestionDock), findsNothing);
  });

  testWidgets('an answered card is dismissed even if it is rebuilt mid-reply', (
    tester,
  ) async {
    final (api, container) = await _mount(
      tester,
      const _Transcript(turns: 1),
      questions: [_request('req-1')],
    );
    api.gate = Completer<void>();

    await tester.tap(find.text('Option A'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Confirm'));
    await tester.pump();

    // Mid-flight: the reply has landed, the agent resumed, and its next turn
    // arrives before the POST completes — which rebuilds this card. Reading
    // `ref` afterwards used to throw, so the card was never taken away and
    // every further tap hit a request the backend had already consumed.
    tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
    await tester.pump();
    api.gate!.complete();
    await tester.pumpAndSettle();

    expect(container.read(pendingProvider).questionsOf('s1'), isEmpty);
    expect(find.byType(QuestionDock), findsNothing);
  });

  testWidgets('a typed answer survives scrolling the dock out of view', (
    tester,
  ) async {
    await _mount(
      tester,
      const _Transcript(turns: 12, rowHeight: 120),
      questions: [_request('req-1')],
    );
    await tester.scrollUntilVisible(find.text('Option A'), 200);
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'my own answer');
    await tester.pumpAndSettle();
    expect(_canSubmit(tester), isTrue);

    // Out of the lazy viewport and back: the card is a different element now.
    await tester.drag(find.byType(ListView), const Offset(0, 4000));
    await tester.pumpAndSettle();
    expect(find.text('Option A'), findsNothing);
    await tester.drag(find.byType(ListView), const Offset(0, -4000));
    await tester.pumpAndSettle();

    expect(
      tester.widget<TextField>(find.byType(TextField)).controller?.text,
      'my own answer',
    );
    expect(_canSubmit(tester), isTrue);
  });

  testWidgets('an unanswered question keeps the submit button disabled', (
    tester,
  ) async {
    await _mount(
      tester,
      const _Transcript(turns: 1),
      questions: [_request('req-1')],
    );
    expect(_canSubmit(tester), isFalse);
  });

  test('an absent `custom` flag still allows a typed answer (web parity)', () {
    expect(QuestionItem.fromJson({'question': 'Pick one'}).custom, isTrue);
    expect(
      QuestionItem.fromJson({'question': 'Pick one', 'custom': false}).custom,
      isFalse,
    );
  });
}
