/// Route path constants, mirroring frontend-v2 `shared/router/paths.ts`.
/// Mobile addition: the workbench panel becomes a routed screen (`/app/w/…`).
abstract final class Paths {
  static const String landing = '/';
  static const String login = '/login';
  static const String register = '/register';
  static const String app = '/app';

  static String chat(String sessionId) => '/app/s/$sessionId';

  static const String cron = '/app/cron';

  static const String skills = '/app/skills';

  /// Optional project scope, like the web `paths.resources(projectId)`.
  static String resources([String? projectId]) => projectId == null
      ? '/app/resources'
      : '/app/resources?project=$projectId';

  static String settings([String? tab]) =>
      tab == null ? '/app/settings' : '/app/settings?tab=$tab';

  /// `tab` defaults to the panel's menu page; pass a surface to deep-link
  /// straight into it (the cron pill and chat's "审阅 →" both do).
  static String workbench(String sessionId, {String tab = 'menu'}) =>
      '/app/w/$sessionId?tab=$tab';
}
