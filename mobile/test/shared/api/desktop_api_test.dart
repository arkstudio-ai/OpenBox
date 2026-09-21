import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/desktop_api.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/events/bus.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:bossip_mobile/shared/models/desktop.dart';
import 'package:bossip_mobile/shared/utils/format.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const scope = (userId: 'user-a', workspaceId: 'workspace-a');

void main() {
  test('production backend and websocket use the requested Alibaba domain', () {
    expect(Env.apiBase, 'https://ai.bossipai.com.cn');
    expect(Env.webBase, 'https://ai.bossipai.com.cn');
    expect(Env.wsBase, 'wss://ai.bossipai.com.cn');
  });

  test(
    'paid prices use the backend catalogue without a client promotion override',
    () {
      final data =
          jsonDecode(File('../backend/billing/plans.json').readAsStringSync())
              as Map<String, dynamic>;
      for (final entry
          in (data['plans'] as List<dynamic>).cast<Map<String, dynamic>>()) {
        final plan = BillingPlan.fromJson(entry);
        final prices = entry['prices_fen'] as Map<String, dynamic>;
        expect(plan.priceFor('monthly'), prices['monthly']);
        expect(plan.priceFor('yearly'), prices['yearly']);
      }
    },
  );

  test(
    'a ten-fen yearly promotion stays a total, not twelve monthly payments',
    () {
      final plan = BillingPlan.fromJson({
        'id': 'promotion',
        'prices_fen': {'monthly': 10, 'yearly': 10},
      });
      expect(plan.priceFor('yearly'), 10);
      expect(formatFen(plan.priceFor('yearly')), '0.1');
    },
  );

  test('parses durable activation and fails closed at subscription expiry', () {
    final status = DesktopStatus.fromJson({
      'mode': 'per_user',
      'state': 'running',
      'entitled': true,
      'retained': true,
      'subscription_ends_at': '2026-10-01T00:00:00Z',
      'channel': {'state': 'up'},
      'activation': {
        'request_id': 'pay-1',
        'state': 'ready',
        'step': 'ready',
        'attempts': 2,
        'can_retry': false,
      },
    });
    expect(status.activation?.requestId, 'pay-1');
    expect(status.activation?.attempts, 2);
    expect(status.channel?.state, 'up');
    expect(status.retained, isTrue);
    expect(status.hasAccessAt(DateTime.utc(2026, 9, 30)), isTrue);
    expect(status.hasAccessAt(DateTime.utc(2026, 10, 1)), isFalse);
    expect(status.hasAccessAt(DateTime.utc(2026, 10, 2)), isFalse);
    expect(
      const DesktopStatus(state: 'running', mode: 'per_user').hasAccess,
      isFalse,
    );
    expect(
      const DesktopStatus(
        state: 'subscription_required',
        entitled: false,
      ).hasAccess,
      isFalse,
    );
    expect(
      const DesktopStatus(state: 'running', mode: 'shared').hasAccess,
      isTrue,
    );
  });

  test(
    'all desktop calls pin workspace and reject changed identity/scope',
    () async {
      final requests = <RequestOptions>[];
      final auth = AuthSession()..userId = scope.userId;
      final workspace = WorkspaceScope()..currentId = scope.workspaceId;
      final dio = Dio();
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            requests.add(options);
            handler.resolve(
              Response<Map<String, dynamic>>(
                requestOptions: options,
                statusCode: 200,
                data: const {'state': 'queued', 'entitled': true},
              ),
            );
          },
        ),
      );
      final api = DesktopApi(dio, auth, workspace);
      await api.status(scope);
      await api.retry(scope);
      await api.ticket(scope, taskId: 'task-1');
      expect(
        requests.map((r) => r.headers['X-Workspace-Id']),
        everyElement(scope.workspaceId),
      );
      expect(requests.last.queryParameters['task_id'], 'task-1');
      workspace.currentId = 'workspace-b';
      await expectLater(api.retry(scope), throwsStateError);
      workspace.currentId = scope.workspaceId;
      auth.userId = 'user-b';
      await expectLater(api.status(scope), throwsStateError);
      expect(requests, hasLength(3));
    },
  );

  test(
    'status polling is read-only and billing settlement refreshes it',
    () async {
      final requests = <String>[];
      final dio = Dio();
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            requests.add('${options.method} ${options.path}');
            handler.resolve(
              Response<Map<String, dynamic>>(
                requestOptions: options,
                statusCode: 200,
                data: const {
                  'mode': 'per_user',
                  'state': 'subscription_required',
                  'entitled': false,
                },
              ),
            );
          },
        ),
      );
      final api = DesktopApi(
        dio,
        AuthSession()..userId = scope.userId,
        WorkspaceScope()..currentId = scope.workspaceId,
      );
      final container = ProviderContainer(
        overrides: [desktopApiProvider.overrideWithValue(api)],
      );
      addTearDown(container.dispose);
      final subscription = container.listen(
        desktopStatusProvider(scope),
        (_, _) {},
      );
      addTearDown(subscription.close);
      expect(
        (await container.read(desktopStatusProvider(scope).future)).state,
        'subscription_required',
      );
      container.read(appEventBusProvider).emit('billing.changed');
      await Future<void>.delayed(Duration.zero);
      await container.read(desktopStatusProvider(scope).future);
      expect(requests, ['GET /api/desktop/status', 'GET /api/desktop/status']);
    },
  );
}
