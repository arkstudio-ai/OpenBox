import 'dart:ui' show Locale;

import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/appearance_store.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/utils/format.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('a device opens in its own language: zh, vi, else English', () {
    expect(I18nController.languageForDevice(const Locale('zh', 'CN')), 'zh-CN');
    expect(I18nController.languageForDevice(const Locale('zh', 'TW')), 'zh-CN');
    expect(I18nController.languageForDevice(const Locale('vi', 'VN')), 'vi-VN');
    expect(I18nController.languageForDevice(const Locale('vi')), 'vi-VN');
    expect(I18nController.languageForDevice(const Locale('en', 'GB')), 'en-US');
    expect(I18nController.languageForDevice(const Locale('fr')), 'en-US');
  });

  test(
    'a build default language stands in for the device until a pick',
    () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      I18nController make(String build) => I18nController(
        I18nBundle({}),
        prefs,
        systemLocale: () => const Locale('zh', 'CN'),
        buildLanguage: build,
      );
      final container = ProviderContainer(
        overrides: [i18nProvider.overrideWith(() => make('vi-VN'))],
      );
      addTearDown(container.dispose);
      final controller = container.read(i18nProvider.notifier);
      expect(container.read(i18nProvider).language, 'vi-VN');
      controller.refreshSystemLanguage();
      expect(container.read(i18nProvider).language, 'vi-VN');
      controller.setLanguage('zh-CN');
      expect(container.read(i18nProvider).language, 'zh-CN');
      controller.setLanguage(null);
      expect(container.read(i18nProvider).language, 'vi-VN');

      final unknown = ProviderContainer(
        overrides: [i18nProvider.overrideWith(() => make('xx'))],
      );
      addTearDown(unknown.dispose);
      expect(unknown.read(i18nProvider).language, 'zh-CN');
    },
  );

  test(
    'phone language wins over legacy account locale and follows changes',
    () async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      await I18nController.migrateLanguagePreference(prefs);
      var locale = const Locale('vi', 'VN');
      final container = ProviderContainer(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          i18nProvider.overrideWith(
            () => I18nController(
              I18nBundle({}),
              prefs,
              systemLocale: () => locale,
            ),
          ),
        ],
      );
      addTearDown(container.dispose);
      final controller = container.read(i18nProvider.notifier);
      container.read(appearanceProvider.notifier).hydrateFromServer({
        'extra': {'ui_language': 'zh-CN', 'locale': 'zh-CN'},
      });
      expect(container.read(i18nProvider).language, 'vi-VN');
      expect(container.read(i18nProvider).followsSystem, isTrue);
      locale = const Locale('en', 'GB');
      controller.refreshSystemLanguage();
      expect(container.read(i18nProvider).language, 'en-US');
      controller.setLanguage('en-US'); // A tap also pins the current language.
      await I18nController.migrateLanguagePreference(prefs);
      expect(prefs.getString('bossip:lang'), 'en-US');
      locale = const Locale('vi', 'VN');
      controller.refreshSystemLanguage();
      expect(container.read(i18nProvider).language, 'en-US');
      controller.setLanguage(null);
      expect(container.read(i18nProvider).language, 'vi-VN');
      expect(container.read(i18nProvider).followsSystem, isTrue);
    },
  );

  test('Vietnamese falls back to English; Chinese and English do not', () {
    final bundle = I18nBundle({
      'en-US': {
        'common': {'hello': 'Hello', 'onlyEn': 'Only English'},
      },
      'zh-CN': {
        'common': {'hello': '你好'},
      },
      'vi-VN': {
        'common': {'hello': 'Xin chào'},
      },
    });
    String t(String lang, String key) =>
        I18nState(language: lang, bundle: bundle).t(key);
    expect(t('vi-VN', 'common:hello'), 'Xin chào');
    expect(t('vi-VN', 'common:onlyEn'), 'Only English');
    // A Chinese key that is missing shows as the key: the locale gate, not a
    // fallback, keeps zh-CN and en-US complete.
    expect(t('zh-CN', 'common:onlyEn'), 'common:onlyEn');
    expect(t('en-US', 'common:missing'), 'common:missing');
  });

  testWidgets('a Vietnamese namespace missing on disk serves English whole', (
    tester,
  ) async {
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    // The real zh/en bundles are complete; vi-VN may have no files at all.
    expect(bundle.namespacesOf('zh-CN'), bundle.namespacesOf('en-US'));
    expect(
      bundle.namespacesOf('vi-VN'),
      everyElement(isIn(bundle.namespacesOf('en-US'))),
    );
    final vi = I18nState(language: 'vi-VN', bundle: bundle);
    expect(vi.t('light:tabs.home'), isNot('light:tabs.home'));
    expect(vi.t('common:action.retry'), isNot('common:action.retry'));
  });

  test('relative time is spelled out in the three languages', () {
    final now = DateTime(2026, 11, 8, 12);
    final past = now.subtract(const Duration(minutes: 5));
    final future = now.add(const Duration(days: 2));
    expect(formatRelative(past, 'zh-CN', now: now), '5分钟前');
    expect(formatRelative(past, 'en-US', now: now), '5 minutes ago');
    expect(formatRelative(past, 'vi-VN', now: now), '5 phút trước');
    expect(formatRelative(future, 'vi-VN', now: now), 'trong 2 ngày');
    expect(formatRelative(future, 'en-US', now: now), 'in 2 days');
  });
}
