/// Which interface the account uses (docs `LIGHT_MODE_PLAN.md` §4): `light`
/// is the four-tab home built for a shop owner, `pro` the full workspace with
/// its drawer. Deliberately not `ColorMode.light`, which is about colours.
library;

import 'json.dart';

enum UiMode { light, pro }

extension UiModeWire on UiMode {
  /// `user_preferences.extra.ui_mode` / `GET|PUT /api/light/mode`.
  String get wire => name;
}

/// Anything but an explicit `light` is `pro`: an account from before Light
/// existed has no value at all, and it must never be switched for them.
UiMode uiModeFromWire(String? value) =>
    value == 'light' ? UiMode.light : UiMode.pro;

/// `extra.ui_mode` of `GET /api/auth/me/preferences`.
UiMode uiModeFromPreferences(Map<String, dynamic> prefs) =>
    uiModeFromWire(asString(asMap(prefs['extra'])['ui_mode']));
