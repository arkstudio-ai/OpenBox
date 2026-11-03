import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/home_screen.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/light.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Auth extends AuthController {
  @override
  AuthState build() => const AuthState(
    isLoading: false,
    user: AuthUser(id: 'u1', username: 'alice', role: 'user'),
  );
}

class _Workspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async =>
      const ActiveWorkspaceState(currentId: 'w1');
}

class _FakeLightApi extends LightApi {
  _FakeLightApi() : super(Dio());

  LightEntriesView view = LightEntriesView.fromJson(_entriesJson());
  List<LightRecentGroup> groups = const [];
  bool recentDown = false;
  String? shop = '巷口小馆';

  @override
  Future<LightEntriesView> entries() async => view;

  @override
  Future<List<LightRecentGroup>> recent({int limit = 8}) async {
    if (recentDown) throw DioException(requestOptions: RequestOptions());
    return groups;
  }

  @override
  Future<String?> shopName() async => shop;

  @override
  Future<String?> assetUrl(String assetId) async => null;

  @override
  Future<List<Session>> sessions() async => const [];

  @override
  Future<List<Map<String, dynamic>>> assistantSessions() async => const [];
}

Map<String, dynamic> _entriesJson({String plan = 'free'}) => {
  'version': '2026-10-31',
  'ui_mode': 'light',
  'plan_id': plan,
  'desktop': false,
  'entries': [
    {
      'id': 'video',
      'title': '做一条视频',
      'subtitle': '店内环境 / 餐品展示 / 探店',
      'kind': 'primary',
      'price_hint': '15 秒省钱档约 4 积分',
      'requires': <String, dynamic>{},
      'availability': 'ok',
    },
    {
      'id': 'menu',
      'title': '做菜单',
      'subtitle': '拍旧菜单，出新菜单',
      'kind': 'primary',
      'requires': <String, dynamic>{},
      'availability': 'ok',
    },
    {
      'id': 'poster',
      'title': '做海报',
      'subtitle': '新品 / 节日',
      'kind': 'primary',
      'requires': <String, dynamic>{},
      'availability': 'ok',
    },
    {
      'id': 'douyin_publish',
      'title': '发到抖音',
      'kind': 'secondary',
      'requires': {'desktop': true},
      'availability': 'needs_desktop',
    },
    {
      'id': 'review',
      'title': '成绩复盘',
      'kind': 'secondary',
      'requires': {'desktop': true, 'binding': 'douyin'},
      'availability': 'needs_desktop',
    },
    {
      'id': 'footage_edit',
      'title': '你拍我剪',
      'subtitle': '自己拍的素材也能出片',
      'kind': 'coming_soon',
      'requires': <String, dynamic>{},
      'availability': 'coming_soon',
    },
  ],
};

Future<(_FakeLightApi, GoRouter)> _mount(
  WidgetTester tester, {
  _FakeLightApi? api,
}) async {
  // Tall enough for the whole lazy list, band and recent section included.
  tester.view.physicalSize = const Size(1170, 4800);
  tester.view.devicePixelRatio = 3.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final light = api ?? _FakeLightApi();
  final router = GoRouter(
    initialLocation: '/home',
    routes: [
      GoRoute(path: '/home', builder: (_, _) => const LightHomeScreen()),
      for (final path in [
        '/light/topic/:entryId',
        '/app/billing',
        '/app/desktop',
        '/light/autopilot',
        '/assistant',
        '/app/billing/:tab',
      ])
        GoRoute(
          path: path,
          builder: (_, state) => Scaffold(
            body: Text('page ${state.uri.path}', key: const Key('page')),
          ),
        ),
    ],
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        authProvider.overrideWith(_Auth.new),
        activeWorkspaceProvider.overrideWith(_Workspace.new),
        lightApiProvider.overrideWithValue(light),
        billingBalanceProvider.overrideWith(
          (ref) async => const CreditBalance(
            workspaceId: 'w1',
            balance: '12.5',
            mode: 'enforce',
          ),
        ),
      ],
      child: MaterialApp.router(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        routerConfig: router,
      ),
    ),
  );
  await tester.pumpAndSettle();
  return (light, router);
}

void main() {
  testWidgets('the home draws the heading, every entry and the empty recent', (
    tester,
  ) async {
    await _mount(tester);
    expect(find.text('巷口小馆'), findsOneWidget);
    expect(find.textContaining('12.5'), findsOneWidget);
    expect(find.byKey(const Key('light-entry-video')), findsOneWidget);
    expect(find.text('做一条视频'), findsOneWidget);
    expect(find.text('15 秒省钱档约 4 积分'), findsOneWidget);
    expect(find.byKey(const Key('light-entry-menu')), findsOneWidget);
    expect(find.byKey(const Key('light-entry-poster')), findsOneWidget);
    // Free tier: the desktop entries are grey and say what is missing.
    expect(find.byKey(const Key('light-entry-douyin_publish')), findsOneWidget);
    expect(find.text('需要云电脑'), findsNWidgets(2));
    expect(find.byKey(const Key('light-entry-footage_edit')), findsOneWidget);
    expect(find.text('即将上线'), findsOneWidget);
    expect(find.text('所有事都能省心托管'), findsOneWidget);
    expect(find.byKey(const Key('light-recent-empty')), findsOneWidget);
    expect(find.byKey(const Key('light-ask')), findsOneWidget);
  });

  testWidgets('an available entry opens its topic page', (tester) async {
    final (_, router) = await _mount(tester);
    await tester.tap(find.byKey(const Key('light-entry-video')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/light/topic/video');
  });

  testWidgets('a grey entry on the free tier goes to the plans', (
    tester,
  ) async {
    final (_, router) = await _mount(tester);
    await tester.tap(find.byKey(const Key('light-entry-douyin_publish')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/app/billing');
  });

  testWidgets('a grey entry on a paid plan goes to the desktop', (
    tester,
  ) async {
    final api = _FakeLightApi()
      ..view = LightEntriesView.fromJson(_entriesJson(plan: 'pro'));
    final (_, router) = await _mount(tester, api: api);
    await tester.tap(find.byKey(const Key('light-entry-douyin_publish')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/app/desktop');
  });

  testWidgets('coming soon stays put; the band and the ask bar navigate', (
    tester,
  ) async {
    final (_, router) = await _mount(tester);
    await tester.tap(find.byKey(const Key('light-entry-footage_edit')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/home');
    await tester.tap(find.text('所有事都能省心托管'));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/light/autopilot');
    router.go('/home');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('light-ask')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/assistant');
  });

  testWidgets('recent groups list their results and open the chat', (
    tester,
  ) async {
    final api = _FakeLightApi()
      ..groups = [
        const LightRecentGroup(
          sessionId: 'sess-1',
          title: '周末探店',
          entryId: 'video',
          outputs: [
            LightRecentOutput(assetId: 'a1', mime: 'video/mp4', kind: 'final'),
            LightRecentOutput(assetId: 'a2', mime: 'image/png', kind: 'result'),
          ],
        ),
      ];
    final (_, _) = await _mount(tester, api: api);
    expect(find.text('周末探店'), findsOneWidget);
    expect(find.textContaining('2 个产出'), findsOneWidget);
    expect(find.byKey(const Key('light-recent-empty')), findsNothing);
  });

  testWidgets('a failed recent read offers a retry', (tester) async {
    final api = _FakeLightApi()..recentDown = true;
    await _mount(tester, api: api);
    expect(find.byKey(const Key('light-recent-failed')), findsOneWidget);
  });
}
