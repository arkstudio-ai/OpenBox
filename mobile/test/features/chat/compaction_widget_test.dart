import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_turn.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/features/chat/widgets/markdown_view.dart';
import 'package:bossip_mobile/features/chat/widgets/traces/compaction_trace.dart';
import 'package:bossip_mobile/features/chat/widgets/traces/trace_shell.dart';
import 'package:bossip_mobile/features/chat/widgets/typing_row.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'compaction_fixtures.dart' show compactRequest, compactSummary, answer;
import 'suggestion_fixtures.dart' show SuggestionFixture;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  late I18nBundle bundle;
  late SuggestionFixture fixture;
  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
    bundle = await I18nBundle.load();
    fixture = await SuggestionFixture.create();
  });
  tearDown(() => fixture.dispose());

  Widget page(List<ChatMessage> messages, {bool streaming = false}) =>
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: SingleChildScrollView(
              child: SizedBox(
                width: 320,
                child: AssistantTurn(
                  turn: buildChatRows(messages).single as AssistantTurnData,
                  sessionId: 's1',
                  streaming: streaming,
                  onReview: () {},
                  onRegenerate: (_) {},
                  onDismiss: (_) {},
                ),
              ),
            ),
          ),
        ),
      );

  testWidgets('summary stays folded while optimization runs and loop resumes', (
    tester,
  ) async {
    await tester.pumpWidget(
      page([compactRequest(), compactSummary()], streaming: true),
    );
    expect(find.text('上下文优化'), findsOneWidget);
    expect(find.byType(MarkdownView), findsNothing);
    expect(find.byType(ThinkingRow), findsNothing);
    expect(tester.widget<TraceShell>(find.byType(TraceShell)).active, isTrue);
    await tester.pumpWidget(
      page([
        compactRequest(),
        compactSummary(finish: 'stop'),
        answer(id: 'm04'),
      ]),
    );
    await tester.pump();
    expect(find.byType(CompactionTrace), findsOneWidget);
    expect(
      tester
          .widgetList<MarkdownView>(find.byType(MarkdownView))
          .map((w) => w.text),
      ['Answer'],
    );
    await tester.tap(find.text('上下文优化'));
    await tester.pumpAndSettle();
    expect(
      tester
          .widgetList<MarkdownView>(find.byType(MarkdownView))
          .any((w) => w.text.contains('## Goal')),
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'failed and interrupted summaries stay inside process with original answer',
    (tester) async {
      for (final finish in ['error', 'aborted']) {
        await tester.pumpWidget(
          page([answer(), compactRequest(), compactSummary(finish: finish)]),
        );
        await tester.pump();
        expect(find.byType(CompactionTrace), findsOneWidget);
        expect(
          tester
              .widgetList<MarkdownView>(find.byType(MarkdownView))
              .map((w) => w.text),
          ['Answer'],
        );
        expect(find.textContaining('原始上下文已保留'), findsOneWidget);
        expect(tester.takeException(), isNull);
      }
    },
  );

  testWidgets('internal compression does not force history readers to scroll', (
    tester,
  ) async {
    fixture.api.messages = [
      ChatMessage.fromJson({
        'id': 'm00',
        'session_id': 's1',
        'role': 'user',
        'parts': [
          {'id': 'question', 'type': 'text', 'text': 'Continue the task'},
        ],
      }),
      answer(),
    ];
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    expect(
      tester.widget<ChatFlow>(find.byType(ChatFlow)).forceScrollToken,
      'm00',
    );
    final store = fixture.container.read(chatStreamProvider.notifier);
    store.addMessage('s1', compactRequest());
    store.addMessage('s1', compactSummary(finish: 'stop'));
    await tester.pump();
    expect(
      tester.widget<ChatFlow>(find.byType(ChatFlow)).forceScrollToken,
      'm00',
    );
    expect(find.byType(CompactionTrace), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
