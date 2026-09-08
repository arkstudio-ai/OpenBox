import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/session_drawer.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/project.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _FixedWorkspaceController extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData(
    projects: [
      Project(id: 'short', name: 'test'),
      Project(id: 'long', name: '默认空间'),
    ],
  );
}

I18nBundle _bundle() => I18nBundle({
  'zh-CN': {
    'workbench': {
      'tabs': {'desktop': '云桌面'},
    },
    'workspace': {
      'newProject': '新建项目',
      'search': '搜索',
      'resourceCenter': '资源中心',
      'authCenter': '授权中心',
      'skillCenter': '技能中心',
      'scheduledTasks': '定时任务',
      'billing': '订购',
      'newChatIn': '新建对话',
      'noChats': '还没有对话',
    },
  },
});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('project new-chat actions share one fixed trailing column', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
          workspaceProvider.overrideWith(_FixedWorkspaceController.new),
          billingBalanceProvider.overrideWith(
            (ref) async =>
                const CreditBalance(workspaceId: '', balance: '0', mode: 'off'),
          ),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: const Scaffold(body: SessionDrawer()),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final short = tester.getCenter(
      find.byKey(const ValueKey('new-chat-short')),
    );
    final long = tester.getCenter(find.byKey(const ValueKey('new-chat-long')));
    expect(short.dx, long.dx);
    expect(find.byTooltip('新建对话'), findsNWidgets(2));
    expect(find.text('云桌面'), findsOneWidget);
  });
}
