import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/state/session_stop.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class StopApi extends AssistantApi {
  StopApi()
    : super(Dio(), (userId: 'stop-owner', workspaceId: 'stop-workspace'));
  final bodies = <Map<String, dynamic>>[];
  Object? failure;
  @override
  Future<Map<String, dynamic>> stopExecution(
    String id,
    Map<String, dynamic> body,
  ) async {
    bodies.add(Map.of(body));
    if (failure case final error?) throw error;
    return {
      'ok': true,
      'task_control': {'command_id': 'original-stop'},
    };
  }
}

Session session({int revision = 2}) => Session.fromJson({
  'id': 'execution',
  'user_id': 'stop-owner',
  'workspace_id': 'stop-workspace',
  'kind': 'normal',
  'assistant_managed': true,
  'task_control': {
    'task_id': 'original-task',
    'expected_revision': revision,
    'expected_run': {'run_id': 'run-$revision', 'generation': revision},
  },
});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  test(
    'lost response and restarted transport replay the original observed stop',
    () async {
      final prefs = await SharedPreferences.getInstance();
      final first = StopApi()..failure = Exception('response lost');
      await expectLater(
        stopAssistantExecution(
          session: session(),
          api: first,
          prefs: prefs,
          isCurrent: () => true,
        ),
        throwsException,
      );
      final restarted = StopApi();
      await stopAssistantExecution(
        session: session(revision: 7).copyWith(title: 'changed'),
        api: restarted,
        prefs: await SharedPreferences.getInstance(),
        isCurrent: () => true,
      );
      expect(restarted.bodies.single, first.bodies.single);
      expect(prefs.getKeys(), isEmpty);
      await stopAssistantExecution(
        session: session(revision: 7),
        api: restarted,
        prefs: prefs,
        isCurrent: () => true,
      );
      expect(restarted.bodies.last['expected_revision'], 7);
    },
  );

  test(
    'stale target refusal clears only that attempt and allows a newly observed stop',
    () async {
      final prefs = await SharedPreferences.getInstance();
      final api = StopApi()
        ..failure = ApiError(
          status: 409,
          code: 'ASSISTANT_RUN_CONFLICT',
          message: 'changed',
        );
      await expectLater(
        stopAssistantExecution(
          session: session(),
          api: api,
          prefs: prefs,
          isCurrent: () => true,
        ),
        throwsA(isA<ApiError>()),
      );
      expect(prefs.getKeys(), isEmpty);
      api.failure = null;
      await stopAssistantExecution(
        session: session(revision: 8),
        api: api,
        prefs: prefs,
        isCurrent: () => true,
      );
      expect(api.bodies.last['expected_revision'], 8);
    },
  );

  test(
    'account switch and a descendant without a direct task cannot send a stop',
    () async {
      final prefs = await SharedPreferences.getInstance();
      final api = StopApi();
      await expectLater(
        stopAssistantExecution(
          session: session(),
          api: api,
          prefs: prefs,
          isCurrent: () => false,
        ),
        throwsStateError,
      );
      final child = Session.fromJson({
        'id': 'child',
        'user_id': 'stop-owner',
        'workspace_id': 'stop-workspace',
        'assistant_managed': true,
      });
      await expectLater(
        stopAssistantExecution(
          session: child,
          api: api,
          prefs: prefs,
          isCurrent: () => true,
        ),
        throwsA(isA<ApiError>()),
      );
      expect(api.bodies, isEmpty);
      expect(prefs.getKeys(), isEmpty);
    },
  );

  test(
    'session updates retain task isolation and the observed control target',
    () {
      final updated = session().copyWith(status: SessionStatus.busy);
      expect(updated.assistantManaged, isTrue);
      expect(updated.taskControl?['expected_revision'], 2);
    },
  );
}
