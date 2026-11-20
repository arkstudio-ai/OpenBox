import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/features/chat/utils/light_tool_names.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/utils/light_steps.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/light.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _FakeApi extends ChatApi {
  _FakeApi() : super(Dio());

  final replies = <List<List<String>>>[];

  /// When set, a reply waits here: the card is mid-send meanwhile.
  Completer<void>? gate;

  @override
  Future<void> replyQuestion(
    String requestId,
    List<List<String>> answers, {
    List<List<String>>? attachments,
  }) async {
    replies.add(answers);
    await gate?.future;
  }

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];
}

class _FakeLightApi extends LightApi {
  _FakeLightApi() : super(Dio());

  final previews = <String>[];
  bool gone = false;

  @override
  Future<LightExamplePreview> examplePreview(String exampleId) async {
    previews.add(exampleId);
    if (gone) {
      throw DioException(
        requestOptions: RequestOptions(path: '/preview'),
        response: Response(
          requestOptions: RequestOptions(path: '/preview'),
          statusCode: 404,
        ),
      );
    }
    return LightExamplePreview(
      exampleId: exampleId,
      title: 'fresh',
      mime: exampleId == 'lex_b' ? 'video/mp4' : 'image/png',
      mediaUrl: 'https://signed.example/$exampleId/fresh',
      posterUrl: 'https://signed.example/$exampleId/fresh.jpg',
    );
  }
}

Map<String, dynamic> _example(
  String id,
  String title, {
  String mime = 'image/png',
  bool lastPick = false,
}) => {
  'id': id,
  'role': 'choose',
  'recommended': false,
  'example_id': id,
  'title': title,
  'mime': mime,
  'media_url':
      'https://signed.example/$id/final.${mime.startsWith('video/') ? 'mp4' : 'png'}',
  'poster_url': 'https://signed.example/$id/poster.jpg',
  'last_pick': lastPick,
  'scene_label': '团购',
};

const _decline = '都不太像，直接做';

/// The card as backend `tool/example_pick.py` files it.
QuestionRequest _card({
  int count = 3,
  String kind = 'light_example_pick',
  List<Map<String, dynamic>>? extras,
  bool withExtras = true,
}) {
  final all = [
    _example('lex_a', '红底团购海报', lastPick: true),
    _example('lex_b', '探店快剪', mime: 'video/mp4'),
    _example('lex_c', '一个标题很长很长很长很长很长很长很长很长的黑金菜单样例'),
  ].take(count).toList();
  return QuestionRequest(
    id: 'q1',
    sessionId: 's1',
    questions: [
      QuestionItem(
        header: '挑样例',
        question: '这几个是同行做过的成品，哪个更像你想要的？',
        options: [
          for (final example in all)
            QuestionOption(
              label: example['title'] as String,
              description: example['last_pick'] == true ? '上次选的' : '',
            ),
          const QuestionOption(label: _decline, description: ''),
        ],
        detail: {
          'kind': kind,
          'present': 'inline',
          'light': {'step_id': 'example'},
          if (withExtras)
            'options':
                extras ??
                [
                  ...all,
                  {'id': 'none', 'role': 'cancel', 'recommended': false},
                ],
        },
      ),
    ],
  );
}

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

Future<(_FakeApi, _FakeLightApi)> _mount(
  WidgetTester tester,
  QuestionRequest request, {
  String lang = 'zh-CN',
  _FakeLightApi? light,
}) async {
  // The narrowest phone the App supports: three tiles must fit.
  tester.view.physicalSize = const Size(360, 800);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  SharedPreferences.setMockInitialValues({'bossip:lang': lang});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final api = _FakeApi();
  final lightApi = light ?? _FakeLightApi();
  final container = ProviderContainer(
    overrides: [
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      chatApiProvider.overrideWithValue(api),
      lightApiProvider.overrideWithValue(lightApi),
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
  return (api, lightApi);
}

FilledButton _use(WidgetTester tester, String id) =>
    tester.widget<FilledButton>(find.byKey(Key('light-example-use-$id')));

void main() {
  testWidgets('the card shows its examples side by side, from detail', (
    tester,
  ) async {
    await _mount(tester, _card());
    expect(find.byKey(const Key('light-example-card')), findsOneWidget);
    expect(find.byKey(const Key('light-question-card')), findsNothing);
    expect(find.byType(ChoiceChip), findsNothing);
    expect(find.text('挑样例'), findsOneWidget);
    expect(find.text('这几个是同行做过的成品，哪个更像你想要的？'), findsOneWidget);
    expect(find.text('点开看看'), findsOneWidget);
    expect(find.text('红底团购海报'), findsOneWidget);
    expect(find.text('探店快剪'), findsOneWidget);
    expect(find.text('就照这个做'), findsNWidgets(3));
    expect(find.text(_decline), findsOneWidget);

    // Equal-width tiles on one line, the buttons on one line under them.
    final tiles = [
      for (final id in ['lex_a', 'lex_b', 'lex_c'])
        tester.getRect(find.byKey(Key('light-example-tile-$id'))),
    ];
    expect(tiles.map((r) => r.width.round()).toSet(), hasLength(1));
    expect(tiles.map((r) => r.top).toSet(), hasLength(1));
    expect(tiles[0].height, greaterThan(tiles[0].width));
    expect(tiles[0].right, lessThan(tiles[1].left));
    expect(tiles[1].right, lessThan(tiles[2].left));
    expect({
      for (final id in ['lex_a', 'lex_b', 'lex_c'])
        tester.getRect(find.byKey(Key('light-example-use-$id'))).top,
    }, hasLength(1));

    // 上次选的 only on the remembered one; the play mark only on the clip.
    expect(find.text('上次选的'), findsOneWidget);
    expect(find.byKey(const Key('light-example-last-lex_a')), findsOneWidget);
    expect(find.byKey(const Key('light-example-play-lex_b')), findsOneWidget);
    expect(find.byKey(const Key('light-example-play-lex_a')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('就照这个做 answers with that example\'s label', (tester) async {
    final (api, _) = await _mount(tester, _card());
    await tester.tap(find.byKey(const Key('light-example-use-lex_b')));
    await tester.pumpAndSettle();
    expect(api.replies, [
      [
        ['探店快剪'],
      ],
    ]);
    expect(find.byKey(const Key('light-example-card')), findsNothing);
  });

  testWidgets('the bottom button answers with the cancel label', (
    tester,
  ) async {
    final (api, _) = await _mount(tester, _card(count: 1));
    expect(find.text('就照这个做'), findsOneWidget);
    await tester.tap(find.byKey(const Key('light-example-decline-none')));
    await tester.pumpAndSettle();
    expect(api.replies, [
      [
        [_decline],
      ],
    ]);
    expect(find.byKey(const Key('light-example-card')), findsNothing);
  });

  testWidgets('the buttons are off while the reply is sent: one answer', (
    tester,
  ) async {
    final (api, _) = await _mount(tester, _card());
    api.gate = Completer<void>();
    await tester.tap(find.byKey(const Key('light-example-use-lex_a')));
    await tester.pump();
    expect(_use(tester, 'lex_a').onPressed, isNull);
    expect(_use(tester, 'lex_c').onPressed, isNull);
    expect(
      tester
          .widget<TextButton>(
            find.byKey(const Key('light-example-decline-none')),
          )
          .onPressed,
      isNull,
    );
    await tester.tap(
      find.byKey(const Key('light-example-use-lex_c')),
      warnIfMissed: false,
    );
    await tester.pump();
    api.gate!.complete();
    await tester.pumpAndSettle();
    expect(api.replies, [
      [
        ['红底团购海报'],
      ],
    ]);
  });

  testWidgets('a still that will not load asks for fresh links once', (
    tester,
  ) async {
    // Widget tests answer every image request with HTTP 400: an expired link.
    final (api, light) = await _mount(tester, _card());
    await tester.pump(const Duration(seconds: 1));
    await tester.pumpAndSettle();
    expect(light.previews..sort(), ['lex_a', 'lex_b', 'lex_c']);
    // The fresh links fail here too; the neutral tile stays and still answers.
    expect(find.byIcon(Icons.image_outlined), findsNWidgets(2));
    expect(find.byIcon(Icons.movie_outlined), findsOneWidget);
    await tester.tap(find.byKey(const Key('light-example-use-lex_a')));
    await tester.pumpAndSettle();
    expect(api.replies.single, [
      ['红底团购海报'],
    ]);
    expect(light.previews, hasLength(3));
  });

  testWidgets('an example that is gone keeps a tile that can be chosen', (
    tester,
  ) async {
    final (api, light) = await _mount(
      tester,
      _card(count: 2),
      light: _FakeLightApi()..gone = true,
    );
    await tester.pumpAndSettle();
    expect(light.previews..sort(), ['lex_a', 'lex_b']);
    expect(find.byKey(const Key('light-example-tile-lex_a')), findsOneWidget);
    await tester.tap(find.byKey(const Key('light-example-use-lex_a')));
    await tester.pumpAndSettle();
    expect(api.replies, hasLength(1));
    expect(tester.takeException(), isNull);
  });

  testWidgets('a picture tile opens full screen, zoomable', (tester) async {
    await _mount(tester, _card());
    await tester.tap(find.byKey(const Key('light-example-tile-lex_a')));
    await tester.pumpAndSettle();
    expect(find.byType(InteractiveViewer), findsOneWidget);
    await tester.tap(find.byKey(const Key('media-viewer-close')));
    await tester.pumpAndSettle();
    expect(find.byType(InteractiveViewer), findsNothing);
    expect(find.byKey(const Key('light-example-card')), findsOneWidget);
  });

  testWidgets('three tiles fit a 360 dp phone in the longer languages', (
    tester,
  ) async {
    for (final (lang, use) in [
      ('en-US', 'Use this look'),
      ('vi-VN', 'Làm theo mẫu này'),
    ]) {
      await _mount(tester, _card(), lang: lang);
      expect(find.text(use), findsNWidgets(3), reason: lang);
      expect(tester.takeException(), isNull, reason: lang);
      await tester.pumpWidget(const SizedBox.shrink());
    }
  });

  testWidgets('an unknown kind falls back to the Light chips', (tester) async {
    final (api, _) = await _mount(tester, _card(kind: 'light_future_card'));
    expect(find.byKey(const Key('light-example-card')), findsNothing);
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
    await tester.tap(find.text('探店快剪'));
    await tester.pumpAndSettle();
    expect(api.replies.single, [
      ['探店快剪'],
    ]);
  });

  testWidgets('a card without its option extras falls back too', (
    tester,
  ) async {
    await _mount(tester, _card(withExtras: false));
    expect(find.byKey(const Key('light-example-card')), findsNothing);
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
    expect(find.text(_decline), findsOneWidget);
  });

  testWidgets('an example with no id to show falls back as well', (
    tester,
  ) async {
    await _mount(
      tester,
      _card(
        count: 1,
        extras: [
          {'id': 'lex_a', 'role': 'choose'},
          {'id': 'none', 'role': 'cancel'},
        ],
      ),
    );
    expect(find.byKey(const Key('light-example-card')), findsNothing);
    expect(find.byKey(const Key('light-question-card')), findsOneWidget);
  });

  test('the card lights the entry\'s example step', () {
    const steps = [
      LightStep(id: 'type', label: '选类型'),
      LightStep(id: 'example', label: '挑样例'),
      LightStep(id: 'content', label: '填内容'),
      LightStep(id: 'generate', label: '出图'),
    ];
    final step = lightStepIdOf(_card());
    expect(step, 'example');
    expect(lightStepIndex(steps, step), 1);
  });

  test('the preview reads the endpoint\'s fields', () {
    final preview = LightExamplePreview.fromJson({
      'example_id': 'lex_b',
      'title': '探店快剪',
      'mime': 'video/mp4',
      'media_url': 'https://signed.example/lex_b/final.mp4',
      'poster_url': 'https://signed.example/lex_b/poster.jpg',
    })!;
    expect(preview.isVideo, isTrue);
    expect(preview.mediaUrl, endsWith('final.mp4'));
    expect(preview.posterUrl, endsWith('poster.jpg'));
    expect(LightExamplePreview.fromJson({'title': 'x'}), isNull);
    expect(
      LightExamplePreview.fromJson({
        'example_id': 'a',
        'poster_url': '',
      })!.posterUrl,
      isNull,
    );
  });

  test('the example_pick tool has its own plain-words name', () {
    const part = ToolPart(
      id: 'p1',
      tool: 'example_pick',
      status: ToolStatus.running,
    );
    expect(lightToolKey(part), 'example_pick');
  });
}
