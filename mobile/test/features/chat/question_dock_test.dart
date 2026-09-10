import 'dart:async';
import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/features/chat/state/question_draft.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

part 'question_dock_pager_cases.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'question': {
        'title': 'agent wants to confirm',
        'reject': 'Reject',
        'submit': 'Confirm',
        'answer': 'Write your answer',
        'needAll': 'Answer all {{count}}',
        'skip': 'Skip',
        'skipAll': 'Skip all',
        'submitting': 'Submitting',
        'submitFailed': 'Submission failed; answers preserved',
        'gone': 'Question no longer active',
        'progress': 'Answered {{answered}} / {{count}}',
        'previous': 'Previous',
        'next': 'Next',
        'page': 'Question {{current}} of {{count}}',
        'customNextHint': 'Finish typing, then use Next',
        'multipleNextHint': 'Choose all, then use Next',
        'saving': 'Saving',
        'draftFailed': 'Draft not synced',
        'draftConflict': 'Draft changed elsewhere',
        'retrySave': 'Retry saving',
      },
    },
  },
});

class _FakeApi extends ChatApi {
  _FakeApi() : super(Dio());

  final replies = <String, List<List<String>>>{};

  /// Set to hold the POST open, so a test can rebuild the card mid-flight.
  Completer<void>? gate;
  int? failure;
  int? draftFailure;
  final skipped = <String>[];
  final savedDrafts = <(String, List<QuestionDraftAnswer>, int)>[];

  DioException _failure(int code) => DioException(
    requestOptions: RequestOptions(path: '/question'),
    response: Response(
      requestOptions: RequestOptions(path: '/question'),
      statusCode: code,
    ),
  );

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers,
  ) async {
    if (failure != null) throw _failure(failure!);
    replies[requestId] = answers;
    await gate?.future;
  }

  @override
  Future<void> rejectQuestion(String requestId) async {
    skipped.add(requestId);
    await gate?.future;
  }

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];

  @override
  Future<QuestionRequest> getQuestion(String id) async => QuestionRequest(
    id: id,
    sessionId: 's1',
    questions: _request(id).questions,
    draftRevision: 8,
  );

  @override
  Future<QuestionRequest> saveQuestionDraft(
    String id,
    List<QuestionDraftAnswer> draft,
    int revision,
  ) async {
    if (draftFailure != null) throw _failure(draftFailure!);
    savedDrafts.add((id, draft, revision));
    return QuestionRequest(
      id: id,
      sessionId: 's1',
      questions: _request(id).questions,
      draft: draft,
      draftRevision: revision + 1,
    );
  }
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

QuestionRequest _pagedRequest(
  String id, {
  List<QuestionDraftAnswer> draft = const [],
}) => QuestionRequest(
  id: id,
  sessionId: 's1',
  draft: draft,
  questions: const [
    QuestionItem(
      question: 'Duration',
      options: [
        QuestionOption(label: '30s'),
        QuestionOption(label: '60s'),
      ],
    ),
    QuestionItem(
      question: 'Extras',
      multiple: true,
      options: [
        QuestionOption(label: 'Captions'),
        QuestionOption(label: 'Music'),
      ],
    ),
    QuestionItem(question: 'Format'),
  ],
);

Future<void> _answerPages(WidgetTester tester) async {
  await tester.tap(find.text('30s'));
  await tester.pump();
  await tester.tap(find.text('Captions'));
  await tester.tap(find.text('Next'));
  await tester.pump();
  await tester.enterText(find.byType(TextField), 'Square');
  await tester.pump();
}

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
  Map<String, Object> preferences = const {},
}) async {
  SharedPreferences.setMockInitialValues(preferences);
  final prefs = await SharedPreferences.getInstance();
  final api = _FakeApi();
  final container = ProviderContainer(
    overrides: [
      i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
      chatApiProvider.overrideWithValue(api),
      apiDioProvider.overrideWithValue(Dio()),
      prefsProvider.overrideWithValue(prefs),
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

  test(
    'question API persists versioned drafts and ordered answer arrays',
    () async {
      final requests = <RequestOptions>[];
      final dio = Dio();
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            requests.add(options);
            handler.resolve(
              Response<Map<String, dynamic>>(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'id': 'q1',
                  'session_id': 's1',
                  'generation': 3,
                  'status': 'pending',
                  'questions': [
                    {'question': 'Duration?'},
                  ],
                  'draft_revision': 5,
                  'draft': [
                    {
                      'selected': <String>[],
                      'custom': '30s',
                      'use_custom': true,
                    },
                  ],
                },
              ),
            );
          },
        ),
      );
      final api = ChatApi(dio);
      final saved = await api.saveQuestionDraft('q1', [
        const QuestionDraftAnswer(custom: '30s', useCustom: true),
      ], 4);
      expect(saved.generation, 3);
      expect(saved.draftRevision, 5);
      expect(saved.draft.single.custom, '30s');
      expect(requests.single.method, 'PUT');
      expect(requests.single.data, {
        'draft': [
          {'selected': <String>[], 'custom': '30s', 'use_custom': true},
        ],
        'revision': 4,
      });
      await api.replyQuestion('q1', [
        ['30s'],
        ['Captions'],
      ]);
      expect(requests.last.data, {
        'answers': [
          ['30s'],
          ['Captions'],
        ],
      });
      expect(sessionStatusFrom('waiting_input'), SessionStatus.waitingInput);
      expect(sessionStatusFrom('queued'), SessionStatus.queued);
      expect(
        const WsEvent('question.cancelled', {'session_id': 's1'}).sessionId,
        's1',
      );
    },
  );

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

  testWidgets('three numbered questions require all answers and submit once', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(390, 844);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final request = QuestionRequest(
      id: 'three',
      sessionId: 's1',
      questions: [
        for (var i = 1; i <= 3; i++)
          QuestionItem(
            question: 'Question $i',
            custom: false,
            options: [QuestionOption(label: 'Answer $i')],
          ),
      ],
    );
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [request],
    );
    expect(find.text('1/3'), findsOneWidget);
    expect(find.text('Question 1'), findsOneWidget);
    expect(find.text('Question 3'), findsNothing);
    expect(_canSubmit(tester), isFalse);
    await tester.tap(find.text('Answer 1'));
    await tester.pump();
    expect(find.text('2/3'), findsOneWidget);
    expect(find.text('Question 1'), findsNothing);
    expect(_canSubmit(tester), isFalse);
    await tester.tap(find.text('Answer 2'));
    await tester.pump();
    expect(find.text('3/3'), findsOneWidget);
    await tester.tap(find.text('Answer 3'));
    await tester.pump();
    expect(_canSubmit(tester), isTrue);
    expect(api.replies, isEmpty);
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(api.replies['three'], [
      ['Answer 1'],
      ['Answer 2'],
      ['Answer 3'],
    ]);
  });

  testWidgets(
    'server draft restores and switching to an option preserves text without submitting it',
    (tester) async {
      final request = QuestionRequest(
        id: 'saved',
        sessionId: 's1',
        questions: _request('saved').questions,
        draftRevision: 7,
        draft: const [QuestionDraftAnswer(custom: 'my draft', useCustom: true)],
      );
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [request],
      );
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'my draft',
      );
      await tester.tap(find.text('Option A'));
      await tester.pump();
      expect(container.read(questionDraftProvider)['saved']!.custom, [
        'my draft',
      ]);
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(api.replies['saved'], [
        ['Option A'],
      ]);
    },
  );

  testWidgets('failed reply preserves selections for retry', (tester) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('retry')],
    );
    api.failure = 500;
    await tester.tap(find.text('Option A'));
    await tester.pump();
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(find.text('Submission failed; answers preserved'), findsOneWidget);
    expect(_picked(tester), isTrue);
    expect(_canSubmit(tester), isTrue);
    api.failure = null;
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('410 removes card and late asked events cannot restore it', (
    tester,
  ) async {
    final (api, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('gone')],
    );
    api.failure = 410;
    await tester.tap(find.text('Option A'));
    await tester.pump();
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    container.read(pendingProvider.notifier).addQuestion(_request('gone'));
    container.read(pendingProvider.notifier).seed([], [_request('gone')]);
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
    expect(container.read(questionDraftProvider), isEmpty);
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('skip holds all controls disabled across a rebuild', (
    tester,
  ) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 1),
      questions: [_request('skip')],
    );
    api.gate = Completer<void>();
    await tester.tap(find.text('Skip'));
    await tester.pump();
    tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
    await tester.pump();
    expect(
      tester
          .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Option A'))
          .onSelected,
      isNull,
    );
    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isFalse);
    expect(api.skipped, ['skip']);
    api.gate!.complete();
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
  });

  testWidgets(
    'locally cached unsent draft survives app restart and syncs its revision',
    (tester) async {
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_request('cached')],
        preferences: {
          'openbox:question-draft:anonymous:cached': jsonEncode({
            'revision': 0,
            'draft': [
              {
                'selected': <String>[],
                'custom': 'offline text',
                'use_custom': true,
              },
            ],
          }),
          'openbox:question-draft:another-user:cached': jsonEncode({
            'revision': 0,
            'draft': [
              {
                'selected': ['Option B'],
                'custom': 'private',
                'use_custom': true,
              },
            ],
          }),
        },
      );
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'offline text',
      );
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();
      expect(api.savedDrafts.single.$3, 0);
      expect(api.savedDrafts.single.$2.single.custom, 'offline text');
      expect(container.read(questionDraftProvider)['cached']!.revision, 1);
      expect(api.replies, isEmpty);
    },
  );

  testWidgets(
    'reconnect updates a clean draft without replacing unsaved local edits',
    (tester) async {
      final (_, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_request('live')],
      );
      QuestionRequest remote(int rev, String value) => QuestionRequest(
        id: 'live',
        sessionId: 's1',
        questions: _request('live').questions,
        draftRevision: rev,
        draft: [QuestionDraftAnswer(custom: value, useCustom: true)],
      );
      container
          .read(pendingProvider.notifier)
          .addQuestion(remote(1, 'from web'));
      await tester.pump();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'from web',
      );
      await tester.enterText(find.byType(TextField), 'local edit');
      container
          .read(pendingProvider.notifier)
          .addQuestion(remote(2, 'another web edit'));
      await tester.pump();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'local edit',
      );
      expect(container.read(questionDraftProvider)['live']!.revision, 1);
      container.read(pendingProvider.notifier).removeQuestion('live');
      await tester.pump();
    },
  );

  testWidgets('switching accounts never restores the previous users draft', (
    tester,
  ) async {
    final (_, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('private')],
    );
    await tester.enterText(find.byType(TextField), 'private old-user answer');
    container
        .read(authProvider.notifier)
        .setAuth('test-token', const AuthUser(id: 'u2', username: 'user2'));
    await tester.pump();
    expect(container.read(pendingProvider).questionsOf('s1'), isEmpty);
    container.read(pendingProvider.notifier).addQuestion(_request('private'));
    await tester.pump();
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      isEmpty,
    );
    expect(container.read(questionDraftProvider)['private']!.answers, [
      <String>[],
    ]);
  });

  testWidgets('draft conflict preserves edits and retry uses fresh revision', (
    tester,
  ) async {
    final (api, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('conflict')],
    );
    api.draftFailure = 409;
    await tester.enterText(find.byType(TextField), 'keep this draft');
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pumpAndSettle();
    expect(find.text('Draft changed elsewhere'), findsOneWidget);
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      'keep this draft',
    );
    api.draftFailure = null;
    await tester.tap(find.text('Retry saving'));
    await tester.pumpAndSettle();
    expect(api.savedDrafts.single.$3, 8);
    expect(api.savedDrafts.single.$2.single.custom, 'keep this draft');
    expect(container.read(questionDraftProvider)['conflict']!.revision, 9);
    expect(api.replies, isEmpty);
  });

  registerQuestionPagerTests();
}
