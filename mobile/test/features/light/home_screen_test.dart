import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/state/assistant_overview.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/home_screen.dart';
import 'package:bossip_mobile/features/light/widgets/entry_card.dart';
import 'package:bossip_mobile/features/store/api/store_api.dart';
import 'package:bossip_mobile/features/store/models/store.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/light.dart';
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

class _FakeStoreApi extends StoreApi {
  _FakeStoreApi() : super(Dio());

  Store? store = const Store(id: 'sto1', name: '巷口小馆');
  @override
  Future<Store?> current() async => store;
}

class _FakeLightApi extends LightApi {
  _FakeLightApi() : super(Dio());

  LightEntriesView view = LightEntriesView.fromJson(_entriesJson());
  int entriesCalls = 0;

  @override
  Future<LightEntriesView> entries() async {
    entriesCalls++;
    return view;
  }
}

Map<String, dynamic> _entriesJson({
  String plan = 'free',
  String videoAvailability = 'ok',
}) => {
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
      'availability': videoAvailability,
    },
    {
      'id': 'voice_assistant',
      'title': '语音助手',
      'subtitle': '打个电话，说一句就办',
      'kind': 'primary',
      'action': 'voice_call',
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
      'kind': 'primary',
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
  ],
};

Future<(_FakeLightApi, GoRouter)> _mount(
  WidgetTester tester, {
  _FakeLightApi? api,
  List<Override> overrides = const [],
  _FakeStoreApi? store,
}) async {
  // Mount all rows so their visibility and ordering can be inspected.
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
        '/light/productions',
        '/app/s/:id',
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
        storeApiProvider.overrideWithValue(store ?? _FakeStoreApi()),
        billingBalanceProvider.overrideWith(
          (ref) async => const CreditBalance(
            workspaceId: 'w1',
            balance: '12.5',
            mode: 'enforce',
          ),
        ),
        ...overrides,
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
  testWidgets('switching the interface language refetches the entries', (
    tester,
  ) async {
    final (api, _) = await _mount(tester);
    final before = api.entriesCalls;
    expect(find.text('做一条视频'), findsOneWidget);
    // The server answers in the request's language; the fake swaps the
    // title so the refetch is visible.
    api.view = LightEntriesView.fromJson({
      ..._entriesJson(),
      'entries': [
        for (final entry in _entriesJson()['entries'] as List)
          if ((entry as Map)['id'] == 'video')
            {...entry.cast<String, dynamic>(), 'title': 'Làm một video'}
          else
            entry,
      ],
    });
    final element = tester.element(find.byType(LightHomeScreen));
    ProviderScope.containerOf(
      element,
    ).read(i18nProvider.notifier).setLanguage('vi-VN');
    await tester.pumpAndSettle();
    expect(api.entriesCalls, before + 1);
    expect(find.text('Làm một video'), findsOneWidget);
    expect(find.text('做一条视频'), findsNothing);
  });

  testWidgets('production and assistant have distinct entries', (tester) async {
    final (_, router) = await _mount(tester);
    expect(find.text('巷口小馆'), findsOneWidget);
    expect(find.textContaining('12.5'), findsOneWidget);
    expect(find.text('内容制作'), findsOneWidget);
    expect(find.text('个人助理'), findsOneWidget);
    expect(find.text('店里的日常，交给我'), findsOneWidget);

    final cards = tester.widgetList<LightEntryCard>(
      find.byType(LightEntryCard),
    );
    expect(cards.map((card) => card.entry.id), [
      'video',
      'footage_edit',
      'menu',
    ]);
    expect(find.byKey(const Key('light-entry-poster')), findsNothing);
    expect(find.text('所有事都能省心托管'), findsNothing);
    expect(find.byKey(const Key('light-assistant-recent')), findsNothing);
    await tester.tap(find.byKey(const Key('light-production-history')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/light/productions');
  });

  testWidgets('the production cards open their existing workflows', (
    tester,
  ) async {
    final (_, router) = await _mount(tester);
    for (final entry in ['video', 'footage_edit', 'menu']) {
      await tester.tap(find.byKey(Key('light-entry-$entry')));
      await tester.pumpAndSettle();
      expect(router.state.uri.path, '/light/topic/$entry');
      router.pop();
      await tester.pumpAndSettle();
    }
  });

  for (final (plan, path) in [
    ('free', '/app/billing'),
    ('pro', '/app/desktop'),
  ]) {
    testWidgets('a gated production card on $plan opens its access action', (
      tester,
    ) async {
      final api = _FakeLightApi()
        ..view = LightEntriesView.fromJson(
          _entriesJson(plan: plan, videoAvailability: 'needs_desktop'),
        );
      final (_, router) = await _mount(tester, api: api);
      await tester.tap(find.byKey(const Key('light-entry-video')));
      await tester.pumpAndSettle();
      expect(router.state.uri.path, path);
    });
  }

  testWidgets('schedule opens and assistant shortcuts pass an editable draft', (
    tester,
  ) async {
    final (_, router) = await _mount(tester);
    await tester.tap(find.byKey(const Key('light-home-schedules')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/light/autopilot');
    router.pop();
    await tester.pumpAndSettle();

    for (final (id, text) in [('reviews', '回复草稿'), ('copy', '活动文案')]) {
      await tester.tap(find.byKey(Key('light-assistant-prompt-$id')));
      await tester.pumpAndSettle();
      expect(router.state.uri.path, '/assistant');
      expect((router.state.extra! as ComposerDraft).text, contains(text));
      router.go('/home');
      await tester.pumpAndSettle();
    }
    await tester.tap(find.byKey(const Key('light-ask')));
    await tester.pumpAndSettle();
    expect(router.state.uri.path, '/assistant');
    expect(router.state.extra, isNull);
  });

  testWidgets(
    'recent assistant work uses successful results and excludes productions',
    (tester) async {
      const scope = (userId: 'u1', workspaceId: 'w1');
      Map<String, dynamic> task(
        String id,
        String title,
        String outcome, {
        String? entry,
      }) => {
        'task': {
          'id': id,
          'title': title,
          'execution_session_id': 'session-$id',
        },
        'latest_result': {
          'outcome': outcome,
          'created_at': '2026-11-20T01:00:00Z',
        },
        'execution_session': {'light_entry': ?entry},
      };
      final (_, router) = await _mount(
        tester,
        overrides: [
          assistantScopeProvider.overrideWithValue(scope),
          assistantOverviewProvider(scope).overrideWith(
            (ref) async => AssistantSnapshot({
              'tasks': [
                task('done', '写好 3 条评价回复', 'succeeded'),
                task('production', '麻辣小龙虾成片', 'succeeded', entry: 'video'),
                task('failed', '活动文案失败', 'error'),
              ],
            }),
          ),
        ],
      );
      expect(find.text('最近帮你办的'), findsOneWidget);
      expect(find.text('写好 3 条评价回复'), findsOneWidget);
      expect(find.text('麻辣小龙虾成片'), findsNothing);
      expect(find.text('活动文案失败'), findsNothing);
      await tester.tap(find.byKey(const Key('light-assistant-recent-done')));
      await tester.pumpAndSettle();
      expect(router.state.uri.path, '/app/s/session-done');
    },
  );

  testWidgets('without a store the heading falls back to the person', (
    tester,
  ) async {
    await _mount(tester, store: _FakeStoreApi()..store = null);
    expect(find.text('alice'), findsOneWidget);
  });
}
