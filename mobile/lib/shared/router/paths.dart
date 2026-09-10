/// Route path constants, mirroring frontend-v2 `shared/router/paths.ts`.
/// Mobile addition: the workbench panel becomes a routed screen (`/app/w/…`).
abstract final class Paths {
  static const String landing = '/';
  static const String login = '/login';
  static const String register = '/register';
  static const String app = '/app';

  static String invite(String token) => '/invite/${Uri.encodeComponent(token)}';

  static String loginFor(String destination) =>
      '$login?redirect=${Uri.encodeComponent(destination)}';

  static String authPeer(String path, Uri current) {
    final destination = current.queryParameters['redirect'];
    return destination == null
        ? path
        : '$path?redirect=${Uri.encodeComponent(destination)}';
  }

  static String postAuthDestination(Uri uri) {
    final destination = uri.queryParameters['redirect'];
    return destination != null && destination.startsWith('/')
        ? destination
        : app;
  }

  static String chat(String sessionId) => '/app/s/$sessionId';

  static const String cron = '/app/cron';

  static const String skills = '/app/skills';

  static const String admin = '/app/admin';
  static const String adminNotifications = '/app/admin/notifications';

  static const String desktop = '/app/desktop';

  static String authCenter({String? jobId}) => jobId == null
      ? '/app/auth-center'
      : '/app/auth-center?job=${Uri.encodeComponent(jobId)}';

  static String billing([String? tab]) =>
      tab == null ? '/app/billing' : '/app/billing/$tab';

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
