import 'package:bossip_mobile/features/chat/utils/tool_map.dart';
import 'package:bossip_mobile/features/chat/widgets/traces/tool_output.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'question': {'unanswered': 'Unanswered', 'waiting': 'Waiting for you'},
      'takeover': {
        'record': 'Taken over: {{reason}} · {{host}}',
        'reason': {'captcha_slider': 'slider captcha', 'other': 'human check'},
      },
    },
  },
});

const _takeover = {
  'kind': 'desktop_takeover',
  'reason': 'captcha_slider',
  'url': 'https://login.taobao.com/member/login.jhtml',
  'host': 'login.taobao.com',
  'browser': 'local',
};

Future<void> _pump(WidgetTester tester, ToolPart part) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(body: ToolOutput(part: part)),
      ),
    ),
  );
  // pump, not pumpAndSettle: the i18n controller keeps a listener alive.
  await tester.pump();
  await tester.pump();
}

void main() {
  test('a desktop takeover reads back as the question it filed', () {
    expect(resolveToolLayout('desktop_takeover'), 'question');
    expect(resolveToolLayout('question'), 'question');
    expect(resolveToolLayout('mcp_douyin_search'), 'generic');
  });

  testWidgets('the takeover record says what blocked the agent and where', (
    tester,
  ) async {
    await _pump(
      tester,
      const ToolPart(
        id: 'p1',
        tool: 'desktop_takeover',
        status: ToolStatus.completed,
        metadata: {
          'questions': ['Finished the check?'],
          'answers': [
            ['Done, continue'],
          ],
          'takeover': _takeover,
        },
      ),
    );

    expect(
      find.text('Taken over: slider captcha · login.taobao.com'),
      findsOneWidget,
    );
    expect(find.text('Finished the check?'), findsOneWidget);
    expect(find.text('Done, continue'), findsOneWidget);
  });

  testWidgets('an ordinary question has no takeover record', (tester) async {
    await _pump(
      tester,
      const ToolPart(
        id: 'p2',
        tool: 'question',
        status: ToolStatus.completed,
        metadata: {
          'questions': ['Cache?'],
          'answers': [
            ['Redis'],
          ],
        },
      ),
    );

    expect(find.text('Redis'), findsOneWidget);
    expect(find.textContaining('Taken over'), findsNothing);
  });
}
