import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../shared/api/ui_mode_store.dart';
import '../shared/router/paths.dart';
import 'auth_redirect.dart';

/// Light 模式 has a home, and every other page hangs off it: going back —
/// the system gesture, the iOS edge swipe, a page's own arrow — walks up to
/// where the person came from and ends on the tabs, never outside the app.
///
/// Two halves keep that true without each call site having to know:
///  * [HomeAnchoredRouter] turns "go to an inner page" into "open it above
///    the tabs", so there is always a page underneath to return to;
///  * [LightHomeAnchor] covers the page that still ends up alone (a cold
///    deep link, the guided first video): back leads to the home.
///
/// The pro workspace is untouched: its chats are siblings picked from the
/// drawer, so `go` keeps replacing there.

/// Whether [location] is an inner page of the signed-in app — one that in
/// Light 模式 belongs above the tabs rather than in place of them.
bool anchorsOnLightHome(String location) {
  final uri = Uri.parse(location);
  final path = uri.path;
  if (path.startsWith('/light/')) return true;
  if (!path.startsWith('${Paths.app}/')) return false;
  // The guided first video replaces its own steps as it advances; the call
  // route only forwards to the assistant.
  if (path.startsWith(Paths.firstVideo) || path == Paths.voice) return false;
  // `/app/assistant` and `/app/inbox` are tabs in Light.
  return uiModeHome(path, uri, light: true) == null;
}

class HomeAnchoredRouter extends GoRouter {
  HomeAnchoredRouter({
    required List<RouteBase> routes,
    required GoRouterRedirect redirect,
    required this.light,
    super.refreshListenable,
    super.initialLocation,
  }) : super.routingConfig(
         routingConfig: ValueNotifier(
           RoutingConfig(routes: anchorRoutes(routes), redirect: redirect),
         ),
       );

  /// Whether the signed-in account is in Light 模式 right now.
  final bool Function() light;

  @override
  void go(String location, {Object? extra}) {
    final stack = routerDelegate.currentConfiguration;
    final onTabs =
        stack.matches.isNotEmpty && stack.matches.first is ShellRouteMatch;
    if (light() && onTabs && anchorsOnLightHome(location)) {
      // Already there: nothing to open twice.
      if (stack.uri.toString() != location) {
        unawaited(push<void>(location, extra: extra));
      }
      return;
    }
    super.go(location, extra: extra);
  }
}

/// [routes] with every in-app page wrapped in a [LightHomeAnchor].
List<RouteBase> anchorRoutes(List<RouteBase> routes) => [
  for (final route in routes)
    if (route is GoRoute &&
        route.builder != null &&
        (route.path == Paths.app ||
            route.path.startsWith('${Paths.app}/') ||
            route.path.startsWith('/light/')))
      GoRoute(
        path: route.path,
        name: route.name,
        builder: (context, state) =>
            LightHomeAnchor(child: route.builder!(context, state)),
        parentNavigatorKey: route.parentNavigatorKey,
        redirect: route.redirect,
        metadata: route.metadata,
        onExit: route.onExit,
        caseSensitive: route.caseSensitive,
        routes: route.routes,
      )
    else
      route,
];

/// Back on a Light page with nothing beneath it opens the home instead of
/// leaving the app. A page that was opened above something pops as usual.
class LightHomeAnchor extends ConsumerWidget {
  const LightHomeAnchor({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final light = ref.watch(uiModeProvider.select((s) => s.isLight));
    return PopScope<Object?>(
      canPop: !light || (ModalRoute.canPopOf(context) ?? false),
      onPopInvokedWithResult: (didPop, _) {
        if (didPop || ModalRoute.of(context)?.isCurrent != true) return;
        // The first back only puts the keyboard away.
        if (MediaQuery.viewInsetsOf(context).bottom > 0) {
          FocusManager.instance.primaryFocus?.unfocus();
          return;
        }
        context.go(Paths.home);
      },
      child: child,
    );
  }
}
