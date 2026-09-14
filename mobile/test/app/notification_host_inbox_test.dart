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

/// A push tap with `notificationId` routes from the durable inbox row (its
/// server-vetted link) after marking it read; without a row it falls back to
/// the legacy payload-type routing.
const _payload = <String, dynamic>{
  'source': 'openbox',
  'schemaVersion': 1,
  'eventId': 'event-1',
  'type': 'task_completed',
  'recipientId': 'user',
  'bindingId': 'binding',
  'workspaceId': 'target-workspace',
  'sessionId': 'payload-session',
  'notificationId': 'ntf_1',
  'title': '完成',
  'body': '结果',
  'provider': 'jpush',
};

class _Native extends SystemNotifications {
  final source = StreamController<NativeNotificationEvent>.broadcast();
  @override
  Stream<NativeNotificationEvent> get events => source.stream;
  @override
  Future<void> refresh() async {}
  void open(Map<String, dynamic> payload) =>
      source.add(NativeNotificationEvent(true, payload));
  @override
  Future<void> setContext(Map<String, dynamic> context) async {}
  @override
  Future<void> clear() async {}
  @override
  Future<bool> showLocal(Map<String, dynamic> payload) async => true;
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
    ref.read(workspaceScopeProvider).currentId = workspaceId;
    state = AsyncData(state.requireValue.copyWith(currentId: workspaceId));
  }
}

void main() {
  for (final rowExists in [true, false]) {
    testWidgets('push tap routes from the inbox row (rowExists=$rowExists)', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final native = _Native();
      final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
      final requests = <RequestOptions>[];
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (o, h) {
            requests.add(o);
            if (o.path == '/api/inbox/ntf_1/read') {
              if (!rowExists) {
                h.reject(
                  DioException(
                    requestOptions: o,
                    response: Response<dynamic>(
                      requestOptions: o,
                      statusCode: 404,
                    ),
                    type: DioExceptionType.badResponse,
                  ),
                );
                return;
              }
              h.resolve(
                Response<dynamic>(
                  requestOptions: o,
                  statusCode: 200,
                  data: {
                    'id': 'ntf_1',
                    'category': 'session',
                    'kind': 'task_completed',
                    'title': '完成',
                    'body': '',
                    'link': {
                      'kind': 'session',
                      'workspaceId': 'target-workspace',
                      'sessionId': 'row-session',
                    },
                    'createdAt': '2026-09-11T08:00:00+00:00',
                  },
                ),
              );
              return;
            }
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
      final push = _Push(dio, native, prefs);
      final router = GoRouter(
        routes: [
          GoRoute(path: '/', builder: (_, _) => const SizedBox()),
          GoRoute(path: '/app/inbox', builder: (_, _) => const SizedBox()),
          GoRoute(
            path: '/app/s/:sessionId',
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
            AuthUser(id: 'user', username: 'User', role: 'user'),
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
      native.open(_payload);
      for (var frame = 0; frame < 8; frame++) {
        await tester.pump(const Duration(milliseconds: 10));
      }
      final paths = requests.map((r) => r.path).toList();
      expect(paths.first, '/api/inbox/ntf_1/read');
      if (rowExists) {
        // The row's link wins over the payload's sessionId.
        expect(paths, contains('/api/agent/session/row-session'));
        expect(
          router.routeInformationProvider.value.uri.path,
          '/app/s/row-session',
        );
      } else {
        expect(paths, contains('/api/agent/session/payload-session'));
        expect(
          router.routeInformationProvider.value.uri.path,
          '/app/s/payload-session',
        );
      }
      expect(
        container.read(workspaceScopeProvider).currentId,
        'target-workspace',
      );
      await tester.pumpWidget(const SizedBox());
      container.dispose();
      router.dispose();
      dio.close();
    });
  }
}
