import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/inbox/api/inbox_api.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/session_drawer.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/inbox.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _EmptyWorkspace extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData();
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
      'inbox': '消息中心',
      'authCenter': '授权中心',
      'skillCenter': '技能中心',
      'scheduledTasks': '定时任务',
      'billing': '订购',
    },
  },
});

Future<void> _mount(WidgetTester tester, InboxUnread unread) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
        workspaceProvider.overrideWith(_EmptyWorkspace.new),
        inboxUnreadProvider.overrideWith((ref) async => unread),
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
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'the inbox row sits above the authorization centre with its unread badge',
    (tester) async {
      await _mount(tester, const InboxUnread(total: 4, session: 3, system: 1));
      final inbox = tester.getTopLeft(find.byKey(const ValueKey('nav-inbox')));
      final auth = tester.getTopLeft(find.text('授权中心'));
      expect(inbox.dy, lessThan(auth.dy));
      expect(find.text('消息中心'), findsOneWidget);
      expect(find.text('4'), findsOneWidget);
    },
  );

  testWidgets('no badge at zero', (tester) async {
    await _mount(tester, const InboxUnread());
    expect(find.text('消息中心'), findsOneWidget);
    expect(find.byKey(const ValueKey('nav-badge-消息中心')), findsNothing);
  });

  testWidgets('large counts are capped at 99+', (tester) async {
    await _mount(tester, const InboxUnread(total: 250, notice: 250));
    expect(find.text('99+'), findsOneWidget);
  });
}
