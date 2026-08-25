import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/providers.dart';
import '../i18n/i18n.dart';
import '../models/json.dart';
import 'tokens.dart';
import 'type_scale.dart';

const _storageKey = 'bossip:appearance';

/// Mirrors frontend-v2 `shared/appearance/store.ts`: theme/mode/fontSize with
/// localStorage(SharedPreferences) persistence and best-effort server sync to
/// `PUT /api/auth/me/preferences` `{theme, extra: {mode, fontSize, locale}}`.
class AppearanceState {
  const AppearanceState({
    this.theme = BossipThemeName.default_,
    this.mode = ColorMode.system,
    this.fontSize = UiFontSize.base,
  });

  final BossipThemeName theme;
  final ColorMode mode;
  final UiFontSize fontSize;

  AppearanceState copyWith({
    BossipThemeName? theme,
    ColorMode? mode,
    UiFontSize? fontSize,
  }) =>
      AppearanceState(
        theme: theme ?? this.theme,
        mode: mode ?? this.mode,
        fontSize: fontSize ?? this.fontSize,
      );
}

class AppearanceController extends Notifier<AppearanceState> {
  @override
  AppearanceState build() {
    final raw = ref.read(prefsProvider).getString(_storageKey);
    if (raw == null) return const AppearanceState();
    try {
      final json = jsonDecode(raw) as Map<String, dynamic>;
      return AppearanceState(
        theme: BossipThemeNameWire.parse(asString(json['theme'])),
        mode: ColorModeX.parse(asString(json['mode'])),
        fontSize: UiFontSizeX.parse(asString(json['fontSize'])),
      );
    } on FormatException {
      return const AppearanceState();
    }
  }

  void setTheme(BossipThemeName theme) => _apply(state.copyWith(theme: theme));

  void setMode(ColorMode mode) => _apply(state.copyWith(mode: mode));

  void setFontSize(UiFontSize fontSize) => _apply(state.copyWith(fontSize: fontSize));

  void setLanguage(String lang) {
    ref.read(i18nProvider.notifier).setLanguage(lang);
    _syncServer();
  }

  /// Applies server prefs after sign-in (web `hydrateFromServer`).
  void hydrateFromServer(Map<String, dynamic> prefs) {
    final extra = asMap(prefs['extra']);
    var next = state;
    final theme = asString(prefs['theme']);
    if (theme != null) next = next.copyWith(theme: BossipThemeNameWire.parse(theme));
    final mode = asString(extra['mode']);
    if (mode != null) next = next.copyWith(mode: ColorModeX.parse(mode));
    final fontSize = asString(extra['fontSize']);
    if (fontSize != null) {
      next = next.copyWith(fontSize: UiFontSizeX.parse(fontSize));
    }
    final locale = asString(extra['locale']);
    if (locale != null) ref.read(i18nProvider.notifier).setLanguage(locale);
    state = next;
    _persistLocal();
  }

  void _apply(AppearanceState next) {
    state = next;
    _persistLocal();
    _syncServer();
  }

  void _persistLocal() {
    ref.read(prefsProvider).setString(
          _storageKey,
          jsonEncode({
            'theme': state.theme.wire,
            'mode': state.mode.wire,
            'fontSize': state.fontSize.wire,
          }),
        );
  }

  Future<void> _syncServer() async {
    try {
      await ref.read(apiDioProvider).put<dynamic>(
        '/api/auth/me/preferences',
        data: {
          'theme': state.theme.wire,
          'extra': {
            'mode': state.mode.wire,
            'fontSize': state.fontSize.wire,
            'locale': ref.read(i18nProvider).language,
          },
        },
      );
    } catch (_) {
      // Best-effort, same as web.
    }
  }
}

final appearanceProvider =
    NotifierProvider<AppearanceController, AppearanceState>(
  AppearanceController.new,
);
