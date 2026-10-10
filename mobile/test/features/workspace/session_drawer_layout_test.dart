import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/session_drawer.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/project.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

class _FixedWorkspaceController extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData(
    projects: [
      Project(id: 'short', name: 'test'),
      Project(id: 'long', name: '默认空间'),
    ],
  );
}

const _tiles = ['云桌面', '消息中心', '知识库', '定时任务', '资源中心', '技能中心', '授权中心', '订购'];

Future<void> _mount(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        workspaceProvider.overrideWith(_FixedWorkspaceController.new),
        billingBalanceProvider.overrideWith(
          (ref) async =>
              const CreditBalance(workspaceId: '', balance: '0', mode: 'off'),
        ),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: const Scaffold(body: SessionDrawer()),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

double _top(WidgetTester tester, Finder finder) => tester.getTopLeft(finder).dy;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'the assistant card leads, the centre pages sit four to a row, and the '
    'work starts with a new chat above the projects',
    (tester) async {
      await _mount(tester, const Size(390, 844));
      final card = find.byKey(const ValueKey('nav-assistant'));
      expect(find.text('个人助理'), findsOneWidget);
      expect(find.text('交代事情，跟进进度'), findsOneWidget);
      for (final label in _tiles) {
        expect(find.text(label), findsOneWidget, reason: label);
      }
      // Two rows of four, everyday pages first.
      final firstRow = _top(tester, find.text('云桌面'));
      for (final label in _tiles.take(4)) {
        expect(_top(tester, find.text(label)), firstRow, reason: label);
      }
      final secondRow = _top(tester, find.text('资源中心'));
      expect(secondRow, greaterThan(firstRow));
      for (final label in _tiles.skip(4)) {
        expect(_top(tester, find.text(label)), secondRow, reason: label);
      }
      expect(_top(tester, card), lessThan(firstRow));
      final newChat = _top(
        tester,
        find.byKey(const ValueKey('drawer-new-chat')),
      );
      final newProject = _top(
        tester,
        find.byKey(const ValueKey('drawer-new-project')),
      );
      expect(newChat, greaterThan(secondRow));
      expect(newProject, greaterThan(newChat));
      expect(find.text('新对话'), findsOneWidget);
      expect(_top(tester, find.text('test')), greaterThan(newProject));
    },
  );

  testWidgets('project new-chat actions share one fixed trailing column', (
    tester,
  ) async {
    await _mount(tester, const Size(390, 844));
    final short = tester.getCenter(
      find.byKey(const ValueKey('new-chat-short')),
    );
    final long = tester.getCenter(find.byKey(const ValueKey('new-chat-long')));
    expect(short.dx, long.dx);
    expect(find.byTooltip('新建对话'), findsNWidgets(2));
  });

  testWidgets(
    'a short phone keeps the centre pages to one row of icons, named for '
    'screen readers and tooltips',
    (tester) async {
      await _mount(tester, const Size(375, 667));
      for (final label in _tiles) {
        expect(find.text(label), findsNothing, reason: label);
        expect(find.byTooltip(label), findsOneWidget, reason: label);
      }
      final row = tester.getTopLeft(find.byTooltip('云桌面')).dy;
      expect(tester.getTopLeft(find.byTooltip('订购')).dy, row);
      expect(find.bySemanticsLabel('消息中心'), findsOneWidget);
      // The projects still have room under the work actions.
      expect(find.text('默认空间'), findsOneWidget);
    },
  );
}
