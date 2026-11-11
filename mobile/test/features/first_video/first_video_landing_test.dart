import 'package:bossip_mobile/app/auth_redirect.dart';
import 'package:bossip_mobile/features/inbox/state/inbox_navigator.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/first_video.dart';
import 'package:bossip_mobile/shared/models/inbox.dart';
import 'package:bossip_mobile/shared/models/workspace.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class _Workspace extends ActiveWorkspaceController {
  final selected = <String>[];
  @override
  Future<ActiveWorkspaceState> build() async {
    ref.read(workspaceScopeProvider).currentId = 'home';
    return const ActiveWorkspaceState(
      currentId: 'home',
      items: [
        WorkspaceSummary(
          id: 'home',
          name: 'Home',
          ownerUserId: 'user',
          kind: WorkspaceKind.personal,
          role: WorkspaceRole.owner,
        ),
        WorkspaceSummary(
          id: 'team',
          name: 'Team',
          ownerUserId: 'boss',
          kind: WorkspaceKind.team,
          role: WorkspaceRole.member,
        ),
      ],
    );
  }

  @override
  Future<void> select(String workspaceId) async {
    selected.add(workspaceId);
    ref.read(workspaceScopeProvider).currentId = workspaceId;
    state = AsyncData(state.requireValue.copyWith(currentId: workspaceId));
  }
}

({ProviderContainer container, GoRouter router, List<RequestOptions> requests})
_harness() {
  final requests = <RequestOptions>[];
  final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (o, h) {
        requests.add(o);
        h.resolve(
          Response<dynamic>(
            requestOptions: o,
            statusCode: 200,
            data: <String, dynamic>{},
          ),
        );
      },
    ),
  );
  final router = GoRouter(
    routes: [
      for (final path in [
        '/',
        '/app/inbox',
        '/app/s/:sessionId',
        Paths.firstVideoResult,
      ])
        GoRoute(path: path, builder: (_, _) => const SizedBox()),
    ],
  );
  final container = ProviderContainer(
    overrides: [
      apiDioProvider.overrideWithValue(dio),
      activeWorkspaceProvider.overrideWith(_Workspace.new),
    ],
  );
  return (container: container, router: router, requests: requests);
}

void main() {
  for (final kind in firstVideoNotificationKinds) {
    test('a $kind notice lands on the result page, not in the chat', () async {
      final h = _harness();
      final result = await InboxNavigator(h.container.read, h.router).open(
        const InboxLink(kind: 'session', workspaceId: 'team', sessionId: 's1'),
        kind: kind,
      );
      expect(result, InboxOpen.opened);
      // Still vetted like any session link: membership, then access.
      expect(h.requests.single.path, '/api/agent/session/s1');
      expect(
        (h.container.read(activeWorkspaceProvider.notifier) as _Workspace)
            .selected,
        ['team'],
      );
      expect(
        h.router.routeInformationProvider.value.uri.path,
        Paths.firstVideoResult,
      );
      h.container.dispose();
      h.router.dispose();
    });
  }

  test('any other kind with a session link still opens the chat', () async {
    final h = _harness();
    await InboxNavigator(h.container.read, h.router).open(
      const InboxLink(kind: 'session', workspaceId: 'home', sessionId: 's1'),
      kind: 'task_completed',
    );
    expect(h.router.routeInformationProvider.value.uri.path, '/app/s/s1');
    h.container.dispose();
    h.router.dispose();
  });

  group('auth guard after sign-in', () {
    const signedIn = AuthState(
      user: AuthUser(id: 'u1', username: 'alice'),
      isLoading: false,
    );
    String? redirect(String location, FirstVideoEntry entry) {
      final uri = Uri.parse(location);
      return authRedirect(
        auth: signedIn,
        location: uri.path,
        uri: uri,
        introSeen: () => true,
        firstVideoEntry: () => entry,
      );
    }

    test('a new account leaves the sign-in page for the guided flow', () {
      expect(redirect('/login', FirstVideoEntry.fresh), Paths.firstVideo);
      expect(redirect('/register', FirstVideoEntry.fresh), Paths.firstVideo);
      expect(
        redirect('/login', FirstVideoEntry.making),
        Paths.firstVideoMaking,
      );
      expect(redirect('/login', FirstVideoEntry.settled), Paths.app);
    });

    test('the entry only matters on the sign-in pages', () {
      expect(redirect('/', FirstVideoEntry.fresh), Paths.app);
      expect(redirect('/app/inbox', FirstVideoEntry.fresh), isNull);
      expect(redirect(Paths.firstVideoShop, FirstVideoEntry.settled), isNull);
    });

    test('a deep link keeps its destination', () {
      expect(
        redirect('/login?redirect=%2Fapp%2Finbox', FirstVideoEntry.fresh),
        '/app/inbox',
      );
    });
  });
}
