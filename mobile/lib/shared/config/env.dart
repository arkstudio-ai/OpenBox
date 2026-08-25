/// Runtime environment, mirroring frontend-v2 `src/shared/config/env.ts`.
///
/// Override at build/run time with `--dart-define=API_BASE=http://host:8080`.
/// Note: the iOS simulator reaches the host's localhost directly; the Android
/// emulator must use `http://10.0.2.2:8080`.
abstract final class Env {
  static const String apiBase = String.fromEnvironment(
    'API_BASE',
    defaultValue: 'http://localhost:8080',
  );

  /// WS origin derives from the HTTP origin (`http→ws`, `https→wss`),
  /// same as web `wsBase()`.
  static String get wsBase => apiBase.replaceFirst(RegExp('^http'), 'ws');
}
