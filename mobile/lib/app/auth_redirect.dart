import '../shared/api/auth_store.dart';
import '../shared/models/first_video.dart';
import '../shared/router/paths.dart';

/// The router's auth guard (web `RequireAuth` + the landing's `useStart`),
/// kept free of widgets so every rule can be tested on its own.
///
/// [location] is the matched path, [uri] the full location with its query.
/// [introSeen] is only asked for a signed-out visit to the landing page;
/// [firstVideoEntry] only when a sign-in page is left behind, to pick between
/// the guided first video and the chat home. Null means "stay where you are".
String? authRedirect({
  required AuthState auth,
  required String location,
  required Uri uri,
  required bool Function() introSeen,
  FirstVideoEntry Function()? firstVideoEntry,
}) {
  // Nothing is decided before the boot refresh settles, nor while a saved
  // sign-in could not be checked (offline): `WorkspaceBootstrap` covers every
  // screen with a retry then, and a deep link must survive until it settles.
  if (auth.isLoading || auth.offline) return null;
  final signedIn = auth.isAuthenticated;
  // A signed-in account never sees the pre-sign-in pages. A cold start begins
  // at the landing page, so this is what opens the chat home on launch. Deep
  // links (`/app/…`, invitations) keep their own destination.
  if (signedIn && (location == Paths.landing || location == Paths.intro)) {
    return Paths.app;
  }
  // Install-level intro (docs/MOBILE_ONBOARDING_PLAN.md L1): once per device,
  // before the landing page.
  if (!signedIn && location == Paths.landing && !introSeen()) {
    return Paths.intro;
  }
  final inApp = location.startsWith(Paths.app);
  final invitation = location.startsWith('/invite/');
  if ((inApp || invitation) && !signedIn) {
    return Paths.loginFor(uri.toString());
  }
  if ((location == Paths.admin || location.startsWith('${Paths.admin}/')) &&
      auth.user?.role != 'admin') {
    return Paths.app;
  }
  final onAuthPage = location == Paths.login || location == Paths.register;
  if (onAuthPage && signedIn) {
    return Paths.postAuthDestination(
      uri,
      entry: firstVideoEntry?.call() ?? FirstVideoEntry.settled,
    );
  }
  return null;
}
