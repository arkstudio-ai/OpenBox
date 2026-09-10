import 'package:bossip_mobile/features/admin/admin_console.dart';
import 'package:bossip_mobile/features/admin/notifications/notifications_page.dart';
import 'package:bossip_mobile/features/settings/widgets/account_section.dart';
import 'package:bossip_mobile/features/settings/widgets/notifications_section.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/notifications/system_notifications.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'admin_test_support.dart';

class _Auth extends AuthController {
  _Auth(this.role);
  final String role;
  @override
  AuthState build() => AuthState(
    isLoading: false,
    user: AuthUser(id: 'operator', username: 'Admin', role: role),
  );
}

Map<String, dynamic> _overview({bool ready = true, String? receipt}) => {
  'device': {
    'registered': ready,
    'bindingId': 'phone-a',
    'platform': 'ios',
    'notificationsEnabled': ready,
    'ready': ready,
  },
  'providers': [
    {'id': 'apns', 'configured': true},
    {'id': 'jpush', 'configured': true},
  ],
  'presence': {'appState': 'background'},
  'templates': [
    {'id': 'system_test', 'title': '通知测试', 'body': '手机通知正常。'},
  ],
  'tests': [
    {
      'id': 'event-a',
      'title': '通知测试',
      'body': '手机通知正常。',
      'status': 'accepted',
      'receipt': receipt,
      'createdAt': '2026-09-10T00:00:00Z',
      'expiresAt': '2026-09-10T00:05:00Z',
    },
  ],
};

void main() {
  setUpAll(() async {
    adminBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
  });
  for (final role in ['user', 'owner']) {
    testWidgets('$role cannot render or fetch notification diagnostics', (
      tester,
    ) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        const AdminNotificationsPage(),
        overrides: [authProvider.overrideWith(() => _Auth(role))],
      );
      expect(find.text('仅平台超级管理员可访问。'), findsOneWidget);
      expect(h.requests, isEmpty);
    });
    testWidgets('$role has no notification settings card', (tester) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        const AccountSection(),
        overrides: [authProvider.overrideWith(() => _Auth(role))],
      );
      expect(find.byType(NotificationsSection), findsNothing);
      expect(find.byType(SwitchListTile), findsNothing);
    });
  }
  for (final brightness in Brightness.values) {
    testWidgets(
      'notification tab fits 320px with large text in $brightness and sends only to bound phone',
      (tester) async {
        final h = AdminHarness();
        var opened = false;
        h.responder = (_) => _overview(receipt: opened ? 'opened' : null);
        await mountAdmin(
          tester,
          h,
          AdminConsole(initialSection: 'notifications', onExit: () {}),
          wrapInScaffold: false,
          width: 320,
          textScale: 1.3,
          brightness: brightness,
          overrides: [
            authProvider.overrideWith(() => _Auth('admin')),
            systemNotificationsProvider.overrideWith(
              (_) => SystemNotifications(),
            ),
          ],
        );
        expect(find.byIcon(Icons.notifications), findsOneWidget);
        expect(tester.takeException(), isNull);
        final button = find.widgetWithText(FilledButton, '发送远程测试');
        await tester.scrollUntilVisible(
          button.hitTestable(),
          200,
          scrollable: find.byType(Scrollable).first,
        );
        await tester.tap(button);
        await tester.pumpAndSettle();
        final sent = h.requests.where((r) => r.method == 'POST').single;
        expect(sent.path, '/api/admin/push/test');
        final body = sent.data as Map<String, dynamic>;
        expect(body.keys.toSet(), {'template', 'bindingId', 'requestId'});
        expect(body['bindingId'], 'phone-a');
        expect(
          body['requestId'],
          matches(
            RegExp(
              r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
            ),
          ),
        );
        await tester.scrollUntilVisible(
          find.text('通道已受理'),
          200,
          scrollable: find.byType(Scrollable).first,
        );
        expect(find.text('已点击通知'), findsNothing);
        opened = true;
        await tester.scrollUntilVisible(
          find.text('刷新状态'),
          -300,
          scrollable: find.byType(Scrollable).first,
        );
        await tester.tap(find.text('刷新状态'));
        await tester.pumpAndSettle();
        await tester.scrollUntilVisible(
          find.text('已点击通知'),
          250,
          scrollable: find.byType(Scrollable).first,
        );
        expect(tester.takeException(), isNull);
      },
    );
  }
  testWidgets('unbound phone disables remote test', (tester) async {
    final h = AdminHarness()..responder = (_) => _overview(ready: false);
    await mountAdmin(
      tester,
      h,
      const AdminNotificationsPage(),
      overrides: [
        authProvider.overrideWith(() => _Auth('admin')),
        systemNotificationsProvider.overrideWith((_) => SystemNotifications()),
      ],
    );
    await tester.scrollUntilVisible(
      find.byType(FilledButton),
      200,
      scrollable: find.byType(Scrollable).first,
    );
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
  });
}
