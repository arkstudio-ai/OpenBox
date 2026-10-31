import 'package:bossip_mobile/features/chat/state/effect_ack.dart';
import 'package:bossip_mobile/features/chat/widgets/execution_effect_notice.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

class AckApi extends TestApi {
  final calls = <Map<String, dynamic>>[];
  bool remaining = false;

  @override
  Future<Map<String, dynamic>> acknowledgeEffects(
    String taskId,
    Map<String, dynamic> body,
  ) async {
    calls.add({'task_id': taskId, ...body});
    return {
      'command_id': 'ack',
      'task_id': taskId,
      'remaining_unresolved': remaining,
      'task_revision': 13,
    };
  }
}

Session session({String observed = 'effect_unknown', bool managed = true}) =>
    Session.fromJson({
      'id': 'execution',
      'user_id': scope.userId,
      'workspace_id': scope.workspaceId,
      'title': '艾薇兰小红瓶',
      'status': 'idle',
      'created_at': '2026-10-31T09:41:18Z',
      'updated_at': '2026-10-31T09:52:44Z',
      'kind': 'normal',
      'assistant_managed': managed,
      'task_control': managed
          ? {
              'task_id': 'task-1',
              'expected_revision': 12,
              'expected_run': null,
              'desired_state': 'canceled',
              'observed_state': observed,
            }
          : null,
    });

Future<Fixture> pump(WidgetTester tester, AckApi api, Widget child) async {
  SharedPreferences.setMockInitialValues({});
  final f = (await tester.runAsync(() async {
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    return Fixture(
      prefs,
      server: api,
      i18n: I18nState(language: 'zh-CN', bundle: bundle),
    );
  }))!;
  addTearDown(f.close);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: f.container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(body: child),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return f;
}

void main() {
  testWidgets('shows only on a task page that waits for confirmation', (
    tester,
  ) async {
    final api = AckApi();
    await pump(
      tester,
      api,
      ExecutionEffectNotice(
        sessionId: 'execution',
        session: session(observed: 'running'),
      ),
    );
    expect(find.text('我已核对，继续'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('confirms with the current revision and clears the request', (
    tester,
  ) async {
    final api = AckApi();
    var acknowledged = 0;
    final f = await pump(
      tester,
      api,
      ExecutionEffectNotice(
        sessionId: 'execution',
        session: session(),
        onAcknowledged: () => acknowledged += 1,
      ),
    );
    f.container
            .read(executionEffectAckRequestedProvider('execution').notifier)
            .state =
        true;
    await tester.pump();
    expect(find.text('有一步的结果没来得及确认'), findsOneWidget);
    await tester.tap(find.text('我已核对，继续'));
    await tester.pumpAndSettle();
    expect(api.calls, hasLength(1));
    expect(api.calls.single['task_id'], 'task-1');
    expect(api.calls.single['expected_revision'], 12);
    expect(api.calls.single['idempotency_key'], startsWith('ack-'));
    expect(acknowledged, 1);
    expect(
      f.container.read(executionEffectAckRequestedProvider('execution')),
      isFalse,
    );
    // The confirmation toast owns a dismissal timer; end it with the test.
    f.container.read(toastProvider.notifier).clear();
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump(const Duration(milliseconds: 1));
  });

  testWidgets('stop confirmation offers stop and keep', (tester) async {
    var confirmed = 0, kept = 0;
    await pump(
      tester,
      AckApi(),
      StopTaskNotice(onConfirm: () => confirmed++, onKeep: () => kept++),
    );
    expect(find.text('停下整个任务？'), findsOneWidget);
    await tester.tap(find.text('继续运行'));
    expect(kept, 1);
    await tester.tap(find.text('停止任务'));
    expect(confirmed, 1);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
