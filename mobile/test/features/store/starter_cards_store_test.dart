import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/onboarding/widgets/starter_cards.dart';
import 'package:bossip_mobile/features/store/models/store.dart';
import 'package:bossip_mobile/features/store/state/store_provider.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'store_test_support.dart';

void main() {
  late SharedPreferences prefs;
  late I18nBundle bundle;
  late Dio dio;
  late FakeAdapter adapter;

  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    adapter = FakeAdapter((o) async {
      if (o.path.endsWith('/starter-cards')) {
        return {
          'items': [
            {'title': '给招牌菜「果切拼盘」做一条 30 秒到店短视频', 'hint': '按门店人设生成'},
            {'title': '把这周 3 条差评写成回复', 'hint': '附一张致歉海报'},
          ],
          'personaStatus': 'active',
        };
      }
      return storeListJson();
    });
    dio.httpClientAdapter = adapter;
  });

  Widget app({
    required StoreSnapshot snapshot,
    Map<String, Object> onboarding = const {},
    required ValueChanged<String> onPick,
  }) => ProviderScope(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      apiDioProvider.overrideWithValue(dio),
      authProvider.overrideWith(SignedIn.new),
      onboardingProvider.overrideWith(() => LoadedOnboarding(onboarding)),
      storeProvider.overrideWith(() => FixedStore(snapshot)),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
    ],
    child: MaterialApp(
      theme: ThemeData(
        extensions: [
          BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
        ],
      ),
      home: Scaffold(body: StarterCards(onPick: onPick)),
    ),
  );

  testWidgets('with a store the cards come from the API, in the app language', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final picked = <String>[];
    final store = Store.fromJson(storeJson());
    await tester.pumpWidget(
      app(snapshot: StoreSnapshot(store: store), onPick: picked.add),
    );
    await tester.pump();
    // Before the API answers, the locale cards for the store's industry.
    expect(find.textContaining('新品上市'), findsOneWidget);
    expect(find.byKey(const Key('industry-food')), findsNothing);

    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 100)),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('果切拼盘'), findsOneWidget);
    expect(find.textContaining('新品上市'), findsNothing);
    final call = adapter.calls.single;
    expect(call.path, '/api/stores/st1/starter-cards');
    expect(call.query['locale'], 'zh-CN');

    await tester.tap(find.textContaining('果切拼盘'));
    expect(picked.single, contains('果切拼盘'));
  });

  testWidgets('without a store the remembered industry picks the locale cards', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    await tester.pumpWidget(
      app(
        snapshot: const StoreSnapshot(),
        onboarding: const {Guides.industryKey: 'retail'},
        onPick: (_) {},
      ),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('爆款商品'), findsOneWidget);
    expect(adapter.calls, isEmpty);
  });

  testWidgets('an `other` store reads the food cards until the API answers', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    dio.httpClientAdapter = FakeAdapter((_) async => throw StateError('down'));
    final store = Store.fromJson(storeJson(category: 'other'));
    await tester.pumpWidget(
      app(snapshot: StoreSnapshot(store: store), onPick: (_) {}),
    );
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 100)),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('新品上市'), findsOneWidget);
    // Dio dispatches the failure through a zero-length timer; let it fire.
    await tester.pump(const Duration(milliseconds: 1));
  });
}
