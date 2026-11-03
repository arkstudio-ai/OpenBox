import 'package:bossip_mobile/features/chat/widgets/task_room_banner.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

Session _room({
  String? taskTitle = '艾薇兰小红瓶调研',
  String desired = 'running',
  String observed = 'running',
  String kind = 'chat',
  bool managed = true,
  bool control = true,
}) => Session.fromJson({
  'id': 'execution',
  'title': '会话标题',
  'status': 'idle',
  'kind': kind,
  'assistant_managed': managed,
  if (control)
    'task_control': {
      'task_id': 't1',
      'expected_revision': 3,
      'expected_run': null,
      'desired_state': desired,
      'observed_state': observed,
      'task_title': ?taskTitle,
    },
});

Future<int> _pump(WidgetTester tester, Session session) async {
  var backs = 0;
  await tester.pumpWidget(
    ProviderScope(
      key: UniqueKey(),
      overrides: [await zhI18n(tester)],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(
          body: TaskRoomBanner(session: session, onBack: () => backs++),
        ),
      ),
    ),
  );
  await tester.pump();
  return backs;
}

void main() {
  testWidgets(
    'names the room by its task title, with its state and a way back',
    (tester) async {
      await _pump(tester, _room());
      expect(find.textContaining('「艾薇兰小红瓶调研」现场'), findsOneWidget);
      expect(find.textContaining('帮手在做'), findsOneWidget);
      await tester.tap(find.byKey(const Key('task-room-back')));
      expect(find.text('回助理'), findsOneWidget);
    },
  );

  testWidgets('stays off the assistant page and ordinary chats', (
    tester,
  ) async {
    await _pump(tester, _room(kind: 'assistant'));
    expect(find.byKey(const Key('task-room-banner')), findsNothing);
    await _pump(tester, _room(managed: false, control: false));
    expect(find.byKey(const Key('task-room-banner')), findsNothing);
  });

  test(
    'falls back to the conversation title and reads the state from both fields',
    () {
      expect(TaskRoomBanner.titleOf(_room(taskTitle: null)), '会话标题');
      expect(
        TaskRoomBanner.stateOf({
          'desired_state': 'canceled',
          'observed_state': 'effect_unknown',
        }),
        'effect_unknown',
      );
      expect(
        TaskRoomBanner.stateOf({
          'desired_state': 'canceled',
          'observed_state': 'idle',
        }),
        'canceled',
      );
      expect(
        TaskRoomBanner.stateOf({
          'desired_state': 'paused',
          'observed_state': 'resume_blocked',
        }),
        'paused',
      );
      expect(
        TaskRoomBanner.stateOf({
          'desired_state': 'running',
          'observed_state': 'waiting_input',
        }),
        'waiting',
      );
    },
  );
}
