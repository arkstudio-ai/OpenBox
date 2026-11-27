import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/session_drawer.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/project.dart';
import 'package:bossip_mobile/shared/models/session.dart';
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

class _ManagedWorkspaceController extends WorkspaceController {
  final submitted = <String>[];
  final conversations = [
    Session.fromJson({'id': 'first', 'title': '活动策划'}),
    Session.fromJson({'id': 'second', 'title': '门店菜单'}),
    Session.fromJson({'id': 'peer', 'title': '共享对话', 'user_id': 'peer'}),
    Session.fromJson({'id': 'assistant', 'title': '固定助理', 'kind': 'assistant'}),
  ];

  @override
  Future<WorkspaceData> build() async => WorkspaceData(sessions: conversations);

  @override
  Future<void> renameSession(String id, String title) async {
    final index = conversations.indexWhere((session) => session.id == id);
    conversations[index] = conversations[index].copyWith(title: title);
    state = AsyncData(WorkspaceData(sessions: [...conversations]));
  }

  @override
  Future<({List<String> deleted, List<String> failed})> deleteSessions(
    List<String> ids,
  ) async {
    submitted.addAll(ids);
    conversations.removeWhere((session) => session.id == ids.first);
    state = AsyncData(WorkspaceData(sessions: [...conversations]));
    return (deleted: [ids.first], failed: ids.skip(1).toList());
  }
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

  testWidgets(
    'standalone chat history renames and keeps only failed selections',
    (tester) async {
      final workspace = _ManagedWorkspaceController();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            await zhI18n(tester),
            workspaceProvider.overrideWith(() => workspace),
          ],
          child: MaterialApp(
            theme: testTheme(),
            home: const Scaffold(body: SessionDrawer(standalone: true)),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('固定助理'), findsNothing);
      expect(find.byTooltip('对话操作'), findsNWidgets(2));
      await tester.tap(find.byTooltip('对话操作').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('重命名对话'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextFormField), ' 周末促销 ');
      await tester.tap(find.text('确认'));
      await tester.pumpAndSettle();
      expect(find.text('周末促销'), findsOneWidget);

      await tester.tap(find.text('批量管理'));
      await tester.pumpAndSettle();
      expect(find.byType(Checkbox), findsNWidgets(2));
      await tester.tap(find.text('周末促销'));
      await tester.tap(find.text('门店菜单'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除 2 条'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.descendant(
          of: find.byType(AlertDialog),
          matching: find.text('删除 2 条'),
        ),
      );
      await tester.pumpAndSettle();
      expect(workspace.submitted, ['first', 'second']);
      expect(find.text('周末促销'), findsNothing);
      expect(find.text('已选择 1 条'), findsOneWidget);
      expect(tester.widget<Checkbox>(find.byType(Checkbox)).value, isTrue);
      expect(find.text('共享对话'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}
