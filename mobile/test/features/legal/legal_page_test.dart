import 'package:bossip_mobile/features/legal/legal_page.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/legal/legal_links.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  for (final language in ['zh-CN', 'en-US']) {
    for (final brightness in [Brightness.light, Brightness.dark]) {
      testWidgets(
        'public documents work offline at 320px, $language $brightness',
        (tester) async {
          tester.view.physicalSize = const Size(320, 720);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          SharedPreferences.setMockInitialValues({'bossip:lang': language});
          final prefs = await SharedPreferences.getInstance();
          final bundle = (await tester.runAsync(I18nBundle.load))!;
          final i18n = I18nState(language: language, bundle: bundle);
          expect(i18n.t('legal:version'), legalVersion);
          final router = GoRouter(
            initialLocation: '/legal',
            routes: [
              GoRoute(path: '/legal', builder: (_, _) => const LegalPage()),
              GoRoute(
                path: '/legal/:doc',
                builder: (_, s) => LegalPage(document: s.pathParameters['doc']),
              ),
            ],
          );
          addTearDown(router.dispose);
          await tester.pumpWidget(
            ProviderScope(
              overrides: [
                i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
              ],
              // No auth or API providers are installed: all content is bundled.
              child: MaterialApp.router(
                routerConfig: router,
                theme: ThemeData(
                  brightness: brightness,
                  extensions: [
                    BossipTokens.resolve(BossipThemeName.default_, brightness),
                  ],
                ),
                builder: (context, child) => MediaQuery(
                  data: MediaQuery.of(
                    context,
                  ).copyWith(textScaler: const TextScaler.linear(1.3)),
                  child: child!,
                ),
              ),
            ),
          );
          await tester.pumpAndSettle();
          expect(find.text(i18n.t('legal:publicAccess')), findsOneWidget);
          expect(tester.takeException(), isNull);
          for (final document in legalDocuments) {
            router.go('/legal/$document');
            await tester.pumpAndSettle();
            final sections = i18n.tList('legal:documents.$document.sections');
            expect(
              find.text((sections.first as Map)['title'] as String),
              findsOneWidget,
            );
            await tester.drag(find.byType(ListView), const Offset(0, -1200));
            await tester.pumpAndSettle();
            expect(tester.takeException(), isNull);
          }
        },
      );
    }
  }
}
