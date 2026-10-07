import 'package:bossip_mobile/app/knowledge_routes.dart';
import 'package:bossip_mobile/app/workspace_shell.dart';
import 'package:bossip_mobile/features/memory/knowledge_screen.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'knowledge_fixture.dart';

class _EmptyWorkspace extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData();
}

class _OfflineWsClient extends AgentWsClient {
  _OfflineWsClient() : super(Dio());

  @override
  Future<void> connect() async {}
}

void main() {
  setUpKnowledgeTests();

  for (final (language, entry, hint) in [
    ('zh-CN', '知识库', '记忆、主题与文件'),
    ('en-US', 'Knowledge', 'Memories, topics and files'),
  ]) {
    testWidgets(
      'the drawer offers the knowledge page like every other row ($language)',
      (tester) async {
        tester.view.physicalSize = const Size(390 * 3, 844 * 3);
        tester.view.devicePixelRatio = 3;
        addTearDown(tester.view.reset);
        SharedPreferences.setMockInitialValues({'bossip:lang': language});
        final prefs = await SharedPreferences.getInstance();
        final router = GoRouter(
          initialLocation: '/app',
          routes: [
            GoRoute(
              path: '/app',
              builder: (_, _) => const WorkspaceShell(child: SizedBox()),
            ),
            ...knowledgeRoutes,
          ],
        );
        addTearDown(router.dispose);
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              i18nProvider.overrideWith(
                () => I18nController(knowledgeBundle, prefs),
              ),
              apiDioProvider.overrideWithValue(FakeKnowledgeServer().dio()),
              activeWorkspaceProvider.overrideWith(FakeWorkspace.new),
              workspaceProvider.overrideWith(_EmptyWorkspace.new),
              wsClientProvider.overrideWithValue(_OfflineWsClient()),
            ],
            child: MaterialApp.router(
              routerConfig: router,
              theme: ThemeData(
                extensions: [
                  BossipTokens.resolve(
                    BossipThemeName.default_,
                    Brightness.light,
                  ),
                ],
              ),
            ),
          ),
        );
        await settle(tester);

        await tester.tap(find.byType(DrawerButton));
        await settle(tester);
        final row = find.byKey(const ValueKey('nav-knowledge'));
        expect(row, findsOneWidget);
        expect(
          find.descendant(of: row, matching: find.text(entry)),
          findsOneWidget,
        );
        // Its label only, like every other row: no quieter text beside it.
        expect(find.textContaining(hint), findsNothing);
        expect(
          find.descendant(of: row, matching: find.byType(Text)),
          findsOneWidget,
        );
        // Between the skill centre and the scheduled tasks, as on the web.
        final skills = tester.getTopLeft(find.byIcon(Icons.extension_outlined));
        final cron = tester.getTopLeft(find.byIcon(Icons.schedule).first);
        final here = tester.getTopLeft(row);
        expect(here.dy, greaterThan(skills.dy));
        expect(here.dy, lessThan(cron.dy));

        await tester.tap(row);
        await settle(tester);
        expect(find.byType(KnowledgeScreen), findsOneWidget);
        expect(
          router.routerDelegate.currentConfiguration.last.matchedLocation,
          '/app/wiki',
        );
      },
    );
  }
}
