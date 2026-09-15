import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/onboarding/widgets/intro_banner_page.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('intro walks three screens and records the device flag', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    final visited = <String>[];
    final router = GoRouter(
      initialLocation: '/intro',
      routes: [
        GoRoute(path: '/intro', builder: (_, _) => const IntroBannerPage()),
        GoRoute(
          path: '/login',
          builder: (_, state) {
            visited.add(state.matchedLocation);
            return const Scaffold(body: Text('login'));
          },
        ),
        GoRoute(path: '/', builder: (_, _) => const Scaffold(body: Text('landing'))),
      ],
    );
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
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
    expect(find.text('随时在线的 AI 搭档'), findsOneWidget);
    expect(find.byKey(const Key('intro-skip')), findsOneWidget);
    await tester.tap(find.byKey(const Key('intro-next')));
    await tester.pumpAndSettle();
    expect(find.text('它在云电脑上替你操作'), findsOneWidget);
    await tester.tap(find.byKey(const Key('intro-next')));
    await tester.pumpAndSettle();
    expect(find.text('结果与提醒回到手机'), findsOneWidget);
    expect(find.byKey(const Key('intro-skip')), findsNothing);
    await tester.runAsync(() async {
      await tester.tap(find.byKey(const Key('intro-sign-in')));
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pumpAndSettle();
    expect(prefs.getBool(introSeenKey), isTrue);
    expect(visited, ['/login']);
  });
}
