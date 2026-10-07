import 'dart:io';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/admin/billing/billing_action_sheet.dart';
import 'package:bossip_mobile/features/admin/billing/workspace_page.dart';
import 'package:bossip_mobile/features/admin/models/admin_data.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/widgets/labeled_checkbox.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'admin_test_support.dart';

const _font = String.fromEnvironment('ADMIN_UI_PREVIEW_FONT');
final _canvas = GlobalKey();

Map<String, dynamic> detail({bool writable = true}) => {
  'can_manage': writable,
  'workspace': {
    'id': 'target',
    'name': '用户创作空间',
    'kind': 'personal',
    'is_deleted': false,
  },
  'owner': {'id': 'e', 'username': 'e', 'email': 'e@example.invalid'},
  'member_count': 1,
  'balance': '12.5',
  'plan_id': 'free',
  'subscription': null,
  'queued': <dynamic>[],
  'history': <dynamic>[],
  'orders': <dynamic>[],
  'ledger': <dynamic>[],
  'usage': {'days': 30, 'items': <dynamic>[]},
};

Future<void> fillCredits(WidgetTester tester) async {
  final credits = find.byKey(const ValueKey('billing-credits'));
  await tester.ensureVisible(credits);
  await tester.enterText(credits, '100.123456');
  final reason = find.byKey(const ValueKey('billing-reason'));
  await tester.ensureVisible(reason);
  await tester.enterText(reason, '客服补充积分');
  tester.testTextInput.hide();
  await tester.pumpAndSettle();
  await tester.ensureVisible(find.byType(LabeledCheckbox));
  await tester.tap(find.byType(LabeledCheckbox));
  await tester.pumpAndSettle();
}

void main() {
  setUpAll(() async {
    adminBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
    if (_font.isNotEmpty) {
      final loader = FontLoader('AdminPreview')
        ..addFont(
          File(_font).readAsBytes().then((v) => ByteData.view(v.buffer)),
        );
      await loader.load();
    }
  });

  testWidgets(
    'credit action requires confirmation and posts exact decimal once',
    (tester) async {
      final h = AdminHarness();
      h.responder = (request) => request.method == 'GET'
          ? detail()
          : {'balance': '112.623456', 'replayed': false};
      await mountAdmin(
        tester,
        h,
        const AdminWorkspacePage(workspaceId: 'target'),
        wrapInScaffold: false,
      );
      await tester.tap(find.text('充值积分'));
      await tester.pumpAndSettle();
      final submit = find.byKey(const ValueKey('submit-billing-action'));
      expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      await fillCredits(tester);
      await tester.tap(submit);
      await tester.pumpAndSettle();
      final writes = h.requests.where((r) => r.method == 'POST').toList();
      expect(writes.length, 1);
      expect(
        writes.single.path,
        '/api/admin/billing/workspaces/target/credits',
      );
      final payload = writes.single.data as Map<String, dynamic>;
      expect(payload['credits'], '100.123456');
      expect(payload['reason'], '客服补充积分');
      expect(payload['request_key'], isNotEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'timeout retry survives closing the sheet without a second grant key',
    (tester) async {
      final h = AdminHarness();
      var writes = 0;
      h.responder = (request) {
        if (request.method == 'GET') return detail();
        if (writes++ == 0) {
          throw DioException(
            requestOptions: request,
            type: DioExceptionType.receiveTimeout,
          );
        }
        return {'balance': '112.623456', 'replayed': true};
      };
      await mountAdmin(
        tester,
        h,
        const AdminWorkspacePage(workspaceId: 'target'),
        wrapInScaffold: false,
      );
      await tester.tap(find.text('充值积分'));
      await tester.pumpAndSettle();
      await fillCredits(tester);
      await tester.tap(find.byKey(const ValueKey('submit-billing-action')));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('billing-credits')))
            .enabled,
        false,
      );
      await tester.tap(find.text('关闭'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('开通 / 续期'));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('billing-credits')))
            .controller!
            .text,
        '100.123456',
      );
      await tester.tap(find.byKey(const ValueKey('submit-billing-action')));
      await tester.pumpAndSettle();
      final posts = h.requests.where((r) => r.method == 'POST').toList();
      expect(posts.length, 2);
      expect(posts[0].path, posts[1].path);
      expect(posts[0].data, posts[1].data);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('read-only accounts expose no billing actions', (tester) async {
    final h = AdminHarness()..responder = (_) => detail(writable: false);
    await mountAdmin(
      tester,
      h,
      const AdminWorkspacePage(workspaceId: 'target'),
      wrapInScaffold: false,
    );
    expect(find.text('充值积分'), findsNothing);
    expect(find.text('开通 / 续期'), findsNothing);
    expect(h.requests.every((r) => r.method == 'GET'), isTrue);
  });

  testWidgets(
    'narrow native form keeps actions visible with keyboard and large text',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        RepaintBoundary(
          key: _canvas,
          child: AdminBillingActionSheet(
            detail: AdminRecord(detail()),
            kind: 'credits',
          ),
        ),
        width: 320,
        height: 844,
        textScale: 1.4,
        safeBottom: 34,
        brightness: Brightness.dark,
        fontFamily: _font.isEmpty ? null : 'AdminPreview',
      );
      await fillCredits(tester);
      tester.view.viewInsets = const FakeViewPadding(bottom: 240);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpAndSettle();
      final submit = find.byKey(const ValueKey('submit-billing-action'));
      expect(submit.hitTestable(), findsOneWidget);
      expect(tester.getRect(submit).bottom, lessThanOrEqualTo(604));
      expect(tester.takeException(), isNull);
      if (_font.isNotEmpty) {
        tester.view.resetViewInsets();
        await tester.pumpAndSettle();
        await tester.runAsync(() async {
          final boundary =
              _canvas.currentContext!.findRenderObject()!
                  as RenderRepaintBoundary;
          final image = await boundary.toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final file = File('build/admin-ui/billing-credits-native.png');
          await file.parent.create(recursive: true);
          await file.writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      }
    },
  );
}
