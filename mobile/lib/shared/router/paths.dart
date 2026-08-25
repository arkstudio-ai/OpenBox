/// Route path constants, mirroring frontend-v2 `shared/router/paths.ts`.
/// Mobile addition: the workbench panel becomes a routed screen (`/app/w/…`).
abstract final class Paths {
  static const String landing = '/';
  static const String login = '/login';
  static const String register = '/register';
  static const String app = '/app';

  static String chat(String sessionId) => '/app/s/$sessionId';

  static String settings([String? tab]) =>
      tab == null ? '/app/settings' : '/app/settings?tab=$tab';

  static String workbench(String sessionId, {String tab = 'review'}) =>
      '/app/w/$sessionId?tab=$tab';
}
