import 'package:bossip_mobile/features/chat/widgets/assistant_requests.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

final _binding = {
  'assistant_session_id': 'main',
  'workspace_id': scope.workspaceId,
  'request_revision': List.filled(64, 'a').join(),
  'options_hash': List.filled(64, 'b').join(),
};

/// One question and one approval, as the assistant page lists them.
class DeckApi extends TestApi {
  @override
  Future<Map<String, dynamic>> requests(String kind, {String? cursor}) async =>
      {
        'items': [
          {
            'id': kind,
            'session_id': 'execution',
            'assistant': _binding,
            'task_title': kind == 'question' ? 'Campaign plan' : 'Site deploy',
            'project_name': 'Project Orchard',
            if (kind == 'permission') ...{
              'tool': 'shell',
              'input': {'command': 'test-command'},
              'always': ['test-command *'],
            } else
              'questions': [
                {
                  'question': 'Which audience?',
                  'options': [
                    {'label': 'Existing customers'},
                  ],
                  'custom': false,
                },
              ],
          },
        ],
        'receipts': <Map<String, dynamic>>[],
      };
}

void main() {
  testWidgets('the call deck shows one card at a time with previous/next', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({});
    final f = (await tester.runAsync(() async {
      final prefs = await SharedPreferences.getInstance();
      final bundle = await I18nBundle.load();
      return Fixture(
        prefs,
        server: DeckApi(),
        i18n: I18nState(language: 'zh-CN', bundle: bundle),
      );
    }))!;
    addTearDown(f.close);
    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: f.container,
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: SizedBox(
              height: 420,
              child: AssistantRequests(scope: scope, deck: true),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('有 2 件事需要你处理'), findsOneWidget);
    expect(find.text('第 1 / 2 张'), findsOneWidget);
    expect(find.text('Which audience?'), findsOneWidget);
    expect(find.textContaining('test-command'), findsNothing);
    final previous = tester.widget<IconButton>(
      find.byKey(const Key('requests-deck-previous')),
    );
    expect(previous.onPressed, isNull);

    await tester.tap(find.byKey(const Key('requests-deck-next')));
    await tester.pumpAndSettle();
    expect(find.text('第 2 / 2 张'), findsOneWidget);
    expect(find.textContaining('test-command'), findsOneWidget);
    final next = tester.widget<IconButton>(
      find.byKey(const Key('requests-deck-next')),
    );
    expect(next.onPressed, isNull);

    await tester.tap(find.byKey(const Key('requests-deck-previous')));
    await tester.pumpAndSettle();
    expect(find.text('第 1 / 2 张'), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(milliseconds: 1));
  });
}
