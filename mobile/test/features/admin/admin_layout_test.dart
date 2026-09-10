import 'dart:io';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/admin/admin_console.dart';
import 'package:bossip_mobile/features/admin/billing/billing_page.dart';
import 'package:bossip_mobile/features/admin/skills/editor_page.dart';
import 'package:bossip_mobile/features/admin/skills/installs_page.dart';
import 'package:bossip_mobile/features/admin/skills/review_detail_page.dart';
import 'package:bossip_mobile/features/admin/skills/store_page.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_confirm.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_filters.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_widgets.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'admin_test_support.dart';

// Optional mock-only PNGs for design review; ordinary regression runs need no fonts.
const _previewFont = String.fromEnvironment('ADMIN_UI_PREVIEW_FONT');
final _canvas = GlobalKey();
Future<void> _preview(WidgetTester tester, String name) async {
  if (_previewFont.isEmpty) return;
  final boundary =
      _canvas.currentContext!.findRenderObject()! as RenderRepaintBoundary;
  await tester.runAsync(() async {
    final image = await boundary.toImage();
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    final file = File('build/admin-ui/$name.png');
    await file.parent.create(recursive: true);
    await file.writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  setUpAll(() async {
    adminBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
    if (_previewFont.isNotEmpty) {
      final loader = FontLoader('AdminPreview')
        ..addFont(
          Future.value(
            ByteData.sublistView(await File(_previewFont).readAsBytes()),
          ),
        );
      await loader.load();
      await (FontLoader(
        'MaterialIcons',
      )..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'))).load();
    }
  });

  for (final language in ['zh-CN', 'en-US']) {
    for (final brightness in [Brightness.light, Brightness.dark]) {
      testWidgets(
        'store handles long content at 320px in $language ${brightness.name}',
        (tester) async {
          final h = AdminHarness()
            ..responder = (_) => testPage([
              {
                ...testEntry,
                'title': 'A long skill title for a multilingual workspace 团队技能',
                'catalog_id': 'community:${'long-identifier-' * 8}',
                'description':
                    'Full description with all the original details. ' * 16,
                'listing': 'listed',
                'featured': true,
                'installs_count': 12345,
              },
              {
                ...testEntry,
                'catalog_id': 'community:second',
                'title': 'Second skill',
              },
            ]);
          await mountAdmin(
            tester,
            h,
            RepaintBoundary(
              key: _canvas,
              child: AdminConsole(initialSection: 'skills', onExit: () {}),
            ),
            height: 740,
            textScale: 1.2,
            language: language,
            brightness: brightness,
            fontFamily: _previewFont.isEmpty ? null : 'AdminPreview',
          );
          expect(tester.takeException(), isNull);
          expect(find.byType(Checkbox), findsNothing);
          await _preview(tester, 'store-$language-${brightness.name}');
          final details = find
              .text(language == 'zh-CN' ? '查看详情' : 'View details')
              .first;
          await tester.ensureVisible(details);
          await tester.tap(details);
          await tester.pumpAndSettle();
          expect(
            find.text('Full description with all the original details. ' * 16),
            findsWidgets,
          );
          expect(
            find.text('community:${'long-identifier-' * 8}'),
            findsOneWidget,
          );
          final edit = find.widgetWithText(
            FilledButton,
            language == 'zh-CN' ? '编辑' : 'Edit',
          );
          expect(edit.hitTestable(), findsOneWidget);
          expect(h.requests.every((r) => r.method == 'GET'), isTrue);
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox.shrink());
        },
      );
    }
  }

  testWidgets(
    'store search leaves room for results above a small-screen keyboard',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        AdminConsole(initialSection: 'skills', onExit: () {}),
        height: 640,
        textScale: 1.4,
        language: 'en-US',
      );
      await tester.tap(find.byType(TextField));
      await tester.enterText(find.byType(TextField), 'draft query');
      tester.view.viewInsets = const FakeViewPadding(bottom: 260);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpAndSettle();
      expect(find.byType(NavigationBar), findsNothing);
      expect(find.byType(TextField).hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
      expect(
        h.requests.length,
        1,
        reason: 'Typing does not query until submitted',
      );
      tester.view.resetViewInsets();
      await tester.pumpAndSettle();
      expect(find.byType(NavigationBar), findsOneWidget);
      expect(find.byKey(const ValueKey('admin-store-trash')), findsOneWidget);
    },
  );

  testWidgets(
    'root admin console preserves search focus across keyboard metrics',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        AdminConsole(initialSection: 'skills', onExit: () {}),
        height: 640,
        language: 'en-US',
        wrapInScaffold: false,
      );
      expect(find.byType(Scaffold), findsOneWidget);
      final consoleSize = tester.getSize(find.byType(AdminConsole));
      await tester.tap(find.byType(TextField));
      await tester.enterText(find.byType(TextField), 'draft query');
      await tester.pumpAndSettle();
      final editable = tester.state<EditableTextState>(
        find.byType(EditableText),
      );
      final focus = editable.widget.focusNode;
      final editingValue = editable.widget.controller.value;
      expect(focus.hasFocus, isTrue);
      addTearDown(tester.view.resetViewInsets);

      // Only metrics change from here on: no tap or enterText may refocus it.
      for (final inset in [260.0, 300.0, 0.0]) {
        tester.view.viewInsets = FakeViewPadding(bottom: inset);
        await tester.pumpAndSettle();
        expect(tester.getSize(find.byType(AdminConsole)), consoleSize);
        expect(
          find.byType(NavigationBar),
          inset > 0 ? findsNothing : findsOneWidget,
        );
        expect(
          tester.state<EditableTextState>(find.byType(EditableText)),
          same(editable),
        );
        expect(editable.mounted, isTrue);
        expect(editable.widget.focusNode, same(focus));
        expect(focus.hasFocus, isTrue);
        expect(editable.widget.controller.value, editingValue);
        expect(tester.takeException(), isNull);
      }
      expect(find.byKey(const ValueKey('admin-store-trash')), findsOneWidget);
      expect(h.requests.length, 1, reason: 'Keyboard changes never query');
    },
  );

  testWidgets('selection is explicit and trash has a visible return path', (
    tester,
  ) async {
    final h = AdminHarness();
    await mountAdmin(tester, h, const AdminStorePage());
    expect(find.byType(Checkbox), findsNothing);
    await tester.tap(find.byKey(const ValueKey('admin-store-select')));
    await tester.pumpAndSettle();
    await tester.tap(find.byType(Checkbox));
    await tester.pumpAndSettle();
    expect(find.byType(AdminActionBar).hitTestable(), findsOneWidget);
    final reads = h.requests.length;
    await tester.tap(find.byKey(const ValueKey('admin-store-select')));
    await tester.pumpAndSettle();
    expect(find.byType(Checkbox), findsNothing);
    expect(find.byType(AdminActionBar), findsNothing);
    expect(h.requests.length, reads);
    await tester.tap(find.byKey(const ValueKey('admin-store-trash')));
    await tester.pumpAndSettle();
    expect(h.requests.last.queryParameters['deleted'], isTrue);
    expect(find.byKey(const ValueKey('admin-store-select')), findsNothing);
    await tester.tap(find.byKey(const ValueKey('admin-store-trash')));
    await tester.pumpAndSettle();
    expect(h.requests.last.queryParameters.containsKey('deleted'), isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'a failed individual deletion exposes the retained selection without retrying',
    (tester) async {
      final h = AdminHarness()
        ..responder = (options) => options.method == 'POST'
            ? testPage([
                {
                  'catalog_id': 'community:entry-1',
                  'ok': false,
                  'error': 'Still in use',
                },
              ])
            : testPage([testEntry]);
      await mountAdmin(tester, h, const AdminStorePage());
      await tester.tap(find.byTooltip('管理操作'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('admin-action-reason')),
        '清理旧条目',
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('confirm-admin-action')));
      await tester.pumpAndSettle();
      expect(tester.widget<Checkbox>(find.byType(Checkbox)).value, isTrue);
      expect(find.textContaining('Still in use'), findsOneWidget);
      expect(h.requests.where((r) => r.method == 'POST').length, 1);
      await tester.tap(find.byKey(const ValueKey('admin-store-select')));
      await tester.pumpAndSettle();
      expect(find.byType(Checkbox), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('fleet state drill-down applies a visible removable filter', (
    tester,
  ) async {
    final h = AdminHarness();
    await mountAdmin(
      tester,
      h,
      RepaintBoundary(
        key: _canvas,
        child: AdminConsole(onExit: () {}),
      ),
      width: 390,
      fontFamily: _previewFont.isEmpty ? null : 'AdminPreview',
    );
    await _preview(tester, 'fleet-zh-CN-light');
    await tester.tap(find.text('已分配'));
    await tester.pumpAndSettle();
    expect(
      h.requests
          .where((r) => r.path.endsWith('/desktops'))
          .last
          .queryParameters['pool_state'],
      'assigned',
    );
    final count = h.requests.length;
    await tester.tap(find.text('概览'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('桌面'));
    await tester.pumpAndSettle();
    expect(h.requests.length, count);
    expect(find.byType(InputChip), findsOneWidget);
    await tester.tap(
      find
          .descendant(of: find.byType(InputChip), matching: find.byType(Icon))
          .last,
    );
    await tester.pumpAndSettle();
    expect(
      h.requests
          .where((r) => r.path.endsWith('/desktops'))
          .last
          .queryParameters
          .containsKey('pool_state'),
      isFalse,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'install view retains lazy history and never triggers a live scan',
    (tester) async {
      final h = AdminHarness();
      await mountAdmin(tester, h, const AdminInstallsPage());
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('平台安装记录').last);
      await tester.pumpAndSettle();
      final count = h.requests.length;
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('虚拟机实际安装').last);
      await tester.pumpAndSettle();
      expect(h.requests.length, count);
      expect(
        h.requests.where(
          (r) => RegExp(
            r'^/api/admin/skills/desktops/[^/]+/skills$',
          ).hasMatch(r.path),
        ),
        isEmpty,
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'confirmation stays reachable above keyboard with a full long target',
    (tester) async {
      final h = AdminHarness();
      const target = '/home/member/skills/a-very-long-archive-directory';
      await mountAdmin(
        tester,
        h,
        Builder(
          builder: (context) => TextButton(
            onPressed: () => confirmAdminAction(
              context,
              title: '确认卸载团队技能',
              body: '保留全部目标与审计说明。' * 20,
              confirm: '确认卸载',
              requireReason: true,
              target: target,
              run: (_, _) async {},
            ),
            child: const Text('打开'),
          ),
        ),
        height: 640,
        safeBottom: 34,
        textScale: 1.4,
        brightness: Brightness.dark,
      );
      await tester.tap(find.text('打开'));
      await tester.pumpAndSettle();
      tester.view.viewInsets = const FakeViewPadding(bottom: 240);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpAndSettle();
      final reason = find.byKey(const ValueKey('admin-action-reason'));
      await tester.ensureVisible(reason);
      await tester.enterText(reason, '重复目录清理');
      final field = find.byKey(const ValueKey('admin-action-target'));
      await tester.ensureVisible(field);
      await tester.enterText(field, target);
      await tester.pumpAndSettle();
      final submit = find.byKey(const ValueKey('confirm-admin-action'));
      expect(tester.widget<FilledButton>(submit).onPressed, isNotNull);
      expect(submit.hitTestable(), findsOneWidget);
      expect(tester.getRect(submit).bottom, lessThanOrEqualTo(400));
      expect(h.requests, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'review decisions remain visible with large text and long archive metadata',
    (tester) async {
      final h = AdminHarness()
        ..responder = (_) => {
          ...testEntry,
          'sha256': 'a' * 64,
          'skill_md': '# Review\n${'Plain content\n' * 80}',
          'files': [
            {'path': 'deep/directory/' * 12, 'size': 4096},
          ],
        };
      await mountAdmin(
        tester,
        h,
        RepaintBoundary(
          key: _canvas,
          child: const AdminReviewDetailPage(catalogId: 'community:entry-1'),
        ),
        height: 640,
        textScale: 1.4,
        safeBottom: 34,
        brightness: Brightness.dark,
        fontFamily: _previewFont.isEmpty ? null : 'AdminPreview',
      );
      final reject = find.widgetWithText(OutlinedButton, '驳回');
      expect(reject.hitTestable(), findsOneWidget);
      expect(
        tester.getRect(find.byType(AdminActionBar)).bottom,
        lessThanOrEqualTo(640),
      );
      await tester.drag(find.byType(ListView), const Offset(0, -500));
      await tester.pumpAndSettle();
      expect(reject.hitTestable(), findsOneWidget);
      expect(h.requests.length, 1);
      expect(tester.takeException(), isNull);
      await _preview(tester, 'review-dark-large-text');
    },
  );

  testWidgets(
    'removing the scoped navigator also removes its filter calendar',
    (tester) async {
      final h = AdminHarness();
      final visible = ValueNotifier(true);
      addTearDown(visible.dispose);
      await mountAdmin(
        tester,
        h,
        ValueListenableBuilder<bool>(
          valueListenable: visible,
          builder: (_, allowed, _) => allowed
              ? Navigator(
                  onGenerateRoute: (_) => MaterialPageRoute<void>(
                    builder: (context) => Scaffold(
                      body: TextButton(
                        onPressed: () => showAdminFilters(
                          context,
                          values: const {'from': '2026-09-10'},
                          fields: const [
                            AdminFilterField('from', '开始日期', date: true),
                          ],
                        ),
                        child: const Text('打开筛选'),
                      ),
                    ),
                  ),
                )
              : const Text('已退出作用域'),
        ),
      );
      await tester.tap(find.text('打开筛选'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(TextField));
      await tester.pumpAndSettle();
      expect(find.byType(DatePickerDialog), findsOneWidget);
      visible.value = false;
      await tester.pumpAndSettle();
      expect(find.byType(DatePickerDialog), findsNothing);
      expect(find.byKey(const ValueKey('apply-admin-filters')), findsNothing);
      expect(find.text('已退出作用域'), findsOneWidget);
      expect(h.requests, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('an uncertain editor save cannot be submitted again', (
    tester,
  ) async {
    final h = AdminHarness()
      ..responder = (o) {
        if (o.method == 'GET') return testEntry;
        throw DioException(
          requestOptions: o,
          type: DioExceptionType.receiveTimeout,
        );
      };
    await mountAdmin(
      tester,
      h,
      const AdminSkillEditorPage(catalogId: 'community:entry-1'),
    );
    final save = find.byKey(const ValueKey('save-admin-entry'));
    await tester.tap(save);
    await tester.pumpAndSettle();
    expect(tester.widget<FilledButton>(save).onPressed, isNull);
    expect(h.requests.where((r) => r.method != 'GET').length, 1);
    expect(find.textContaining('不要重复提交'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('billing retains the explicit query across section changes', (
    tester,
  ) async {
    final h = AdminHarness();
    await mountAdmin(
      tester,
      h,
      const AdminBillingPage(),
      height: 640,
      textScale: 1.2,
    );
    await tester.enterText(find.byType(TextField), 'long-workspace-identifier');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pumpAndSettle();
    await tester.tap(find.text('订单'));
    await tester.pumpAndSettle();
    final count = h.requests.length;
    await tester.tap(find.text('订阅'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      'long-workspace-identifier',
    );
    expect(h.requests.length, count);
    expect(tester.takeException(), isNull);
  });
}
