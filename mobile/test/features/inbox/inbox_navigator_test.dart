import 'package:bossip_mobile/features/inbox/state/inbox_navigator.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/inbox.dart';
import 'package:bossip_mobile/shared/models/workspace.dart';
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

({
  ProviderContainer container,
  GoRouter router,
  List<RequestOptions> requests,
  _Workspace Function() workspace,
})
_harness({int sessionStatus = 200}) {
  final requests = <RequestOptions>[];
  final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (o, h) {
        requests.add(o);
        if (sessionStatus == 200) {
          h.resolve(
            Response<dynamic>(
              requestOptions: o,
              statusCode: 200,
              data: <String, dynamic>{},
            ),
          );
        } else {
          h.reject(
            DioException(
              requestOptions: o,
              response: Response<dynamic>(
                requestOptions: o,
                statusCode: sessionStatus,
              ),
              type: DioExceptionType.badResponse,
            ),
          );
        }
      },
    ),
  );
  final router = GoRouter(
    routes: [
      for (final path in [
        '/',
        '/app/inbox',
        '/app/s/:sessionId',
        '/app/w/:sessionId',
        '/app/cron',
        '/app/auth-center',
        '/app/skills',
        '/app/admin/:section',
        '/app/topics/:slug',
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
  return (
    container: container,
    router: router,
    requests: requests,
    workspace: () =>
        container.read(activeWorkspaceProvider.notifier) as _Workspace,
  );
}

void main() {
  test(
    'session link verifies access, switches workspace, opens the chat',
    () async {
      final h = _harness();
      final navigator = InboxNavigator(h.container.read, h.router);
      final result = await navigator.open(
        const InboxLink(kind: 'session', workspaceId: 'team', sessionId: 's1'),
      );
      expect(result, InboxOpen.opened);
      expect(h.requests.single.path, '/api/agent/session/s1');
      expect(h.requests.single.headers['X-Workspace-Id'], 'team');
      expect(h.workspace().selected, ['team']);
      expect(h.router.routeInformationProvider.value.uri.path, '/app/s/s1');
      h.container.dispose();
      h.router.dispose();
    },
  );

  test(
    'a takeover-shaped session link opens the desktop panel with control',
    () async {
      final h = _harness();
      final navigator = InboxNavigator(h.container.read, h.router);
      await navigator.open(
        const InboxLink(
          kind: 'session',
          workspaceId: 'home',
          sessionId: 's2',
          panel: 'desktop',
          control: true,
        ),
      );
      expect(h.workspace().selected, isEmpty); // already there
      final uri = h.router.routeInformationProvider.value.uri;
      expect(uri.path, '/app/w/s2');
      expect(uri.queryParameters, {'tab': 'desktop', 'control': '1'});
      h.container.dispose();
      h.router.dispose();
    },
  );

  test(
    'a session the user can no longer see is reported, not opened',
    () async {
      final h = _harness(sessionStatus: 404);
      final navigator = InboxNavigator(h.container.read, h.router);
      final result = await navigator.open(
        const InboxLink(
          kind: 'session',
          workspaceId: 'team',
          sessionId: 'gone',
        ),
      );
      expect(result, InboxOpen.unavailable);
      expect(h.workspace().selected, isEmpty);
      expect(h.router.routeInformationProvider.value.uri.path, '/');
      h.container.dispose();
      h.router.dispose();
    },
  );

  test(
    'a workspace the user left is unavailable without any request',
    () async {
      final h = _harness();
      final navigator = InboxNavigator(h.container.read, h.router);
      final result = await navigator.open(
        const InboxLink(kind: 'cron', workspaceId: 'elsewhere'),
      );
      expect(result, InboxOpen.unavailable);
      expect(h.requests, isEmpty);
      h.container.dispose();
      h.router.dispose();
    },
  );

  test(
    'cron, auth centre and skills links route inside their workspace',
    () async {
      for (final (link, path) in [
        (const InboxLink(kind: 'cron', workspaceId: 'home'), '/app/cron'),
        (
          const InboxLink(
            kind: 'auth_center',
            workspaceId: 'home',
            jobId: 'j1',
          ),
          '/app/auth-center',
        ),
        (const InboxLink(kind: 'skills', workspaceId: 'home'), '/app/skills'),
        (const InboxLink(kind: 'admin_skills'), '/app/admin/skills'),
        (
          const InboxLink(kind: 'topic', slug: 'release-2026-09'),
          '/app/topics/release-2026-09',
        ),
      ]) {
        final h = _harness();
        final navigator = InboxNavigator(h.container.read, h.router);
        expect(await navigator.open(link), InboxOpen.opened);
        final uri = h.router.routeInformationProvider.value.uri;
        expect(uri.path, path, reason: link.kind);
        if (link.jobId != null) expect(uri.queryParameters['job'], link.jobId);
        h.container.dispose();
        h.router.dispose();
      }
    },
  );

  test(
    'unknown kinds and missing links fall back to the inbox itself',
    () async {
      for (final link in [
        null,
        const InboxLink(kind: 'open_app', url: 'https://x/'),
      ]) {
        final h = _harness();
        final navigator = InboxNavigator(h.container.read, h.router);
        expect(await navigator.open(link), InboxOpen.opened);
        expect(h.router.routeInformationProvider.value.uri.path, '/app/inbox');
        expect(h.requests, isEmpty);
        h.container.dispose();
        h.router.dispose();
      }
    },
  );

  test('url links must be https and never navigate in-app', () async {
    final h = _harness();
    final navigator = InboxNavigator(h.container.read, h.router);
    expect(
      await navigator.open(
        const InboxLink(kind: 'url', url: 'http://bossip.example/'),
      ),
      InboxOpen.unavailable,
    );
    expect(
      await navigator.open(
        const InboxLink(kind: 'url', url: 'javascript:alert(1)'),
      ),
      InboxOpen.unavailable,
    );
    expect(h.router.routeInformationProvider.value.uri.path, '/');
    h.container.dispose();
    h.router.dispose();
  });
}
