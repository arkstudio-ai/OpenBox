import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/cards/card_popup_host.dart';
import 'package:bossip_mobile/features/chat/cards/server_card.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
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
  final rejected = <String>[];

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers, {
    List<List<String>>? attachments,
  }) async => replies[requestId] = answers;

  @override
  Future<void> rejectQuestion(String requestId) async =>
      rejected.add(requestId);

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];

  @override
  Future<List<PermissionRequest>> listPermissions() async => [];
}

/// What backend `tool/video_model_card.py` files, as `question.asked` carries it.
QuestionRequest _switchCard(String id, {String present = 'sheet'}) =>
    QuestionRequest.fromJson({
      'id': id,
      'session_id': 'task-1',
      'created_at': '2026-11-12T10:00:00Z',
      'questions': [
        {
          'header': '换视频模型',
          'question': '要把这个对话的视频模型换成「灵活档 · 768p」吗？',
          'custom': false,
          'options': [
            {'label': '换成灵活档 · 768p', 'description': '0.25 积分/秒'},
            {'label': '保持标准档 · 720p', 'description': '1.40 积分/秒'},
          ],
          'detail': {
            'kind': 'video_model_switch',
            'present': present,
            'options': [
              {
                'id': 'switch',
                'role': 'choose',
                'recommended': false,
                'video': {'description': '快速出片、成本最低'},
              },
              {'id': 'keep', 'role': 'cancel', 'recommended': true},
            ],
          },
        },
      ],
    });

Future<(_FakeApi, ProviderContainer)> _mount(
  WidgetTester tester,
  List<QuestionRequest> questions,
) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final api = _FakeApi();
  final container = ProviderContainer(
    overrides: [
      i18nProvider.overrideWith(
        () => I18nController(
          I18nBundle({
            'en-US': {
              'chat': {
                'card': {'later': 'Later', 'recommended': 'Recommended'},
              },
            },
          }),
          prefs,
        ),
      ),
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
        builder: (context, child) => CardPopupHost(child: child!),
        home: const Scaffold(body: Text('page')),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return (api, container);
}

void main() {
  test('a question without a server-card detail is not a server card', () {
    final plain = QuestionRequest.fromJson({
      'id': 'q',
      'session_id': 's',
      'questions': [
        {
          'question': 'Pick',
          'options': [
            {'label': 'A'},
            {'label': 'B'},
          ],
        },
      ],
    });
    expect(ServerCard.of(plain), isNull);
    final card = ServerCard.of(_switchCard('c'))!;
    expect(card.kind, 'video_model_switch');
    expect(card.options.map((o) => o.id), ['switch', 'keep']);
    expect(card.options.last.cancel, isTrue);
    expect(card.options.last.recommended, isTrue);
  });

  testWidgets('a sheet card pops up and answers with the option label', (
    tester,
  ) async {
    final (api, container) = await _mount(tester, [_switchCard('c1')]);
    expect(find.byKey(const Key('card-popup-sheet')), findsOneWidget);
    expect(find.text('Recommended'), findsOneWidget);
    expect(find.text('快速出片、成本最低'), findsOneWidget);

    await tester.tap(find.byKey(const Key('card-option-switch')));
    await tester.pumpAndSettle();

    expect(api.replies['c1'], [
      ['换成灵活档 · 768p'],
    ]);
    expect(container.read(pendingProvider).questionsOf('task-1'), isEmpty);
    expect(find.byKey(const Key('card-popup-sheet')), findsNothing);
  });

  testWidgets('later folds the popup without answering', (tester) async {
    final (api, container) = await _mount(tester, [_switchCard('c1')]);
    await tester.tap(find.byKey(const Key('card-popup-later')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('card-popup-sheet')), findsNothing);
    expect(api.replies, isEmpty);
    expect(api.rejected, isEmpty);
    expect(container.read(pendingProvider).questionsOf('task-1'), hasLength(1));
  });

  testWidgets('an inline card or one answered elsewhere does not pop up', (
    tester,
  ) async {
    final (_, container) = await _mount(tester, [
      _switchCard('c1', present: 'inline'),
    ]);
    expect(find.byKey(const Key('card-popup-sheet')), findsNothing);

    container.read(pendingProvider.notifier).addQuestion(_switchCard('c2'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('card-popup-sheet')), findsOneWidget);

    container.read(pendingProvider.notifier).removeQuestion('c2');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('card-popup-sheet')), findsNothing);
  });
}
