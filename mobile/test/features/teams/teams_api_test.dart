import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/teams/api/teams_api.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/models/team.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

const scope = (userId: 'owner', workspaceId: 'workspace');
void main() {
  test(
    'team controls carry the viewed revision and a stable idempotency key, never a budget',
    () async {
      final auth = AuthSession()..userId = scope.userId;
      final workspace = WorkspaceScope()..currentId = scope.workspaceId;
      final dio = Dio();
      final requests = <RequestOptions>[];
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            requests.add(options);
            handler.resolve(
              Response(requestOptions: options, data: <String, dynamic>{}),
            );
          },
        ),
      );
      final api = TeamsApi(dio, auth, workspace);
      await api.control(
        scope,
        TeamRun.fromJson({'id': 'run', 'revision': 9}),
        'pause',
        'request-one',
      );
      expect(requests.single.data, {'expected_revision': 9});
      expect(requests.single.headers['Idempotency-Key'], 'request-one');
      expect(requests.single.extra[requestScopeUserKey], scope.userId);
      expect(
        requests.single.extra[requestScopeWorkspaceKey],
        scope.workspaceId,
      );
      workspace.currentId = 'other';
      await expectLater(
        api.control(
          scope,
          TeamRun.fromJson({'id': 'run'}),
          'cancel',
          'request-two',
        ),
        throwsA(isA<ApiError>()),
      );
      expect(requests, hasLength(1));
    },
  );
  test('an old account response cannot populate the new workspace', () async {
    final auth = AuthSession()..userId = scope.userId;
    final workspace = WorkspaceScope()..currentId = scope.workspaceId;
    final dio = Dio();
    final started = Completer<void>(), finish = Completer<void>();
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          started.complete();
          await finish.future;
          handler.resolve(
            Response(requestOptions: options, data: {'items': <Object?>[]}),
          );
        },
      ),
    );
    final future = TeamsApi(dio, auth, workspace).templates(scope);
    await started.future;
    final check = expectLater(future, throwsA(isA<ApiError>()));
    auth.userId = 'next-owner';
    finish.complete();
    await check;
  });
  test(
    'selection is sent only in team mode and follows the ordinary Inbox path',
    () async {
      final requests = <RequestOptions>[];
      final dio = Dio()
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (options, handler) {
              requests.add(options);
              handler.resolve(
                Response(requestOptions: options, data: {'ok': true}),
              );
            },
          ),
        );
      final api = ChatApi(dio);
      const request = TeamRequest(
        templateId: 'template',
        allowSupplement: false,
      );
      await api.promptAsync(
        's',
        text: 'goal',
        clientMessageId: 'one',
        agent: 'team',
        teamRequest: request,
      );
      await api.promptAsync(
        's',
        text: 'ordinary',
        clientMessageId: 'two',
        agent: 'build',
        teamRequest: request,
      );
      final first = requests.first.data as Map<String, dynamic>;
      final last = requests.last.data as Map<String, dynamic>;
      expect(first['team_request'], {
        'template_id': 'template',
        'allow_supplement': false,
      });
      expect(first['delivery'], 'followup');
      expect(last.containsKey('team_request'), isFalse);
    },
  );
}
