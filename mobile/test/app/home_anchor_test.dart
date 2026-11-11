import 'package:bossip_mobile/app/home_anchor.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class _Mode extends UiModeController {
  _Mode(this.mode);
  final UiMode mode;
  @override
  UiModeState build() => UiModeState(userId: 'u1', mode: mode, loaded: true);
}

GoRoute _page(String path) => GoRoute(
  path: path,
  builder: (context, state) => Scaffold(body: Text('page ${state.uri.path}')),
);

/// The app's route shape in miniature: the Light tabs on a shell, the inner
/// pages beside it on the root navigator.
Future<GoRouter> _pump(
  WidgetTester tester, {
  required UiMode mode,
  String initial = '/home',
}) async {
  final router = HomeAnchoredRouter(
    light: () => mode == UiMode.light,
    initialLocation: initial,
    redirect: (context, state) => null,
    routes: [
      _page('/'),
      StatefulShellRoute.indexedStack(
        builder: (context, state, shell) => shell,
        branches: [
          StatefulShellBranch(routes: [_page('/home')]),
          StatefulShellBranch(routes: [_page('/inbox')]),
        ],
      ),
      _page('/app'),
      _page('/app/s/:id'),
      _page('/app/billing'),
      _page('/light/s/:id'),
    ],
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [uiModeProvider.overrideWith(() => _Mode(mode))],
      child: MaterialApp.router(routerConfig: router),
    ),
  );
  await tester.pumpAndSettle();
  return router;
}

String _at(GoRouter router) =>
    router.routerDelegate.currentConfiguration.uri.toString();

/// The system back gesture / button.
Future<bool> _back(WidgetTester tester) async {
  final handled = await tester.binding.handlePopRoute();
  await tester.pumpAndSettle();
  return handled;
}

void main() {
  test('inner pages hang off the Light home; tabs and homes do not', () {
    for (final location in [
      '/light/s/1',
      '/light/topic/video',
      '/light/autopilot',
      '/app/s/1',
      '/app/billing/usage',
      '/app/settings?tab=account',
      '/app/cron',
      '/app/auth-center?job=1',
    ]) {
      expect(anchorsOnLightHome(location), isTrue, reason: location);
    }
    for (final location in [
      '/',
      '/login',
      '/home',
      '/inbox',
      '/app',
      '/app/assistant?intro=all',
      '/app/inbox',
      '/app/voice',
      '/app/first-video/making',
    ]) {
      expect(anchorsOnLightHome(location), isFalse, reason: location);
    }
  });

  testWidgets('Light: going to a page from a tab keeps the tab beneath it', (
    tester,
  ) async {
    final router = await _pump(tester, mode: UiMode.light, initial: '/inbox');

    router.go('/light/s/1');
    await tester.pumpAndSettle();
    expect(find.text('page /light/s/1'), findsOneWidget);
    expect(router.canPop(), isTrue);

    // From that page on to another one: each back is one step up.
    router.go('/app/s/2');
    await tester.pumpAndSettle();
    expect(find.text('page /app/s/2'), findsOneWidget);

    expect(await _back(tester), isTrue);
    expect(find.text('page /light/s/1'), findsOneWidget);
    expect(await _back(tester), isTrue);
    expect(_at(router), '/inbox');
  });

  testWidgets('Light: going where you already are opens nothing twice', (
    tester,
  ) async {
    final router = await _pump(tester, mode: UiMode.light);
    router.go('/light/s/1');
    await tester.pumpAndSettle();
    router.go('/light/s/1');
    await tester.pumpAndSettle();
    expect(await _back(tester), isTrue);
    expect(_at(router), '/home');
  });

  testWidgets('Light: a tab or the home is still a plain go', (tester) async {
    final router = await _pump(tester, mode: UiMode.light);
    router.go('/app/billing');
    await tester.pumpAndSettle();
    router.go('/inbox');
    await tester.pumpAndSettle();
    expect(_at(router), '/inbox');
    expect(router.canPop(), isFalse);
  });

  testWidgets('Light: back on a page with nothing beneath opens the home', (
    tester,
  ) async {
    final router = await _pump(
      tester,
      mode: UiMode.light,
      initial: '/light/s/1',
    );
    expect(router.canPop(), isFalse);
    expect(await _back(tester), isTrue);
    expect(_at(router), '/home');
  });

  testWidgets('pro: go replaces, as it always has', (tester) async {
    final router = await _pump(tester, mode: UiMode.pro, initial: '/app');
    router.go('/app/s/1');
    await tester.pumpAndSettle();
    expect(_at(router), '/app/s/1');
    expect(router.canPop(), isFalse);
    // Nothing of ours holds the back gesture in the pro workspace.
    expect(await _back(tester), isFalse);
  });
}
