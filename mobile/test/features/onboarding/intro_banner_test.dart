import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/onboarding/widgets/intro_banner_page.dart';
import 'package:bossip_mobile/features/onboarding/widgets/intro_sample.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Mounts the intro on its own route. Returns the prefs and the auth screens
/// the intro opened.
Future<(SharedPreferences, List<String>)> _mountIntro(
  WidgetTester tester,
) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final visited = <String>[];
  Widget auth(GoRouterState state) {
    visited.add(state.matchedLocation);
    return Scaffold(body: Text(state.matchedLocation));
  }

  final router = GoRouter(
    initialLocation: '/intro',
    routes: [
      GoRoute(path: '/intro', builder: (_, _) => const IntroBannerPage()),
      GoRoute(path: '/login', builder: (_, state) => auth(state)),
      GoRoute(path: '/register', builder: (_, state) => auth(state)),
      GoRoute(
        path: '/',
        builder: (_, _) => const Scaffold(body: Text('landing')),
      ),
    ],
  );
  addTearDown(router.dispose);
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
  await tester.pump();
  expect(find.text('你的店，也该有一条自己的视频'), findsOneWidget);
  // One screen: no pager, no skip.
  expect(find.byKey(const Key('intro-next')), findsNothing);
  expect(find.byKey(const Key('intro-skip')), findsNothing);
  // The sample slot is on screen (poster until a clip ships).
  expect(find.byType(IntroSample), findsOneWidget);
  return (prefs, visited);
}

Future<void> _tapAndSettle(WidgetTester tester, Key key) async {
  await tester.runAsync(() async {
    await tester.tap(find.byKey(key));
    await Future<void>.delayed(const Duration(milliseconds: 50));
  });
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
}

void main() {
  testWidgets('the one button makes a video: it opens sign-up', (tester) async {
    final (prefs, visited) = await _mountIntro(tester);
    expect(
      find.descendant(
        of: find.byKey(const Key('intro-register')),
        matching: find.text('免费做一条你的店的视频'),
      ),
      findsOne,
    );
    await _tapAndSettle(tester, const Key('intro-register'));
    expect(prefs.getBool(introSeenKey), isTrue);
    expect(visited, ['/register']);
  });

  testWidgets('已有账号 goes straight to sign-in', (tester) async {
    final (prefs, visited) = await _mountIntro(tester);
    expect(
      find.descendant(
        of: find.byKey(const Key('intro-sign-in')),
        matching: find.text('已有账号，直接进入'),
      ),
      findsOne,
    );
    await _tapAndSettle(tester, const Key('intro-sign-in'));
    expect(prefs.getBool(introSeenKey), isTrue);
    expect(visited, ['/login']);
  });
}
