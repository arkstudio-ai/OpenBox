import 'dart:async';

import 'package:bossip_mobile/app/notification_host.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/workspace.dart';
import 'package:bossip_mobile/shared/notifications/push_controller.dart';
import 'package:bossip_mobile/shared/notifications/system_notifications.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _payload = <String, dynamic>{
  'source': 'openbox',
  'schemaVersion': 1,
  'eventId': 'event-1',
  'type': 'task_completed',
  'recipientId': 'user',
  'bindingId': 'binding',
  'workspaceId': 'target-workspace',
  'sessionId': 'session-1',
  'title': '完成',
  'body': '结果',
  'provider': 'jpush',
};

class _Native extends SystemNotifications {
  final source = StreamController<NativeNotificationEvent>.broadcast();
  int localShows = 0;
  bool cold = true;
  @override
  Stream<NativeNotificationEvent> get events => source.stream;
  @override
  Future<void> refresh() async {
    if (cold) {
      cold = false;
      open(_payload);
      open(_payload);
    }
  }

  void open(Map<String, dynamic> payload) =>
      source.add(NativeNotificationEvent(true, payload));
  void receive(Map<String, dynamic> payload) =>
      source.add(NativeNotificationEvent(false, payload));
  @override
  Future<void> setContext(Map<String, dynamic> context) async {}
  @override
  Future<void> clear() async {}
  @override
  Future<bool> showLocal(Map<String, dynamic> payload) async {
    localShows++;
    return true;
  }

  @override
  void dispose() {
    unawaited(source.close());
    super.dispose();
  }
}

class _Push extends PushController {
  _Push(super.dio, super.native, super.prefs);
  @override
  Future<void> checkSession() async {
    bindingId = 'binding';
  }

  @override
  Future<void> reportPresence() async {}
}

class _Auth extends AuthController {
  @override
  AuthState build() => const AuthState();
}

class _Workspace extends ActiveWorkspaceController {
  int selections = 0;
  @override
  Future<ActiveWorkspaceState> build() async {
    ref.read(workspaceScopeProvider).currentId = 'original';
    return const ActiveWorkspaceState(
      currentId: 'original',
      items: [
        WorkspaceSummary(
          id: 'target-workspace',
          name: 'Target',
          ownerUserId: 'user',
          kind: WorkspaceKind.personal,
          role: WorkspaceRole.owner,
        ),
      ],
    );
  }

  @override
  Future<void> select(String workspaceId) async {
    selections++;
    ref.read(workspaceScopeProvider).currentId = workspaceId;
    state = AsyncData(state.requireValue.copyWith(currentId: workspaceId));
  }
}

void main() {
  for (final role in ['admin', 'user']) {
    testWidgets(
      'test receipt and navigation are fenced by identity and binding ($role)',
      (tester) async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final native = _Native()..cold = false;
        final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
        final requests = <RequestOptions>[];
        dio.interceptors.add(
          InterceptorsWrapper(
            onRequest: (o, h) {
              requests.add(o);
              h.resolve(
                Response<dynamic>(
                  requestOptions: o,
                  statusCode: 200,
                  data: {'ok': true},
                ),
              );
            },
          ),
        );
        final push = _Push(dio, native, prefs);
        final router = GoRouter(
          routes: [
            GoRoute(path: '/', builder: (_, _) => const SizedBox()),
            GoRoute(
              path: '/app/admin/notifications',
              builder: (_, _) => const SizedBox(),
            ),
          ],
        );
        final container = ProviderContainer(
          overrides: [
            prefsProvider.overrideWithValue(prefs),
            apiDioProvider.overrideWithValue(dio),
            systemNotificationsProvider.overrideWith((_) => native),
            pushControllerProvider.overrideWith((_) => push),
            authProvider.overrideWith(_Auth.new),
            activeWorkspaceProvider.overrideWith(_Workspace.new),
            routerProvider.overrideWithValue(router),
            i18nProvider.overrideWith(
              () => I18nController(I18nBundle({}), prefs),
            ),
          ],
        );
        container
            .read(authProvider.notifier)
            .setAuth(
              'token',
              AuthUser(id: 'user', username: 'User', role: role),
              mobileSessionId: 'mobile-session',
            );
        await tester.pumpWidget(
          UncontrolledProviderScope(
            container: container,
            child: MaterialApp.router(
              routerConfig: router,
              builder: (_, child) => NotificationHost(child: child!),
            ),
          ),
        );
        await tester.pumpAndSettle();
        final payload = {..._payload, 'type': 'system_test'};
        for (final invalid in [
          {...payload, 'eventId': 'wrong-user', 'recipientId': 'someone-else'},
          {
            ...payload,
            'eventId': 'wrong-phone',
            'bindingId': 'retired-binding',
          },
        ]) {
          native.receive(invalid);
          native.open(invalid);
        }
        await tester.pumpAndSettle();
        expect(requests, isEmpty);
        expect(router.routeInformationProvider.value.uri.path, '/');
        native.receive(payload);
        native.receive(payload);
        for (var frame = 0; frame < 5; frame++) {
          await tester.pump(const Duration(milliseconds: 10));
        }
        native.open(payload);
        for (var frame = 0; frame < 5; frame++) {
          await tester.pump(const Duration(milliseconds: 10));
        }
        expect(
          requests.map((r) => r.data),
          role == 'admin'
              ? [
                  {'kind': 'received'},
                  {'kind': 'opened'},
                ]
              : <Map<String, String>>[],
        );
        expect(
          requests.every(
            (r) => r.path == '/api/admin/push/messages/event-1/receipt',
          ),
          isTrue,
        );
        expect(
          router.routeInformationProvider.value.uri.path,
          role == 'admin' ? '/app/admin/notifications' : '/',
        );
        native.open({...payload, 'eventId': 'local-preview'});
        await tester.pumpAndSettle();
        expect(requests.length, role == 'admin' ? 2 : 0);
        await tester.pumpWidget(const SizedBox());
        container.dispose();
        router.dispose();
        dio.close();
      },
    );
  }
  for (final allowed in [true, false]) {
    testWidgets(
      'cold click waits for auth and validates before switching workspace (allowed=$allowed)',
      (tester) async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final native = _Native();
        final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
        var validations = 0;
        RequestOptions? validationRequest;
        dio.interceptors.add(
          InterceptorsWrapper(
            onRequest: (o, h) {
              validations++;
              validationRequest = o;
              if (allowed) {
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
                      statusCode: 404,
                    ),
                  ),
                );
              }
            },
          ),
        );
        final push = _Push(dio, native, prefs);
        final router = GoRouter(
          routes: [
            GoRoute(path: '/', builder: (_, _) => const SizedBox()),
            GoRoute(path: '/app/s/:id', builder: (_, _) => const SizedBox()),
          ],
        );
        final container = ProviderContainer(
          overrides: [
            prefsProvider.overrideWithValue(prefs),
            apiDioProvider.overrideWithValue(dio),
            systemNotificationsProvider.overrideWith((ref) => native),
            pushControllerProvider.overrideWith((ref) => push),
            authProvider.overrideWith(_Auth.new),
            activeWorkspaceProvider.overrideWith(_Workspace.new),
            routerProvider.overrideWithValue(router),
            i18nProvider.overrideWith(
              () => I18nController(I18nBundle({}), prefs),
            ),
          ],
        );
        await tester.pumpWidget(
          UncontrolledProviderScope(
            container: container,
            child: MaterialApp.router(
              routerConfig: router,
              builder: (_, child) => NotificationHost(child: child!),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(validations, 0);
        container
            .read(authProvider.notifier)
            .setAuth(
              'token',
              const AuthUser(id: 'user', username: 'User'),
              mobileSessionId: 'mobile-session',
            );
        // Dio schedules its interceptor continuation separately from frames.
        for (var turn = 0; turn < 5; turn++) {
          await tester.pump(const Duration(milliseconds: 10));
        }
        await tester.pumpAndSettle();
        expect(
          validations,
          1,
        ); // Initial notification and queued copy deduplicate.
        final workspace =
            container.read(activeWorkspaceProvider.notifier) as _Workspace;
        expect(validationRequest?.path, '/api/agent/session/session-1');
        expect(
          validationRequest?.headers['X-Workspace-Id'],
          'target-workspace',
        );
        expect(workspace.selections, allowed ? 1 : 0);
        expect(
          router.routeInformationProvider.value.uri.path,
          allowed ? '/app/s/session-1' : '/',
        );
        native.open({
          ..._payload,
          'eventId': 'another-user',
          'recipientId': 'someone-else',
        });
        await tester.pumpAndSettle();
        expect(validations, 1);
        if (allowed) {
          native.receive(_payload);
          native.receive(_payload);
          await tester.pumpAndSettle();
          expect(
            native.localShows,
            0,
          ); // Active conversation uses the in-app hint.
        }
        // A foreground page outside this conversation must also stay quiet.
        router.go('/');
        await tester.pumpAndSettle();
        for (final state in [
          AppLifecycleState.resumed,
          AppLifecycleState.inactive,
          AppLifecycleState.hidden,
          AppLifecycleState.paused,
        ]) {
          tester.binding.handleAppLifecycleStateChanged(state);
          native.receive({..._payload, 'eventId': 'other-page-${state.name}'});
          await tester.pumpAndSettle();
          expect(push.lifecycle, state.name);
          expect(
            push.foreground,
            state == AppLifecycleState.resumed ||
                state == AppLifecycleState.inactive,
          );
          expect(native.localShows, 0);
        }
        tester.binding.handleAppLifecycleStateChanged(
          AppLifecycleState.resumed,
        );
        await tester.pumpWidget(const SizedBox());
        expect(tester.takeException(), isNull);
        container.dispose();
        router.dispose();
        dio.close();
      },
    );
  }
}
