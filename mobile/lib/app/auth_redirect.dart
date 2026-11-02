import '../shared/api/auth_store.dart';
import '../shared/models/first_video.dart';
import '../shared/models/ui_mode.dart';
import '../shared/router/paths.dart';

/// The router's auth guard (web `RequireAuth` + the landing's `useStart`),
/// kept free of widgets so every rule can be tested on its own.
///
/// [location] is the matched path, [uri] the full location with its query.
/// [introSeen] is only asked for a signed-out visit to the landing page;
/// [firstVideoEntry] only when a sign-in page is left behind, to pick between
/// the guided first video and the chat home. [uiMode] decides which home a
/// signed-in account has (docs `LIGHT_MODE_PLAN.md` §4.2): Light's four tabs
/// or the pro workspace; absent means pro, today's UI. Null means "stay where
/// you are".
String? authRedirect({
  required AuthState auth,
  required String location,
  required Uri uri,
  required bool Function() introSeen,
  FirstVideoEntry Function()? firstVideoEntry,
  UiMode Function()? uiMode,
}) {
  // Nothing is decided before the boot refresh settles, nor while a saved
  // sign-in could not be checked (offline): `WorkspaceBootstrap` covers every
  // screen with a retry then, and a deep link must survive until it settles.
  if (auth.isLoading || auth.offline) return null;
  final signedIn = auth.isAuthenticated;
  final light = signedIn && (uiMode?.call() ?? UiMode.pro) == UiMode.light;
  final home = light ? Paths.home : Paths.app;
  // A signed-in account never sees the pre-sign-in pages. A cold start begins
  // at the landing page, so this is what opens the home on launch. Deep
  // links (`/app/…`, invitations) keep their own destination.
  if (signedIn && (location == Paths.landing || location == Paths.intro)) {
    return home;
  }
  // Install-level intro (docs/MOBILE_ONBOARDING_PLAN.md L1): once per device,
  // before the landing page.
  if (!signedIn && location == Paths.landing && !introSeen()) {
    return Paths.intro;
  }
  final inApp = location.startsWith(Paths.app) || Paths.isLightPath(location);
  final invitation = location.startsWith('/invite/');
  if ((inApp || invitation) && !signedIn) {
    return Paths.loginFor(uri.toString());
  }
  if ((location == Paths.admin || location.startsWith('${Paths.admin}/')) &&
      auth.user?.role != 'admin') {
    return home;
  }
  final onAuthPage = location == Paths.login || location == Paths.register;
  if (onAuthPage && signedIn) {
    final destination = Paths.postAuthDestination(
      uri,
      entry: firstVideoEntry?.call() ?? FirstVideoEntry.settled,
    );
    final parsed = Uri.parse(destination);
    return uiModeHome(parsed.path, parsed, light: light) ?? destination;
  }
  // Each mode has its own home and tabs; a page of the other mode is
  // translated to its counterpart rather than shown.
  if (signedIn) return uiModeHome(location, uri, light: light);
  return null;
}

/// The counterpart of [location] in the current interface mode, its query
/// kept, or null when it already belongs there. Only the pages both modes
/// have — the home, the personal assistant and the message centre — move;
/// everything else (a chat, Settings, billing) is shared as it is.
String? uiModeHome(String location, Uri uri, {required bool light}) {
  String withQuery(String path) =>
      uri.hasQuery ? '$path?${uri.query}' : path;
  if (light) {
    return switch (location) {
      Paths.app => Paths.home,
      Paths.assistant => withQuery(Paths.lightAssistant),
      Paths.inbox => withQuery(Paths.lightInbox),
      _ => null,
    };
  }
  return switch (location) {
    Paths.home || Paths.me => Paths.app,
    Paths.lightAssistant => withQuery(Paths.assistant),
    Paths.lightInbox => withQuery(Paths.inbox),
    _ => null,
  };
}
