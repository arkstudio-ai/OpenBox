import 'package:bossip_mobile/features/landing/landing_page.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

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
            child: MaterialApp(
              theme: ThemeData(
                extensions: [
                  BossipTokens.resolve(
                    BossipThemeName.default_,
                    Brightness.light,
                  ),
                ],
              ),
              home: const LandingPage(),
            ),
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
}
