import 'package:bossip_mobile/app/auth_redirect.dart';
import 'package:bossip_mobile/features/inbox/inbox_screen.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:bossip_mobile/shared/models/workspace.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _OfflineWsClient extends AgentWsClient {
  _OfflineWsClient() : super(Dio());
  @override
  Future<void> connect() async {}
}

class _InboxAuth extends AuthController {
  _InboxAuth(this.role);
  final String role;

  @override
  AuthState build() => AuthState(
    user: AuthUser(id: 'user', username: 'User', role: role),
    isLoading: false,
  );
}

class _InboxMode extends UiModeController {
  @override
  UiModeState build() =>
      const UiModeState(userId: 'user', mode: UiMode.light, loaded: true);
}

class _Workspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async {
    ref.read(workspaceScopeProvider).currentId = 'home';
    return const ActiveWorkspaceState(
      currentId: 'home',
      items: [
        WorkspaceSummary(
          id: 'home',
          name: 'Home',
          ownerUserId: 'user',
          kind: WorkspaceKind.personal,
          role: WorkspaceRole.owner,
        ),
        WorkspaceSummary(
          id: 'team',
          name: 'Team',
          ownerUserId: 'boss',
          kind: WorkspaceKind.team,
          role: WorkspaceRole.member,
        ),
      ],
    );
  }

  @override
  Future<void> select(String workspaceId) async {
    ref.read(workspaceScopeProvider).currentId = workspaceId;
    state = AsyncData(state.requireValue.copyWith(currentId: workspaceId));
  }
}

Map<String, dynamic> _item(
  String id, {
  String category = 'session',
  String kind = 'task_completed',
  bool read = false,
  String workspaceId = 'home',
  Map<String, dynamic>? link,
}) => {
  'id': id,
  'category': category,
  'kind': kind,
  'title': 'Title $id',
  'body': 'Body $id',
  'link':
      link ??
      {'kind': 'session', 'workspaceId': workspaceId, 'sessionId': 'sess-$id'},
  'workspaceId': workspaceId,
  'readAt': read ? '2026-09-11T00:00:00+00:00' : null,
  'resolvedAt': null,
  'createdAt': '2026-09-11T08:00:00+00:00',
};

final _unread = {'total': 3, 'session': 2, 'system': 1, 'notice': 0};

class _Server {
  final requests = <RequestOptions>[];
  Map<String, List<Map<String, dynamic>>> pages = {
    '': [
      _item('a'),
      _item('b', category: 'system', kind: 'publish_done', workspaceId: 'team'),
      _item('c', read: true),
    ],
    'session': [_item('a'), _item('c', read: true)],
    'system': [
      _item('b', category: 'system', kind: 'publish_done', workspaceId: 'team'),
    ],
    'notice': [],
  };

  Dio dio() {
    final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (o, h) {
          requests.add(o);
          Object? data;
          if (o.path == '/api/inbox') {
            final category = (o.queryParameters['category'] as String?) ?? '';
            data = {
              'items': pages[category] ?? [],
              'nextCursor': null,
              'unread': _unread,
            };
          } else if (o.path == '/api/inbox/unread') {
            data = _unread;
          } else if (o.path == '/api/inbox/read-all') {
            data = {'updated': 2};
          } else if (o.path.startsWith('/api/inbox/') &&
              o.path.endsWith('/read')) {
            final id = o.path.split('/')[3];
            final row = pages['']!.firstWhere((r) => r['id'] == id);
            data = {...row, 'readAt': '2026-09-11T09:00:00+00:00'};
          } else {
            data = {};
          }
          h.resolve(
            Response<dynamic>(requestOptions: o, statusCode: 200, data: data),
          );
        },
      ),
    );
    return dio;
  }
}

late I18nBundle _bundle;

Future<GoRouter> _mount(
  WidgetTester tester,
  _Server server, {
  bool light = false,
  String? role,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final router = GoRouter(
    initialLocation: light ? '/inbox' : '/app/inbox',
    redirect: (_, state) =>
        uiModeHome(state.matchedLocation, state.uri, light: light),
    routes: [
      GoRoute(path: '/app/inbox', builder: (_, _) => const InboxScreen()),
      GoRoute(
        path: '/inbox',
        builder: (_, _) => const InboxScreen(embedded: true),
      ),
      for (final path in [
        '/app/s/:sessionId',
        '/app/auth-center',
        '/app/topics/:slug',
        '/light/productions/:sessionId',
      ])
        GoRoute(path: path, builder: (_, _) => const SizedBox()),
    ],
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle, prefs)),
        apiDioProvider.overrideWithValue(server.dio()),
        wsClientProvider.overrideWithValue(_OfflineWsClient()),
        activeWorkspaceProvider.overrideWith(_Workspace.new),
        if (light) uiModeProvider.overrideWith(_InboxMode.new),
        if (role != null) authProvider.overrideWith(() => _InboxAuth(role)),
      ],
      child: MaterialApp.router(
        routerConfig: router,
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return router;
}

void main() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    _bundle = await I18nBundle.load();
  });

  testWidgets('production notices group by task and open the production', (
    tester,
  ) async {
    final server = _Server();
    const link = {
      'kind': 'session',
      'workspaceId': 'home',
      'sessionId': 'menu',
      'production': true,
    };
    server.pages[''] = [
      {..._item('ready', link: link), 'productionTitle': '周末特惠菜单'},
      {
        ..._item('review', kind: 'approval_required', link: link),
        'productionTitle': '周末特惠菜单',
      },
    ];
    final router = await _mount(tester, server, light: true);
    expect(find.text('周末特惠菜单'), findsOneWidget);
    expect(find.text('Title review'), findsNothing);
    final group = find.byType(ExpansionTile);
    await tester.tap(
      find.descendant(of: group, matching: find.byIcon(Icons.expand_more)),
    );
    await tester.pumpAndSettle();
    expect(find.text('Title review'), findsOneWidget);
    await tester.tap(find.text('周末特惠菜单'));
    await tester.pumpAndSettle();
    expect(
      router.routeInformationProvider.value.uri.path,
      '/light/productions/menu',
    );
  });

  testWidgets('an unread failed-turn notice opens its exact admin trace', (
    tester,
  ) async {
    const launcher = MethodChannel('plugins.flutter.io/url_launcher');
    final opened = <MethodCall>[];
    final messenger = tester.binding.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(launcher, (call) async {
      opened.add(call);
      return true;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(launcher, null));
    final server = _Server();
    server.pages[''] = [
      _item(
        'failed',
        category: 'system',
        kind: 'trajectory_turn_failed',
        link: {
          'kind': 'admin_trajectory_turn',
          'userId': 'user-a',
          'sessionId': 'session-b',
          'turnId': 'turn-c',
        },
      ),
    ];
    final router = await _mount(tester, server, light: true, role: 'admin');
    expect(find.byKey(const ValueKey('inbox-unread-failed')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('inbox-failed')));
    await tester.pumpAndSettle();
    expect(
      server.requests.map((request) => request.path),
      contains('/api/inbox/failed/read'),
    );
    expect(find.byKey(const ValueKey('inbox-unread-failed')), findsNothing);
    expect(opened, hasLength(1));
    expect(opened.single.method, 'launch');
    final arguments = opened.single.arguments as Map<Object?, Object?>;
    expect(
      arguments['url'],
      '${Env.webBase}/app/admin/trajectories/users/user-a/turns/session-b/turn-c',
    );
    expect(arguments['useSafariVC'], isTrue);
    expect(arguments['headers'], isEmpty);
    expect(router.canPop(), isFalse);
  });

  testWidgets('marking a linkless notice read does not re-open the inbox', (
    tester,
  ) async {
    final server = _Server();
    server.pages[''] = [
      {..._item('note', category: 'notice'), 'link': null},
    ];
    final router = await _mount(tester, server, light: true);
    await tester.tap(find.byKey(const ValueKey('inbox-note')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('inbox-unread-note')), findsNothing);
    expect(router.state.uri.path, '/inbox');
    expect(router.canPop(), isFalse);
  });

  testWidgets(
    'lists rows with unread dots, foreign workspace chip and tab counts',
    (tester) async {
      final server = _Server();
      await _mount(tester, server);
      expect(find.text('Title a'), findsOneWidget);
      expect(find.byKey(const ValueKey('inbox-unread-a')), findsOneWidget);
      expect(find.byKey(const ValueKey('inbox-unread-c')), findsNothing);
      expect(find.text('工作空间：Team'), findsOneWidget);
      expect(find.text('全部 3'), findsOneWidget);
      expect(find.text('会话 2'), findsOneWidget);
      expect(find.text('官方'), findsOneWidget);
    },
  );

  testWidgets(
    'switching tabs requests that category; empty tab shows the empty copy',
    (tester) async {
      final server = _Server();
      await _mount(tester, server);
      await tester.tap(find.text('官方'));
      await tester.pumpAndSettle();
      expect(
        server.requests
            .where((r) => r.path == '/api/inbox')
            .last
            .queryParameters['category'],
        'notice',
      );
      expect(find.text('暂无消息'), findsOneWidget);
      await tester.tap(find.text('系统 1'));
      await tester.pumpAndSettle();
      expect(find.text('Title b'), findsOneWidget);
      expect(find.text('Title a'), findsNothing);
    },
  );

  testWidgets(
    'tapping an unread row marks it read on the server and opens the chat',
    (tester) async {
      final server = _Server();
      final router = await _mount(tester, server);
      await tester.tap(find.byKey(const ValueKey('inbox-a')));
      await tester.pumpAndSettle();
      final paths = server.requests.map((r) => r.path).toList();
      expect(paths, contains('/api/inbox/a/read'));
      expect(paths, contains('/api/agent/session/sess-a'));
      expect(router.routeInformationProvider.value.uri.path, '/app/s/sess-a');
    },
  );

  testWidgets('a read row is opened without another read receipt', (
    tester,
  ) async {
    final server = _Server();
    final router = await _mount(tester, server);
    await tester.tap(find.byKey(const ValueKey('inbox-c')));
    await tester.pumpAndSettle();
    expect(
      server.requests.map((r) => r.path),
      isNot(contains('/api/inbox/c/read')),
    );
    expect(router.routeInformationProvider.value.uri.path, '/app/s/sess-c');
  });

  testWidgets('mark all read posts for the current tab and clears the dots', (
    tester,
  ) async {
    final server = _Server();
    await _mount(tester, server);
    await tester.tap(find.byKey(const ValueKey('inbox-read-all')));
    await tester.pumpAndSettle();
    final call = server.requests.singleWhere(
      (r) => r.path == '/api/inbox/read-all',
    );
    expect(call.queryParameters.containsKey('category'), isFalse);
    expect(find.byKey(const ValueKey('inbox-unread-a')), findsNothing);
    expect(find.byKey(const ValueKey('inbox-unread-b')), findsNothing);
  });
}
