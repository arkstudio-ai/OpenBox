import 'dart:async';

import 'package:bossip_mobile/features/admin/admin_console.dart';
import 'package:bossip_mobile/features/admin/billing/billing_page.dart';
import 'package:bossip_mobile/features/admin/billing/workspace_page.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_confirm.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_filters.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'admin_test_support.dart';

void main() {
  setUpAll(() async {
    adminBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
  });
  testWidgets(
    'console uses 320px bottom navigation and lazy destination reads',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(tester, h, AdminConsole(onExit: () {}));
      expect(tester.takeException(), isNull);
      expect(h.requests.length, 4);
      expect(
        h.requests.every((r) => r.path.startsWith('/api/admin/fleet')),
        isTrue,
      );
      await tester.tap(find.text('技能管理'));
      await tester.pumpAndSettle();
      expect(h.requests.last.path, '/api/admin/skills/store');
      final count = h.requests.length;
      await tester.pump(const Duration(seconds: 31));
      expect(
        h.requests.length,
        count,
        reason: 'Fleet is not polled while another tab is active',
      );
      await tester.tap(find.text('订阅管理'));
      await tester.pumpAndSettle();
      expect(h.requests.last.path, '/api/admin/billing/subscriptions');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
  testWidgets('fleet pauses polling when app backgrounds', (tester) async {
    final h = AdminHarness();
    await mountAdmin(tester, h, AdminConsole(onExit: () {}));
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump(const Duration(seconds: 31));
    expect(h.requests.length, 4);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump(const Duration(seconds: 31));
    await tester.pumpAndSettle();
    expect(h.requests.length, 8);
    await tester.pumpWidget(const SizedBox.shrink());
  });
  testWidgets(
    'destructive confirmation requires reason and exact target, submits once',
    (tester) async {
      final h = AdminHarness();
      final pending = Completer<void>();
      var calls = 0;
      await mountAdmin(
        tester,
        h,
        Builder(
          builder: (context) => TextButton(
            onPressed: () => confirmAdminAction(
              context,
              title: '危险操作',
              body: '目标目录',
              confirm: '执行',
              requireReason: true,
              target: '/exact/directory',
              run: (note, cancel) {
                calls++;
                expect(note, '测试原因');
                return pending.future;
              },
            ),
            child: const Text('打开'),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await tester.pumpAndSettle();
      final submit = find.byKey(const ValueKey('confirm-admin-action'));
      expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-reason')),
        '测试原因',
      );
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-target')),
        '/wrong',
      );
      await tester.pump();
      expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-target')),
        '/exact/directory',
      );
      await tester.pump();
      await tester.ensureVisible(submit);
      await tester.tap(submit);
      await tester.pump();
      expect(calls, 1);
      expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      pending.complete();
      await tester.pumpAndSettle();
      expect(find.text('危险操作'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'unknown write outcome disables retry until cancelled and refreshed',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        Builder(
          builder: (context) => TextButton(
            onPressed: () => confirmAdminAction(
              context,
              title: '操作',
              body: 'body',
              confirm: '执行',
              run: (_, _) async => throw DioException(
                requestOptions: RequestOptions(path: '/write'),
              ),
            ),
            child: const Text('打开'),
          ),
        ),
      );
      await tester.tap(find.text('打开'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('confirm-admin-action')));
      await tester.pumpAndSettle();
      expect(find.textContaining('不要重复提交'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('confirm-admin-action')),
            )
            .onPressed,
        isNull,
      );
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
    },
  );
  testWidgets(
    'filter sheet rejects reversed UTC days and cancelling keeps draft private',
    (tester) async {
      final h = AdminHarness();
      Map<String, String>? applied;
      await mountAdmin(
        tester,
        h,
        Builder(
          builder: (context) => TextButton(
            onPressed: () async {
              applied = await showAdminFilters(
                context,
                values: {'from': '2026-09-10', 'to': '2026-09-01', 'q': 'keep'},
                fields: const [
                  AdminFilterField('from', '开始', date: true),
                  AdminFilterField('to', '结束', date: true),
                ],
              );
            },
            child: const Text('筛选'),
          ),
        ),
      );
      await tester.tap(find.text('筛选'));
      await tester.pumpAndSettle();
      expect(find.text('开始日期不能晚于结束日期。'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('apply-admin-filters')),
            )
            .onPressed,
        isNull,
      );
      await tester.tap(find.byTooltip('关闭'));
      await tester.pumpAndSettle();
      expect(applied, isNull);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'billing search is explicit, orders lazy, never refetches on focus/timer',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(tester, h, const AdminBillingPage());
      expect(h.requests.length, 1);
      await tester.enterText(find.byType(TextField), 'keyword');
      await tester.pump(const Duration(seconds: 2));
      expect(h.requests.length, 1);
      await tester.testTextInput.receiveAction(TextInputAction.search);
      await tester.pumpAndSettle();
      expect(h.requests.last.queryParameters['q'], 'keyword');
      await tester.tap(find.text('订单'));
      await tester.pumpAndSettle();
      expect(h.requests.where((r) => r.path.endsWith('/orders')).length, 1);
      final count = h.requests.length;
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump(const Duration(minutes: 1));
      expect(h.requests.length, count);
      expect(h.requests.every((r) => r.method == 'GET'), isTrue);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'workspace tabs share one audited response and preserve Decimal strings',
    (tester) async {
      final h = AdminHarness()
        ..responder = (_) => {
          'workspace': {'id': 'tenant-1', 'name': '已删除空间', 'is_deleted': true},
          'owner': null,
          'member_count': 0,
          'balance': '0.000000001',
          'plan_id': 'free',
          'subscription': null,
          'queued': <dynamic>[],
          'history': <dynamic>[],
          'orders': <dynamic>[],
          'ledger': <dynamic>[],
          'usage': {'days': 30, 'items': <dynamic>[]},
        };
      await mountAdmin(
        tester,
        h,
        const AdminWorkspacePage(workspaceId: 'tenant-1'),
      );
      expect(find.text('0.000000001'), findsOneWidget);
      expect(find.text('已删除'), findsOneWidget);
      await tester.tap(find.text('订单'));
      await tester.pumpAndSettle();
      expect(find.text('该空间没有订单。'), findsOneWidget);
      await tester.tap(find.text('积分账本'));
      await tester.pumpAndSettle();
      expect(h.requests.length, 1);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets('workspace 404 is recoverable', (tester) async {
    final h = AdminHarness()
      ..responder = (o) => throw DioException(
        requestOptions: o,
        response: Response<dynamic>(requestOptions: o, statusCode: 404),
      );
    await mountAdmin(
      tester,
      h,
      const AdminWorkspacePage(workspaceId: 'missing'),
    );
    expect(find.textContaining('找不到这个空间'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
