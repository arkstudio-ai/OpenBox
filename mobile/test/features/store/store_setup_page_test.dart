import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/store/api/store_api.dart';
import 'package:bossip_mobile/features/store/models/store.dart';
import 'package:bossip_mobile/features/store/widgets/store_setup_page.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'store_test_support.dart';

void main() {
  late SharedPreferences prefs;
  late I18nBundle bundle;
  late Dio dio;
  late FakeAdapter adapter;
  late Map<String, dynamic> onboarding;

  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
    onboarding = {};
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    adapter = FakeAdapter(
      (o) async {
        if (o.method == 'PUT' && o.path == '/api/auth/me/preferences') {
          onboarding = Map<String, dynamic>.from(
            (o.data as Map)['onboarding'] as Map,
          );
          return {'onboarding': onboarding};
        }
        if (o.path == '/api/stores' && o.method == 'POST') {
          final body = o.data as Map;
          return storeJson(
            name: body['name'] as String,
            category: body['category'] as String,
            platforms: (body['main_platforms'] as List).cast<String>(),
          );
        }
        if (o.path == '/api/stores') return storeListJson();
        return {'onboarding': onboarding};
      },
      status: (o) => o.method == 'POST' ? 201 : 200,
    );
    dio.httpClientAdapter = adapter;
  });

  Widget app() => ProviderScope(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      apiDioProvider.overrideWithValue(dio),
      authProvider.overrideWith(SignedIn.new),
      activeWorkspaceProvider.overrideWith(OneWorkspace.new),
      onboardingProvider.overrideWith(LoadedOnboarding.new),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
    ],
    child: MaterialApp.router(
      theme: ThemeData(
        extensions: [
          BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
        ],
      ),
      routerConfig: GoRouter(
        initialLocation: Paths.storeSetup,
        routes: [
          GoRoute(
            path: Paths.app,
            builder: (_, _) => const Scaffold(body: Text('home')),
          ),
          GoRoute(
            path: Paths.storeSetup,
            builder: (_, _) => const StoreSetupPage(),
          ),
        ],
      ),
    ),
  );

  Future<void> settle(WidgetTester tester) async {
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 100)),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('save posts the store and marks the step seen', (tester) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    await tester.pumpWidget(app());
    await settle(tester);

    expect(find.text('你的店'), findsOneWidget);
    // Only the open category can be picked; the others say so.
    final beauty = tester.widget<ChoiceChip>(
      find.byKey(const Key('store-category-beauty')),
    );
    expect(beauty.onSelected, isNull);
    expect(find.textContaining('即将支持'), findsNWidgets(2));
    expect(
      tester
          .widget<ChoiceChip>(find.byKey(const Key('store-category-food')))
          .selected,
      isTrue,
    );
    // Nothing to save without a name.
    expect(
      tester.widget<FilledButton>(find.byKey(const Key('store-save'))).onPressed,
      isNull,
    );

    await tester.enterText(find.byKey(const Key('store-name')), ' 泽岚鲜果 ');
    await tester.tap(find.byKey(const Key('store-platform-douyin_laike')));
    await tester.pump();
    // Toggling off works too: only what is still selected is sent.
    await tester.tap(find.byKey(const Key('store-platform-meituan_merchant')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('store-platform-meituan_merchant')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('store-save')));
    await settle(tester);

    final post = adapter.calls.singleWhere((c) => c.method == 'POST');
    expect(post.path, '/api/stores');
    expect(post.data, {
      'name': '泽岚鲜果',
      'category': 'food',
      'main_platforms': ['douyin_laike'],
    });
    expect(onboarding[Guides.storeSetup], isTrue);
    expect(find.text('home'), findsOneWidget);
  });

  testWidgets('skip leaves without posting and still marks the step seen', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    await tester.pumpWidget(app());
    await settle(tester);

    await tester.tap(find.byKey(const Key('store-skip')));
    await settle(tester);

    expect(adapter.calls.where((c) => c.method == 'POST'), isEmpty);
    expect(onboarding[Guides.storeSetup], isTrue);
    expect(find.text('home'), findsOneWidget);
  });

  test('the store API sends the documented bodies and reads the catalogue', () async {
    final api = StoreApi(dio);
    final snapshot = await api.list();
    expect(snapshot.hasStore, isFalse);
    expect(snapshot.openCategories, ['food']);
    expect(snapshot.platforms, ['douyin_laike', 'meituan_merchant']);
    final created = await api.create(
      name: 'A',
      category: 'food',
      mainPlatforms: const ['meituan_merchant'],
    );
    expect(created.mainPlatforms, ['meituan_merchant']);
    expect(created.starterIndustry, 'food');
    expect(Store.fromJson(storeJson(category: 'other')).starterIndustry, 'food');
  });
}
