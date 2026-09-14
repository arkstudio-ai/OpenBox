import 'package:bossip_mobile/features/admin/admin_console.dart';
import 'package:bossip_mobile/features/admin/messages/announcements_page.dart';
import 'package:bossip_mobile/features/admin/messages/topics_page.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'admin_test_support.dart';

Map<String, dynamic> _announcement({
  String id = 'ann_1',
  String status = 'draft',
  Map<String, dynamic>? audience,
  bool push = false,
}) => {
  'id': id,
  'status': status,
  'title': '版本更新 $id',
  'body': '新版本已上线',
  'link': {'kind': 'topic', 'slug': 'release-2026-09'},
  'audience': audience ?? {'kind': 'all'},
  'push': push,
  'publishAt': null,
  'expiresAt': null,
  'publishedAt': status == 'published' ? '2026-09-11T09:00:00Z' : null,
  'fanoutAt': status == 'published' ? '2026-09-11T09:00:05Z' : null,
  'fanoutCount': status == 'published' ? 12 : 0,
  'createdBy': 'operator',
  'createdAt': '2026-09-11T08:00:00Z',
  'updatedAt': '2026-09-11T08:00:00Z',
};

Map<String, dynamic> _topic({String id = 'tpc_1', String status = 'draft'}) => {
  'id': id,
  'slug': 'release-2026-09',
  'title': '九月更新',
  'coverUrl': null,
  'contentMd': '# 新功能\n消息中心上线。',
  'ctaLabel': '去看看',
  'ctaLink': {'kind': 'skills', 'workspaceId': 'ws'},
  'status': status,
  'createdBy': 'operator',
  'createdAt': '2026-09-11T08:00:00Z',
  'updatedAt': '2026-09-11T08:00:00Z',
  'publishedAt': status == 'published' ? '2026-09-11T09:00:00Z' : null,
};

void main() {
  setUpAll(() async {
    adminBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
  });

  testWidgets('console gains a fifth destination for messages', (tester) async {
    final h = AdminHarness();
    h.responder = (options) => {'items': <dynamic>[]};
    await mountAdmin(
      tester,
      h,
      AdminConsole(initialSection: 'messages', onExit: () {}),
      wrapInScaffold: false,
    );
    expect(find.byIcon(Icons.campaign), findsOneWidget);
    expect(find.text('消息通知'), findsWidgets);
    expect(find.text('还没有公告。'), findsOneWidget);
    expect(h.requests.single.path, '/api/admin/messages/announcements');
  });

  testWidgets(
    'announcements list shows status, audience and the actions each status allows',
    (tester) async {
      final h = AdminHarness();
      h.responder = (options) => {
        'items': [
          _announcement(),
          _announcement(
            id: 'ann_2',
            status: 'published',
            audience: {
              'kind': 'users',
              'ids': ['u1', 'u2'],
            },
            push: true,
          ),
        ],
      };
      await mountAdmin(tester, h, const AdminAnnouncementsPage());
      expect(find.text('版本更新 ann_1'), findsOneWidget);
      expect(find.text('草稿'), findsOneWidget);
      expect(find.text('已发布'), findsOneWidget);
      expect(find.textContaining('已送达 12 人'), findsOneWidget);
      expect(find.textContaining('2 位指定用户'), findsOneWidget);
      expect(find.textContaining('专题 release-2026-09'), findsNWidgets(2));
      expect(
        find.byKey(const ValueKey('announcement-publish-ann_1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const ValueKey('announcement-publish-ann_2')),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('announcement-revoke-ann_2')),
        findsOneWidget,
      );
    },
  );

  testWidgets('the editor validates locally and posts the normalised body', (
    tester,
  ) async {
    final h = AdminHarness();
    h.responder = (options) =>
        options.method == 'POST' ? _announcement() : {'items': <dynamic>[]};
    await mountAdmin(tester, h, const AdminAnnouncementsPage());
    await tester.tap(find.byKey(const ValueKey('announcement-create')));
    await tester.pumpAndSettle();
    // Empty title is refused before any request.
    await tester.tap(find.byKey(const ValueKey('announcement-save')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('announcement-error')), findsOneWidget);
    expect(h.requests.where((r) => r.method == 'POST'), isEmpty);

    await tester.enterText(
      find.byKey(const ValueKey('announcement-title')),
      '  版本更新 ',
    );
    await tester.enterText(
      find.byKey(const ValueKey('announcement-body')),
      '新版本已上线',
    );
    await tester.tap(find.text('专题页'));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('announcement-link-value')),
      'release-2026-09',
    );
    await tester.tap(find.byKey(const ValueKey('announcement-audience')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('指定用户').last);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('announcement-user-ids')),
      'u1\nu2, u1',
    );
    await tester.tap(find.byKey(const ValueKey('announcement-push')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('announcement-save')));
    await tester.pumpAndSettle();
    final post = h.requests.where((r) => r.method == 'POST').single;
    expect(post.path, '/api/admin/messages/announcements');
    expect(post.data, {
      'title': '版本更新',
      'body': '新版本已上线',
      'link': {'kind': 'topic', 'slug': 'release-2026-09'},
      'audience': {
        'kind': 'users',
        'ids': ['u1', 'u2'],
      },
      'push': true,
      'publishAt': null,
      'expiresAt': null,
    });
    // Back on the list, which reloaded.
    expect(find.byKey(const ValueKey('announcement-create')), findsOneWidget);
    expect(h.requests.where((r) => r.method == 'GET').length, 2);
  });

  testWidgets('publishing confirms with the live recipient count', (
    tester,
  ) async {
    final h = AdminHarness();
    var published = false;
    h.responder = (options) {
      if (options.path.endsWith('/announcements/ann_1')) {
        return {..._announcement(), 'recipientCount': 42};
      }
      if (options.path.endsWith('/publish')) {
        published = true;
        return _announcement(status: 'published');
      }
      return {
        'items': [_announcement(status: published ? 'published' : 'draft')],
      };
    };
    await mountAdmin(tester, h, const AdminAnnouncementsPage());
    await tester.tap(find.byKey(const ValueKey('announcement-publish-ann_1')));
    await tester.pumpAndSettle();
    expect(find.textContaining('42 位收件人'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('confirm-admin-action')));
    await tester.pumpAndSettle();
    expect(
      h.requests.map((r) => r.path),
      contains('/api/admin/messages/announcements/ann_1/publish'),
    );
    expect(find.text('已发布'), findsOneWidget);
  });

  testWidgets('topics are read-only on the phone except publish state', (
    tester,
  ) async {
    final h = AdminHarness();
    var published = false;
    h.responder = (options) {
      if (options.path.endsWith('/publish')) {
        published = true;
        return _topic(status: 'published');
      }
      return {
        'items': [_topic(status: published ? 'published' : 'draft')],
      };
    };
    await mountAdmin(tester, h, const AdminTopicsPage());
    expect(find.text('九月更新'), findsOneWidget);
    expect(find.text('/topics/release-2026-09'), findsOneWidget);
    expect(find.text('专题正文请在网页端编辑。'), findsOneWidget);
    expect(find.text('复制链接'), findsNothing);
    await tester.tap(find.byKey(const ValueKey('topic-flip-tpc_1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('confirm-admin-action')));
    await tester.pumpAndSettle();
    expect(
      h.requests.map((r) => r.path),
      contains('/api/admin/messages/topics/tpc_1/publish'),
    );
    expect(find.text('复制链接'), findsOneWidget);
    await tester.tap(find.text('查看'));
    await tester.pumpAndSettle();
    expect(find.text('九月更新'), findsWidgets);
    expect(find.textContaining('新功能', findRichText: true), findsWidgets);
  });
}
