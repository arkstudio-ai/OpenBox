import 'dart:async';

import 'package:bossip_mobile/app/home_anchor.dart';
import 'package:bossip_mobile/features/light/widgets/light_shell.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('side back stays inside Light while tabs mount and load', (
    tester,
  ) async {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    const tabs = [Paths.home, Paths.lightAssistant, Paths.lightInbox, Paths.me];
    final platformHandling = <bool>[];
    final messenger = tester.binding.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'SystemNavigator.setFrameworkHandlesBack') {
        platformHandling.add(call.arguments as bool);
      }
      return null;
    });
    addTearDown(
      () => messenger.setMockMethodCallHandler(SystemChannels.platform, null),
    );
    final router = GoRouter(
      initialLocation: Paths.home,
      routes: [
        StatefulShellRoute.indexedStack(
          builder: (_, _, shell) => LightShell(shell: shell),
          branches: [
            for (final tab in tabs)
              StatefulShellBranch(
                routes: [
                  GoRoute(
                    path: tab,
                    builder: (_, _) => Scaffold(
                      body: tab == Paths.home
                          ? const Text('Home body')
                          : const CircularProgressIndicator(),
                    ),
                  ),
                ],
              ),
          ],
        ),
        GoRoute(
          path: '/detail',
          builder: (_, _) => const Scaffold(body: Text('Detail body')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp.router(
          routerConfig: router,
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          onNavigationNotification: (notification) =>
              reportFrameworkBack(router, notification),
        ),
      ),
    );
    await tester.pump();

    for (final tab in tabs.skip(1)) {
      platformHandling.clear();
      router.go(tab);
      await tester.pump();
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(platformHandling, isNotEmpty);
      expect(
        platformHandling,
        everyElement(isTrue),
        reason: 'first frame of $tab',
      );
      expect(await tester.binding.handlePopRoute(), isTrue);
      await tester.pump();
      // A second gesture may already be in progress while Home is mounting.
      expect(await tester.binding.handlePopRoute(), isTrue);
      await tester.pump();
      expect(router.routerDelegate.currentConfiguration.uri.path, Paths.home);
    }

    router.go(Paths.me);
    await tester.pump();
    unawaited(router.push<void>('/detail'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.text('Detail body'), findsOneWidget);
    expect(await tester.binding.handlePopRoute(), isTrue);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(router.routerDelegate.currentConfiguration.uri.path, Paths.me);

    router.go('/detail');
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(platformHandling.last, isFalse);
  });
}
