import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _FakeApi extends ChatApi {
  _FakeApi() : super(Dio());

  final replies = <String, List<List<String>>>{};
  final skipped = <String>[];
  QuestionRequest? current;

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers, {
    List<List<String>>? attachments,
  }) async {
    replies[requestId] = answers;
  }

  @override
  Future<void> rejectQuestion(String requestId) async {
    skipped.add(requestId);
  }

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];

  @override
  Future<QuestionRequest> getQuestion(String id) async => current!;

  @override
  Future<QuestionRequest> saveQuestionDraft(
    String id,
    List<QuestionDraftAnswer> draft,
    int revision,
  ) async => QuestionRequest(
    id: id,
    sessionId: 's1',
    questions: current!.questions,
    draft: draft,
    draftRevision: revision + 1,
  );
}

const _light = {
  'light': {'entry_id': 'video', 'step_id': 'confirm', 'kind': 'choice'},
};

QuestionRequest _single() => const QuestionRequest(
  id: 'q1',
  sessionId: 's1',
  questions: [
    QuestionItem(
      question: '多长？',
      options: [
        QuestionOption(label: '15 秒', description: '最快'),
        QuestionOption(label: '30 秒'),
      ],
      detail: _light,
    ),
  ],
);

QuestionRequest _multiple() => const QuestionRequest(
  id: 'q2',
  sessionId: 's1',
  questions: [
    QuestionItem(
      question: '加什么？',
      multiple: true,
      options: [
        QuestionOption(label: '字幕'),
        QuestionOption(label: '配乐'),
      ],
      detail: _light,
    ),
  ],
);

QuestionRequest _plain() => const QuestionRequest(
  id: 'q3',
  sessionId: 's1',
  questions: [
    QuestionItem(
      question: 'Plain?',
      options: [QuestionOption(label: 'A')],
    ),
  ],
);

class _Dock extends ConsumerWidget {
  const _Dock();
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final questions = ref.watch(pendingProvider).questionsOf('s1');
    return Scaffold(
      body: ListView(
        children: [
          for (final question in questions)
            QuestionDock(key: ValueKey(question.id), request: question),
        ],
      ),
    );
  }
}

Future<_FakeApi> _mount(WidgetTester tester, QuestionRequest request) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final api = _FakeApi()..current = request;
  final container = ProviderContainer(
    overrides: [
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      chatApiProvider.overrideWithValue(api),
      apiDioProvider.overrideWithValue(Dio()),
      prefsProvider.overrideWithValue(prefs),
    ],
  );
  addTearDown(container.dispose);
  container.read(pendingProvider.notifier).seed(const [], [request]);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: const _Dock(),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return api;
}

void main() {
  testWidgets('a Light-tagged card wears big chips; one tap is the answer', (
    tester,
  ) async {
    final api = await _mount(tester, _single());
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
    expect(find.byType(ChoiceChip), findsNothing);
    expect(find.text('15 秒'), findsOneWidget);
    expect(find.text('最快'), findsOneWidget);
    expect(find.text('就这样'), findsOneWidget);
    await tester.tap(find.text('15 秒'));
    await tester.pumpAndSettle();
    expect(api.replies['q1'], [
      ['15 秒'],
    ]);
    expect(find.byKey(const Key('light-question-card')), findsNothing);
  });

  testWidgets('a multiple-choice card waits for 就这样', (tester) async {
    final api = await _mount(tester, _multiple());
    final submit = find.byKey(const Key('light-question-submit'));
    expect(tester.widget<FilledButton>(submit).onPressed, isNull);
    await tester.tap(find.text('字幕'));
    await tester.pump();
    await tester.tap(find.text('配乐'));
    await tester.pump();
    expect(api.replies, isEmpty);
    expect(tester.widget<FilledButton>(submit).onPressed, isNotNull);
    await tester.tap(submit);
    await tester.pumpAndSettle();
    expect(api.replies['q2'], [
      ['字幕', '配乐'],
    ]);
  });

  testWidgets('先跳过 rejects the question', (tester) async {
    final api = await _mount(tester, _single());
    await tester.tap(find.text('先跳过'));
    await tester.pumpAndSettle();
    expect(api.skipped, ['q1']);
  });

  testWidgets('a plain card is untouched', (tester) async {
    await _mount(tester, _plain());
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.byType(ChoiceChip), findsOneWidget);
  });
}
