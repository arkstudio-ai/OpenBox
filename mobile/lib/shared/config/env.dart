/// Runtime environment, mirroring frontend-v2 `src/shared/config/env.ts`.
///
/// Override at build/run time with `--dart-define=API_BASE=http://host:8080`.
/// Note: the iOS simulator reaches the host's localhost directly; the Android
/// emulator must use `http://10.0.2.2:8080`.
abstract final class Env {
  static const String apiBase = String.fromEnvironment(
    'API_BASE',
    defaultValue: 'https://ai.bossipai.com.cn',
  );

  /// Public web origin used for shareable invitation links.
  static const String webBase = String.fromEnvironment(
    'WEB_BASE',
    defaultValue: 'https://ai.bossipai.com.cn',
  );

  /// Where Logto hands control back after the hosted sign-in page.
  ///
  /// A custom scheme rather than a URL: the browser sheet must reopen *this*
  /// app, and it has to be registered on the Logto native application for the
  /// authorize request to be accepted at all.
  static const String ssoRedirectUri = String.fromEnvironment(
    'SSO_REDIRECT_URI',
    defaultValue: 'com.bossip.bipmobile://callback',
  );

  /// Where Logto returns after ending its centralized browser session.
  /// This URI must also be listed under the Native application's Post sign-out
  /// redirect URIs. It intentionally shares the existing callback handler.
  static const String ssoPostLogoutRedirectUri = String.fromEnvironment(
    'SSO_POST_LOGOUT_REDIRECT_URI',
    defaultValue: 'com.bossip.bipmobile://callback',
  );

  /// WS origin derives from the HTTP origin (`http→ws`, `https→wss`),
  /// same as web `wsBase()`.
  static String get wsBase => apiBase.replaceFirst(RegExp('^http'), 'ws');

  /// The production origin the app ships against by default.
  static const String productionBase = 'https://ai.bossipai.com.cn';

  /// A build pointed anywhere else is a test build: the brand mark and the
  /// Light 「我的」 footer say so, with the host, so a tester can tell which
  /// backend a screenshot came from.
  static bool get isProduction => apiBase == productionBase;

  /// Host of [apiBase], for the test-build label.
  static String get apiHost => Uri.tryParse(apiBase)?.host ?? apiBase;
}
