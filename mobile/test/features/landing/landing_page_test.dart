import 'package:bossip_mobile/features/landing/landing_page.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

final _theme = ThemeData(
  extensions: [
    BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
  ],
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final lang in supportedLangs) {
    testWidgets(
      'landing uses the shipped $lang translations on a narrow phone',
      (tester) async {
        tester.view.physicalSize = const Size(360, 800);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        SharedPreferences.setMockInitialValues({'bossip:lang': lang});
        final prefs = await SharedPreferences.getInstance();
        final bundle = (await tester.runAsync(I18nBundle.load))!;
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
            ],
            child: MaterialApp(theme: _theme, home: const LandingPage()),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.textContaining('landing:'), findsNothing);
        expect(
          find.text(bundle.lookup(lang, 'landing', 'hero.title') as String),
          findsOneWidget,
        );
        final items =
            bundle.lookup(lang, 'landing', 'capabilities.items') as List;
        final lastFeature = items.last as Map<String, dynamic>;
        await tester.scrollUntilVisible(
          find.text(lastFeature['title'] as String),
          300,
        );
        expect(find.text(lastFeature['title'] as String), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('登录 opens sign-in and 注册新账号 opens sign-up', (tester) async {
    tester.view.physicalSize = const Size(360, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    final router = GoRouter(
      routes: [
        GoRoute(path: '/', builder: (context, state) => const LandingPage()),
        GoRoute(
          path: '/login',
          builder: (context, state) => const Scaffold(body: Text('sign-in')),
        ),
        GoRoute(
          path: '/register',
          builder: (context, state) => const Scaffold(body: Text('sign-up')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp.router(routerConfig: router, theme: _theme),
      ),
    );
    await tester.pumpAndSettle();

    final signIn = find.byKey(const Key('landing-sign-in'));
    final register = find.byKey(const Key('landing-register'));
    expect(find.descendant(of: signIn, matching: find.text('登录')), findsOne);
    expect(
      find.descendant(of: register, matching: find.text('注册新账号')),
      findsOne,
    );
    expect(find.text('立刻开始'), findsNothing);

    await tester.tap(signIn);
    await tester.pumpAndSettle();
    expect(find.text('sign-in'), findsOneWidget);

    router.go('/');
    await tester.pumpAndSettle();
    await tester.ensureVisible(register);
    await tester.tap(register);
    await tester.pumpAndSettle();
    expect(find.text('sign-up'), findsOneWidget);
  });
}
