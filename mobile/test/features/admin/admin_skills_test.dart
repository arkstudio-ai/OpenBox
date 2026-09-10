import 'package:bossip_mobile/features/admin/skills/desktop_install_page.dart';
import 'package:bossip_mobile/features/admin/skills/editor_page.dart';
import 'package:bossip_mobile/features/admin/skills/review_detail_page.dart';
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
    'metadata-only editor keeps revision and archive content on conflict',
    (tester) async {
      final h = AdminHarness()
        ..responder = (o) {
          if (o.method == 'GET') return testEntry;
          throw DioException(
            requestOptions: o,
            response: Response<dynamic>(
              requestOptions: o,
              statusCode: 409,
              data: {'detail': 'Revision conflict'},
            ),
          );
        };
      await mountAdmin(
        tester,
        h,
        const AdminSkillEditorPage(catalogId: 'community:entry-1'),
      );
      final title = find.byKey(const ValueKey('admin-editor-title'));
      await tester.ensureVisible(title);
      await tester.enterText(title, '修改后的名称');
      await tester.tap(find.byKey(const ValueKey('save-admin-entry')));
      await tester.pumpAndSettle();
      final body = h.requests.last.data as Map<String, dynamic>;
      expect(body['expected_revision'], 7);
      expect(body.containsKey('content'), isFalse);
      expect(body['title'], '修改后的名称');
      expect(find.text('修改后的名称'), findsOneWidget);
      expect(find.text('操作冲突，请刷新后重试。'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets('create rejects invalid slug and empty title before any write', (
    tester,
  ) async {
    final h = AdminHarness();
    await mountAdmin(tester, h, const AdminSkillEditorPage());
    await tester.enterText(
      find.byKey(const ValueKey('admin-editor-name')),
      'Invalid Name',
    );
    await tester.tap(find.byKey(const ValueKey('save-admin-entry')));
    await tester.pumpAndSettle();
    expect(find.textContaining('标识须为小写英文'), findsOneWidget);
    expect(h.requests, isEmpty);
    expect(tester.takeException(), isNull);
  });
  testWidgets('unreadable archive is not presented as empty or safe', (
    tester,
  ) async {
    final h = AdminHarness()
      ..responder = (_) => {
        ...testEntry,
        'archive_error': 'Too many entries',
        'skill_md': null,
        'files': <dynamic>[],
      };
    await mountAdmin(
      tester,
      h,
      const AdminReviewDetailPage(catalogId: 'community:entry-1'),
    );
    await tester.scrollUntilVisible(
      find.textContaining('服务端没有打开这个包'),
      250,
      scrollable: find.byType(Scrollable).first,
    );
    expect(find.textContaining('这些字节没有人读过'), findsOneWidget);
    expect(find.text('这个包里没有 SKILL.md。'), findsNothing);
    expect(h.requests.single.method, 'GET');
    expect(tester.takeException(), isNull);
  });
  testWidgets('review shows plaintext and reject requires audit note', (
    tester,
  ) async {
    final h = AdminHarness()
      ..responder = (_) => {
        ...testEntry,
        'skill_md': '<script>ignored()</script> **plain**',
        'files': [
          {'path': 'SKILL.md', 'size': 32},
        ],
        'files_total': 501,
        'files_truncated': true,
      };
    await mountAdmin(
      tester,
      h,
      const AdminReviewDetailPage(catalogId: 'community:entry-1'),
    );
    await tester.scrollUntilVisible(
      find.text('驳回'),
      350,
      scrollable: find.byType(Scrollable).first,
    );
    expect(find.text('<script>ignored()</script> **plain**'), findsOneWidget);
    await tester.tap(find.text('驳回'));
    await tester.pumpAndSettle();
    final button = find.byKey(const ValueKey('confirm-admin-action'));
    expect(tester.widget<FilledButton>(button).onPressed, isNull);
    await tester.enterText(
      find.byKey(const ValueKey('admin-action-reason')),
      '缺少必要说明',
    );
    await tester.pump();
    await tester.ensureVisible(button);
    await tester.tap(button);
    await tester.pumpAndSettle();
    final writes = h.requests.where((r) => r.method != 'GET').toList();
    expect(
      writes.single.path,
      '/api/admin/skills/review/community%3Aentry-1/reject',
    );
    expect(writes.single.data, {'note': '缺少必要说明'});
    expect(h.skillsChanged, 1);
  });
  testWidgets(
    'desktop selection never scans automatically, partial is unknown',
    (tester) async {
      final h = AdminHarness()
        ..responder = (_) => {
          'items': <dynamic>[],
          'unavailable': ['mcp'],
          'scanned_at': '2026-09-10T00:00:00Z',
        };
      await mountAdmin(
        tester,
        h,
        const AdminDesktopInstallPage(desktop: testDesktop),
      );
      expect(h.requests, isEmpty);
      await tester.tap(find.byKey(const ValueKey('admin-scan-desktop')));
      await tester.pumpAndSettle();
      expect(h.requests.single.queryParameters, {'user_id': 'member-1'});
      expect(find.textContaining('部分扫描失败'), findsOneWidget);
      expect(find.textContaining('扫描完成：'), findsNothing);
      await tester.tap(find.byKey(const ValueKey('admin-scan-member')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Bob · bob@example.invalid').last);
      await tester.pumpAndSettle();
      expect(h.requests.length, 1);
      expect(find.textContaining('部分扫描失败'), findsNothing);
      await tester.tap(find.byKey(const ValueKey('admin-scan-desktop')));
      await tester.pumpAndSettle();
      expect(h.requests.last.queryParameters, {'user_id': 'member-2'});
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'uninstall scopes exact directory, failure forces rescan without retry',
    (tester) async {
      final h = AdminHarness()
        ..responder = (o) {
          if (o.method == 'POST') {
            throw DioException(
              requestOptions: o,
              response: Response<dynamic>(
                requestOptions: o,
                statusCode: 409,
                data: {'detail': 'Not confirmed'},
              ),
            );
          }
          return {
            'items': [testSkill],
            'unavailable': <dynamic>[],
            'scanned_at': '2026-09-10T00:00:00Z',
          };
        };
      await mountAdmin(
        tester,
        h,
        const AdminDesktopInstallPage(desktop: testDesktop),
      );
      await tester.tap(find.byKey(const ValueKey('admin-scan-desktop')));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('卸载'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.text('卸载'));
      await tester.pumpAndSettle();
      final button = find.byKey(const ValueKey('confirm-admin-action'));
      expect(tester.widget<FilledButton>(button).onPressed, isNull);
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-reason')),
        '测试卸载',
      );
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-target')),
        '/home/member-1/skills/bundle',
      );
      await tester.pump();
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(tester.widget<FilledButton>(button).onPressed, isNull);
      expect(
        (h.requests.last.data as Map<String, dynamic>)['user_id'],
        'member-1',
      );
      await tester.tap(find.text('取消'));
      await tester.pumpAndSettle();
      expect(find.textContaining('卸载未确认'), findsOneWidget);
      expect(find.text('卸载'), findsNothing);
      expect(h.requests.where((r) => r.method == 'POST').length, 1);
      expect(tester.takeException(), isNull);
    },
  );
}
