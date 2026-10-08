import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/inbox/api/inbox_api.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/session_drawer.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/inbox.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

class _EmptyWorkspace extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData();
}

Future<void> _mount(
  WidgetTester tester,
  InboxUnread unread, {
  int assistantUnread = 0,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        workspaceProvider.overrideWith(_EmptyWorkspace.new),
        inboxUnreadProvider.overrideWith((ref) async => unread),
        billingBalanceProvider.overrideWith(
          (ref) async =>
              const CreditBalance(workspaceId: '', balance: '0', mode: 'off'),
        ),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(body: SessionDrawer(assistantUnread: assistantUnread)),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'the message centre is on the first row of tiles, above the authorization centre, with its unread count',
    (tester) async {
      await _mount(tester, const InboxUnread(total: 4, session: 3, system: 1));
      final inbox = tester.getTopLeft(find.byKey(const ValueKey('nav-inbox')));
      final auth = tester.getTopLeft(find.text('授权中心'));
      expect(inbox.dy, lessThan(auth.dy));
      expect(find.text('消息中心'), findsOneWidget);
      expect(find.byKey(const ValueKey('nav-badge-消息中心')), findsOneWidget);
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

  testWidgets('assistant and cross-workspace inbox badges remain distinct', (
    tester,
  ) async {
    await _mount(tester, const InboxUnread(total: 3), assistantUnread: 7);
    expect(find.byKey(const ValueKey('nav-badge-个人助理')), findsOneWidget);
    expect(find.text('7'), findsOneWidget);
    expect(find.text('3'), findsOneWidget);
  });
}
