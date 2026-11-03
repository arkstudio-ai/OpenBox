import '../models/first_video.dart';

/// Route path constants, mirroring frontend-v2 `shared/router/paths.ts`.
/// Mobile addition: the workbench panel becomes a routed screen (`/app/w/…`).
abstract final class Paths {
  static const String landing = '/';
  static const String login = '/login';
  static const String register = '/register';
  static const String app = '/app';
  static const String assistant = '/app/assistant';

  /// The personal assistant with its first meeting open again, every question
  /// asked (Settings' "重新认识一下").
  static const String assistantIntro = '/app/assistant?intro=all';

  /// Mobile-only: the full-screen voice call with the personal assistant.
  /// Popping it collapses the call into the top call bar; it keeps going.
  static const String voice = '/app/voice';
  static const String legal = '/legal';
  static String legalDocument(String id) =>
      const ['collection', 'third-parties', 'permissions'].contains(id)
      ? '$legal/privacy/$id'
      : '$legal/$id';

  /// Mobile-only: install-level intro banner shown before the landing page.
  static const String intro = '/intro';

  static String invite(String token) => '/invite/${Uri.encodeComponent(token)}';

  static String loginFor(String destination) =>
      '$login?redirect=${Uri.encodeComponent(destination)}';

  static String authPeer(String path, Uri current) {
    final destination = current.queryParameters['redirect'];
    return destination == null
        ? path
        : '$path?redirect=${Uri.encodeComponent(destination)}';
  }

  /// Where a sign-in lands. A deep link's `redirect` keeps its destination;
  /// otherwise the account's first-video state decides (docs
  /// `FIRST_VIDEO.md`): offered at sign-up and untouched → the guided flow,
  /// in flight → its making page, anything else (including accounts from
  /// before the flow, which have no record) → the chat home.
  static String postAuthDestination(
    Uri uri, {
    FirstVideoEntry entry = FirstVideoEntry.settled,
  }) {
    final destination = uri.queryParameters['redirect'];
    if (destination != null && destination.startsWith('/')) return destination;
    return switch (entry) {
      FirstVideoEntry.fresh => firstVideo,
      FirstVideoEntry.making => firstVideoMaking,
      FirstVideoEntry.settled => app,
    };
  }

  /// Mobile-only guided first video (Light 模式 M1). Root-navigator routes
  /// outside the workspace shell; `firstVideo` itself forwards to the first
  /// screen.
  static const String firstVideo = '/app/first-video';
  static const String firstVideoShop = '/app/first-video/shop';
  static const String firstVideoTemplate = '/app/first-video/template';
  static const String firstVideoPhoto = '/app/first-video/photo';
  static const String firstVideoMaking = '/app/first-video/making';
  static const String firstVideoResult = '/app/first-video/result';

  static String chat(String sessionId) => '/app/s/$sessionId';

  static const String cron = '/app/cron';

  static const String skills = '/app/skills';

  static const String admin = '/app/admin';
  static const String adminNotifications = '/app/admin/notifications';

  static const String desktop = '/app/desktop';

  /// Message centre (web `paths.inbox`); `topic` is the in-app topic page
  /// behind a first-party notice (web serves it publicly at `/topics/:slug`).
  static const String inbox = '/app/inbox';

  static String topic(String slug) =>
      '/app/topics/${Uri.encodeComponent(slug)}';

  /// Kept for old links; it opens the knowledge page on its memories.
  static const String memory = '/app/memory';

  /// The knowledge page (知识库): memories, topics and files in one place.
  static String wiki({String? projectId, String? view}) {
    final query = {
      if (projectId != null && projectId.isNotEmpty) 'project': projectId,
      if (view != null && view.isNotEmpty) 'view': view,
    };
    return Uri(
      path: '/app/wiki',
      queryParameters: query.isEmpty ? null : query,
    ).toString();
  }

  /// One topic or document page of the knowledge page.
  static String wikiPage(String pageId, {String? projectId}) => Uri(
    path: '/app/wiki/${Uri.encodeComponent(pageId)}',
    queryParameters: projectId == null || projectId.isEmpty
        ? null
        : {'project': projectId},
  ).toString();

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
  ///
  /// `control` (desktop only) switches input control on as soon as the stream
  /// is up — a takeover card's link, mirroring web `paths.desktopTakeover`.
  static String workbench(
    String sessionId, {
    String tab = 'menu',
    bool control = false,
  }) => '/app/w/$sessionId?tab=$tab${control ? '&control=1' : ''}';
}
