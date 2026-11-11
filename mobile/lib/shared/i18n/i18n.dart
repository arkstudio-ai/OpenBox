import 'dart:convert';
import 'dart:ui' show Locale, PlatformDispatcher;

import 'package:flutter/foundation.dart' show FlutterError;
import 'package:flutter/services.dart' show AssetManifest, rootBundle;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Lightweight i18next-compatible i18n, mirroring frontend-v2
/// `shared/i18n/index.ts`: namespaces = features, keys `ns:block.element`,
/// plurals `_one`/`_other`, interpolation `{{var}}`.
///
/// The locale JSON files under assets/locales/ are copied VERBATIM from
/// frontend-v2/src/locales — keep them in sync, never edit independently.
const supportedLangs = ['zh-CN', 'en-US', 'vi-VN'];

/// Where a language looks when it lacks a key (docs `LIGHT_MODE_I18N_PLAN.md`
/// §2.2): Vietnamese may show English; Chinese and English look nowhere, so a
/// missing key shows as the key and the locale gate (`scripts/check_locales`)
/// is what keeps them complete.
const langFallbacks = <String, List<String>>{
  'vi-VN': ['en-US'],
};

/// The languages whose bundle files may be missing on disk: those with a
/// fallback, which then serves the whole namespace.
bool _bundleOptional(String lang) => langFallbacks[lang]?.isNotEmpty ?? false;

const _langStorageKey = 'bossip:lang';

/// Mobile-only namespaces live under assets/locales-mobile/ and are NOT
/// mirrored from the web bundle (scripts/check_locales.sh ignores them).
const _mobileNamespaces = ['onboarding', 'first_video', 'light'];

const _namespaces = [
  'admin',
  'admin-billing',
  'admin-messages',
  'admin-skills',
  'auth',
  'auth-center',
  'billing',
  'chat',
  'common',
  'cron',
  'errors',
  'inbox',
  'jobs',
  'knowledge',
  'landing',
  'memory',
  'legal',
  'resources',
  'settings',
  'skills',
  'voice',
  'workbench',
  'workspace',
  'wiki',
];

class I18nBundle {
  I18nBundle(this._data);

  /// lang → namespace → parsed JSON tree.
  final Map<String, Map<String, dynamic>> _data;

  static Future<I18nBundle> load() async {
    final data = <String, Map<String, dynamic>>{};
    // The manifest says which files shipped; a missing file is skipped (or
    // refused) up front, because asking rootBundle for an absent asset reports
    // its error outside the awaiting future and never resolves it.
    final shipped = (await AssetManifest.loadFromAssetBundle(
      rootBundle,
    )).listAssets().toSet();
    for (final lang in supportedLangs) {
      final byNs = <String, dynamic>{};
      for (final ns in _namespaces) {
        final raw = await _read('assets/locales/$lang/$ns.json', lang, shipped);
        if (raw != null) byNs[ns] = jsonDecode(raw);
      }
      for (final ns in _mobileNamespaces) {
        final raw = await _read(
          'assets/locales-mobile/$lang/$ns.json',
          lang,
          shipped,
        );
        if (raw != null) byNs[ns] = jsonDecode(raw);
      }
      data[lang] = byNs;
    }
    return I18nBundle(data);
  }

  /// A bundle file; null when [lang] may lack it. A language without a
  /// fallback has nothing to show instead, so its missing file is an error.
  static Future<String?> _read(
    String asset,
    String lang,
    Set<String> shipped,
  ) async {
    if (shipped.contains(asset)) return rootBundle.loadString(asset);
    if (_bundleOptional(lang)) return null;
    throw FlutterError('Locale file missing from the bundle: $asset');
  }

  /// Which namespaces [lang] has on disk (tests; the fallback fills the rest).
  Set<String> namespacesOf(String lang) => _data[lang]?.keys.toSet() ?? {};

  dynamic lookup(String lang, String namespace, String path) {
    dynamic node = _data[lang]?[namespace];
    for (final segment in path.split('.')) {
      if (node is Map<String, dynamic>) {
        node = node[segment];
      } else {
        return null;
      }
    }
    return node;
  }
}

class I18nState {
  const I18nState({required this.language, required this.bundle});

  final String language;
  final I18nBundle bundle;

  Locale get locale {
    final parts = language.split('-');
    return Locale(parts[0], parts.length > 1 ? parts[1] : null);
  }

  /// Translate `ns:path.to.key`. With [count], tries `key_one`/`key_other`
  /// per CLDR (zh has only `other`); [vars] fills `{{name}}` slots
  /// (`count` is implicitly available).
  String t(String key, {Map<String, Object?>? vars, int? count}) {
    final raw = _resolve(key, count: count);
    if (raw is! String) return key;
    var out = raw;
    final allVars = <String, Object?>{...?vars, 'count': ?count};
    allVars.forEach((name, value) {
      out = out.replaceAll('{{$name}}', '$value');
    });
    return out;
  }

  /// For `returnObjects: true` arrays (e.g. workspace:suggestions).
  List<dynamic> tList(String key) {
    final raw = _resolve(key);
    return raw is List ? raw : const [];
  }

  dynamic _resolve(String key, {int? count}) {
    final colon = key.indexOf(':');
    final namespace = colon == -1 ? 'common' : key.substring(0, colon);
    final path = colon == -1 ? key : key.substring(colon + 1);
    for (final lang in [language, ...?langFallbacks[language]]) {
      if (count != null) {
        final suffix = count == 1 && lang.startsWith('en') ? '_one' : '_other';
        final plural = bundle.lookup(lang, namespace, '$path$suffix');
        if (plural != null) return plural;
      }
      final exact = bundle.lookup(lang, namespace, path);
      if (exact != null) return exact;
    }
    return null;
  }
}

class I18nController extends Notifier<I18nState> {
  I18nController(this._bundle, this._prefs);

  final I18nBundle _bundle;
  final SharedPreferences _prefs;

  @override
  I18nState build() =>
      I18nState(language: _detectInitialLanguage(), bundle: _bundle);

  String _detectInitialLanguage() {
    final stored = _prefs.getString(_langStorageKey);
    if (stored != null && supportedLangs.contains(stored)) return stored;
    return languageForDevice(PlatformDispatcher.instance.locale);
  }

  /// The language a device locale opens in: Chinese and Vietnamese by their
  /// own, everything else English.
  static String languageForDevice(Locale locale) =>
      switch (locale.languageCode) {
        'zh' => 'zh-CN',
        'vi' => 'vi-VN',
        _ => 'en-US',
      };

  void setLanguage(String lang) {
    if (!supportedLangs.contains(lang) || lang == state.language) return;
    state = I18nState(language: lang, bundle: _bundle);
    _prefs.setString(_langStorageKey, lang);
  }
}

/// Overridden in main() with the loaded bundle + prefs.
final i18nProvider = NotifierProvider<I18nController, I18nState>(
  () => throw UnimplementedError('i18nProvider must be overridden in main()'),
);
