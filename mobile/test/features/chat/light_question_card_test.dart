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

  final folded = <String>[];
  int? foldFailure;

  @override
  Future<QuestionRequest> foldQuestion(String requestId) async {
    if (foldFailure != null) {
      throw DioException(
        requestOptions: RequestOptions(path: '/fold'),
        response: Response(
          requestOptions: RequestOptions(path: '/fold'),
          statusCode: foldFailure,
        ),
      );
    }
    folded.add(requestId);
    return QuestionRequest(
      id: requestId,
      sessionId: 's1',
      questions: current!.questions,
      folded: true,
      expiresAt: DateTime.now().add(const Duration(minutes: 30)),
    );
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

  testWidgets('先跳过 asks first, and rejects only on 跳过', (tester) async {
    final api = await _mount(tester, _single());
    await tester.tap(find.text('先跳过'));
    await tester.pumpAndSettle();
    expect(find.text('跳过这一步？'), findsOneWidget);
    expect(find.textContaining('不会扣积分'), findsOneWidget);
    await tester.tap(find.byKey(const Key('question-skip-cancel')));
    await tester.pumpAndSettle();
    expect(api.skipped, isEmpty);
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);

    await tester.tap(find.text('先跳过'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('question-skip-confirm')));
    await tester.pumpAndSettle();
    expect(api.skipped, ['q1']);
    expect(find.byKey(const Key('light-question-card')), findsNothing);
  });

  testWidgets('a plain card asks before 跳过 too', (tester) async {
    final api = await _mount(tester, _plain());
    await tester.tap(find.text('跳过'));
    await tester.pumpAndSettle();
    expect(api.skipped, isEmpty);
    await tester.tap(find.byKey(const Key('question-skip-confirm')));
    await tester.pumpAndSettle();
    expect(api.skipped, ['q3']);
  });

  testWidgets('稍后再说 folds the card to one row that opens again', (
    tester,
  ) async {
    final api = await _mount(tester, _single());
    await tester.tap(find.byKey(const Key('light-question-later')));
    await tester.pumpAndSettle();
    expect(api.folded, ['q1']);
    expect(api.skipped, isEmpty);
    expect(api.replies, isEmpty);
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.byKey(const Key('question-folded-row')), findsOneWidget);
    expect(find.text('多长？'), findsOneWidget);
    expect(find.text('已收起 · 还有 30 分钟失效'), findsOneWidget);

    // Opening it is local, and it can still be answered.
    await tester.tap(find.byKey(const Key('question-folded-row')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
    expect(api.folded, ['q1']);
    // Putting it away again does not restart the server's clock.
    await tester.tap(find.byKey(const Key('light-question-later')));
    await tester.pumpAndSettle();
    expect(api.folded, ['q1']);
    await tester.tap(find.byKey(const Key('question-folded-row')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('15 秒'));
    await tester.pumpAndSettle();
    expect(api.replies['q1'], [
      ['15 秒'],
    ]);
  });

  testWidgets('a plain card folds with 稍后再说', (tester) async {
    final api = await _mount(tester, _plain());
    await tester.tap(find.byKey(const Key('question-later')));
    await tester.pumpAndSettle();
    expect(api.folded, ['q3']);
    expect(find.byType(ChoiceChip), findsNothing);
    expect(find.byKey(const Key('question-folded-row')), findsOneWidget);
  });

  testWidgets('a card the server says is folded starts collapsed', (
    tester,
  ) async {
    final api = await _mount(
      tester,
      const QuestionRequest(
        id: 'q4',
        sessionId: 's1',
        folded: true,
        questions: [
          QuestionItem(
            header: '时长',
            question: '多长？',
            options: [QuestionOption(label: '15 秒')],
            detail: _light,
          ),
        ],
      ),
    );
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.text('时长'), findsOneWidget);
    expect(find.text('已收起 · 30 分钟内不处理会失效'), findsOneWidget);
    await tester.tap(find.byKey(const Key('question-folded-row')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
    expect(api.folded, isEmpty);
  });

  testWidgets('a card that is gone when folded leaves the screen', (
    tester,
  ) async {
    final api = await _mount(tester, _single());
    api.foldFailure = 410;
    await tester.tap(find.byKey(const Key('light-question-later')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.byKey(const Key('question-folded-row')), findsNothing);
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('cards with their own flow cannot be put away', (tester) async {
    await _mount(
      tester,
      const QuestionRequest(
        id: 'q5',
        sessionId: 's1',
        folded: true,
        questions: [
          QuestionItem(
            question: '确认脚本？',
            options: [QuestionOption(label: '确认')],
            detail: {'kind': 'video_script_approval', 'script_text': '讲稿'},
          ),
        ],
      ),
    );
    expect(find.byKey(const Key('question-folded-row')), findsNothing);
    expect(find.byKey(const Key('question-later')), findsNothing);
    expect(find.text('稍后再说'), findsNothing);
  });

  test('QuestionRequest reads folded and expires_at', () {
    final request = QuestionRequest.fromJson(const {
      'id': 'q',
      'session_id': 's',
      'questions': <Object>[],
      'folded': true,
      'expires_at': '2026-11-13T10:30:00Z',
    });
    expect(request.folded, isTrue);
    expect(request.expiresAt, DateTime.utc(2026, 11, 13, 10, 30));
    expect(
      QuestionRequest.fromJson(const {'id': 'q', 'session_id': 's'}).folded,
      isFalse,
    );
  });

  testWidgets('a plain card is untouched', (tester) async {
    await _mount(tester, _plain());
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.byType(ChoiceChip), findsOneWidget);
  });
}
