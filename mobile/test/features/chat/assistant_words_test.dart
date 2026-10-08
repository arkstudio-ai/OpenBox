import 'dart:convert';
import 'package:bossip_mobile/features/chat/utils/assistant_activity.dart';
import 'package:bossip_mobile/features/chat/utils/memory_receipt.dart';
import 'package:bossip_mobile/features/chat/utils/task_status.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:flutter_test/flutter_test.dart';

import 'assistant_redesign_parts.dart';

/// Plain words for task states, what the assistant is doing, where an
/// answer starts and which memory results become chips.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('task status in plain words', () {
    final cases = <(Map<String, Object?>, TaskStatus)>[
      ({'pending': 1, 'session': 'busy'}, TaskStatus.waiting),
      ({'session': 'waiting_input'}, TaskStatus.waiting),
      ({'session': 'busy', 'observed': 'idle'}, TaskStatus.running),
      ({'observed': 'running'}, TaskStatus.running),
      ({'session': 'queued'}, TaskStatus.queued),
      (
        {'desired': 'paused', 'observed': 'running', 'session': 'idle'},
        TaskStatus.paused,
      ),
      ({'desired': 'canceled', 'observed': 'running'}, TaskStatus.stopped),
      ({'observed': 'effect_unknown'}, TaskStatus.failed),
      ({'session': 'error'}, TaskStatus.failed),
      ({'observed': 'completed'}, TaskStatus.done),
      ({'observed': 'completed', 'outcome': 'error'}, TaskStatus.failed),
      ({'observed': 'idle', 'outcome': 'succeeded'}, TaskStatus.done),
      ({'observed': 'idle', 'outcome': 'aborted'}, TaskStatus.stopped),
      ({'observed': 'idle'}, TaskStatus.idle),
    ];
    for (final (input, expected) in cases) {
      test('$input reads as ${expected.name}', () {
        expect(
          taskStatus(
            sessionStatus: input['session'] as String?,
            observedState: input['observed'] as String?,
            desiredState: input['desired'] as String?,
            pendingQuestions: (input['pending'] as int?) ?? 0,
            outcome: input['outcome'] as String?,
          ),
          expected,
        );
      });
    }

    test('only waiting, running, queued and paused work is unfinished', () {
      expect(
        [
          TaskStatus.waiting,
          TaskStatus.running,
          TaskStatus.queued,
          TaskStatus.paused,
        ].every(isActiveTask),
        isTrue,
      );
      expect(
        [
          TaskStatus.done,
          TaskStatus.failed,
          TaskStatus.stopped,
          TaskStatus.idle,
        ].any(isActiveTask),
        isFalse,
      );
    });

    test('says how long ago within a week, then a short date', () {
      final now = DateTime(2026, 10, 7, 12);
      expect(
        sinceLabel(now.subtract(const Duration(hours: 2)), 'en-US', now: now),
        '2 hours ago',
      );
      expect(
        sinceLabel(now.subtract(const Duration(hours: 2)), 'zh-CN', now: now),
        '2小时前',
      );
      expect(sinceLabel(DateTime(2026, 3, 4), 'en-US', now: now), 'Mar 4');
      expect(sinceLabel(DateTime(2020, 3, 4), 'zh-CN', now: now), '2020年3月4日');
      expect(sinceLabel(null, 'en-US'), '');
    });
  });

  group('what the assistant is doing, in words', () {
    for (final (tool, expected) in const [
      ('tasks.submit', 'delegating'),
      ('tasks.pause', 'updatingTask'),
      ('results.read', 'checkingWork'),
      ('memory.remember', 'remembering'),
      ('memory.search', 'recalling'),
      ('knowledge.read', 'reading'),
      ('requests.list', 'checkingRequests'),
      ('briefing.configure', 'scheduling'),
      ('projects.brief.update', 'updatingBrief'),
      ('assets.attach', 'handlingFiles'),
      ('status.credits', 'checkingStatus'),
      ('something.new', 'working'),
    ]) {
      test('$tool reads as $expected', () {
        expect(
          assistantActivity([toolPart(tool, status: 'running')]),
          expected,
        );
      });
    }

    test('names the call in flight over the last one, and thinks first', () {
      expect(
        assistantActivity([
          toolPart('tasks.list', status: 'running'),
          toolPart('memory.search'),
        ]),
        'checkingWork',
      );
      expect(
        assistantActivity([toolPart('tasks.list'), toolPart('memory.search')]),
        'recalling',
      );
      expect(assistantActivity([]), 'thinking');
    });
  });

  test('a task result or the daily briefing starts an answer of its own', () {
    final rows = buildChatRows([
      userMessage('m01'),
      replyMessage('m02', text: 'On it.'),
      userMessage('m03', origin: 'task_result', synthetic: true),
      replyMessage('m04', text: 'The page is dark now.'),
      userMessage(
        'm05',
        origin: 'system_recovery',
        ref: {'entrypoint': 'daily_briefing'},
        synthetic: true,
      ),
      replyMessage('m06', text: 'Good morning, here is your day.'),
      // Other hidden inputs keep the answer in the same turn.
      userMessage('m07', origin: 'system_recovery', synthetic: true),
      replyMessage('m08', text: 'And one more thing.'),
      userMessage('m09'),
      replyMessage('m10', text: 'Sure.'),
    ]);
    expect(rows.map((row) => row.runtimeType.toString()), [
      'UserRowData',
      'AssistantTurnData',
      'AssistantTurnData',
      'AssistantTurnData',
      'UserRowData',
      'AssistantTurnData',
    ]);
    final turns = rows.whereType<AssistantTurnData>().toList();
    expect(turns.map((turn) => turn.origin), [
      null,
      TurnOrigin.report,
      TurnOrigin.briefing,
      null,
    ]);
    expect(turns[2].messages.map((m) => m.id), ['m06', 'm08']);
  });

  test('only a completed memory tool result becomes a chip', () {
    final remembered = memoryReceipt(
      toolPart(
        'memory.remember',
        output: jsonEncode({
          'state': 'remembered',
          'memory_id': 'mem-1',
          'summary': 'Prefers dark pages',
          'revision': 2,
        }),
      ),
    );
    expect(remembered?.kind, MemoryReceiptKind.remembered);
    expect(remembered?.summary, 'Prefers dark pages');
    expect(remembered?.revision, 2);
    expect(
      memoryReceipt(
        toolPart(
          'memory.remember',
          status: 'running',
          output: jsonEncode({'state': 'remembered'}),
        ),
      ),
      isNull,
    );
    expect(
      memoryReceipt(toolPart('memory.remember', output: 'remembered')),
      isNull,
    );
    expect(
      memoryReceipt(
        toolPart('memory.forget', output: jsonEncode({'state': 'forgotten'})),
      )?.kind,
      MemoryReceiptKind.forgotten,
    );
  });
}
