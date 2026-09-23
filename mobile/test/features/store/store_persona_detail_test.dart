import 'dart:convert';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/store_persona_detail.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _items = [
  PersonaBundleItem(
    memoryId: 'm1',
    type: 'IDENTITY',
    label: '店是谁',
    summary: '南宁·泽岚鲜果，社区鲜果切与果汁，人均 25',
  ),
  PersonaBundleItem(
    memoryId: 'm2',
    type: 'VOICE',
    label: '表达风格',
    summary: '亲切、直接、带本地口语',
  ),
];

QuestionRequest _request() => QuestionRequest(
  id: 'q1',
  sessionId: 's1',
  questions: [
    QuestionItem(
      question: '我对你的店的理解，对吗？',
      options: const [
        QuestionOption(label: '确认'),
        QuestionOption(label: '稍后'),
      ],
      detail: {
        'kind': storePersonaBundleKind,
        'items': [
          for (final item in _items)
            {
              'memory_id': item.memoryId,
              'type': item.type,
              'label': item.label,
              'summary': item.summary,
            },
        ],
      },
    ),
  ],
);

class _FakeApi extends ChatApi {
  _FakeApi() : super(Dio());

  final replies = <String, List<List<String>>>{};

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers,
  ) async {
    replies[requestId] = answers;
  }

  @override
  Future<QuestionRequest> saveQuestionDraft(
    String id,
    List<QuestionDraftAnswer> draft,
    int revision,
  ) async => QuestionRequest(
    id: id,
    sessionId: 's1',
    questions: _request().questions,
    draft: draft,
    draftRevision: revision + 1,
  );

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];
}

void main() {
  group('personaAnswer', () {
    test('later and an untouched confirm keep the picked label', () {
      expect(
        personaAnswer(picked: ['稍后'], items: _items, edits: {'m1': 'x'}),
        isNull,
      );
      expect(
        personaAnswer(
          picked: ['确认'],
          items: _items,
          edits: {'m1': '${_items.first.summary} '},
        ),
        isNull,
      );
    });

    test('confirm with edits carries only the changed ids as JSON', () {
      final answer = personaAnswer(
        picked: ['确认'],
        items: _items,
        edits: {'m1': _items.first.summary, 'm2': '亲切、直接，不用网络梗'},
      );
      expect(answer, hasLength(1));
      expect(jsonDecode(answer!.single), {
        'confirm': true,
        'items': {'m2': '亲切、直接，不用网络梗'},
      });
      expect(isPersonaEditAnswer(answer.single), isTrue);
      expect(isPersonaEditAnswer('确认'), isFalse);
      expect(isPersonaEditAnswer('{"confirm":false}'), isFalse);
    });
  });

  testWidgets('the dock edits a field and submits the JSON answer', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    final api = _FakeApi();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          apiDioProvider.overrideWithValue(Dio()),
          chatApiProvider.overrideWithValue(api),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: SingleChildScrollView(child: QuestionDock(request: _request())),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    // One field per item, seeded with the summary; no generic answer box.
    expect(find.byType(TextField), findsNWidgets(2));
    expect(find.text(_items.first.summary), findsOneWidget);
    expect(find.text('店是谁'), findsOneWidget);
    expect(find.text('写下你的回答'), findsNothing);

    await tester.enterText(
      find.byKey(const Key('persona-m2')),
      '亲切、直接，不用网络梗',
    );
    await tester.pump();
    expect(find.text('已修改'), findsOneWidget);
    await tester.tap(find.widgetWithText(ChoiceChip, '确认'));
    await tester.pump(const Duration(milliseconds: 500));
    await tester.tap(find.byType(FilledButton));
    await tester.pump(const Duration(milliseconds: 500));

    final answer = api.replies['q1']!.single.single;
    expect(jsonDecode(answer), {
      'confirm': true,
      'items': {'m2': '亲切、直接，不用网络梗'},
    });
  });

  testWidgets('a plain confirm and a later both send the bare label', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    for (final label in ['确认', '稍后']) {
      final api = _FakeApi();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            prefsProvider.overrideWithValue(prefs),
            apiDioProvider.overrideWithValue(Dio()),
            chatApiProvider.overrideWithValue(api),
            i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          ],
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: Scaffold(
              body: SingleChildScrollView(
                child: QuestionDock(request: _request()),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      if (label == '稍后') {
        // Edits are ignored when the person defers.
        await tester.enterText(find.byKey(const Key('persona-m1')), '改了');
        await tester.pump();
      }
      await tester.tap(find.widgetWithText(ChoiceChip, label));
      await tester.pump(const Duration(milliseconds: 500));
      await tester.tap(find.byType(FilledButton));
      await tester.pump(const Duration(milliseconds: 500));
      expect(api.replies['q1'], [
        [label],
      ]);
    }
  });
}
