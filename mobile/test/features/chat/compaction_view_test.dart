import 'package:bossip_mobile/features/chat/utils/compaction_view.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter_test/flutter_test.dart';

import 'compaction_fixtures.dart';

void main() {
  test('streaming summary is process before summary flag is set', () {
    final messages = [compactRequest(), compactSummary()];
    final row = buildChatRows(messages).single as AssistantTurnData;
    expect(row.lastReply, isNull);
    expect(row.bodyText, isEmpty);
    expect(row.notices, isEmpty);
    final content = buildAssistantContentView(messages, true);
    expect(content.hasFinal, isFalse);
    expect(content.workEvents, isEmpty);
    expect(
      buildCompactionViews(messages, true).single.status,
      CompactionStatus.running,
    );
  });

  test('manual and auto compression preserve the answer and reply actions', () {
    for (final auto in [true, false]) {
      final original = answer();
      final messages = [
        original,
        compactRequest(auto: auto),
        compactSummary(finish: 'stop'),
      ];
      final row = buildChatRows(messages).single as AssistantTurnData;
      expect(row.lastReply, same(original));
      expect(row.lastMessageId, original.id);
      expect(row.finish, 'stop');
      final content = buildAssistantContentView(messages, false);
      expect(content.finalText, 'Answer');
      expect(content.finalMessageId, original.id);
      expect(content.workEvents, isEmpty);
      expect(
        buildCompactionViews(messages, false).single.status,
        CompactionStatus.completed,
      );
    }
  });

  test('continued loop keeps multiple optimizations in one turn', () {
    final messages = [
      answer(text: 'Working', finish: 'tool_calls'),
      compactRequest(),
      compactSummary(finish: 'stop'),
      answer(id: 'm04', text: 'Continue', finish: 'tool_calls'),
      compactRequest(id: 'm05'),
      compactSummary(id: 'm06', parent: 'm05', finish: 'stop'),
      answer(id: 'm07', text: 'Final answer'),
    ];
    expect(buildChatRows(messages), hasLength(1));
    expect(buildCompactionViews(messages, false).map((v) => v.id), [
      'm02',
      'm05',
    ]);
    final content = buildAssistantContentView(messages, false);
    expect(content.finalText, 'Final answer');
    expect(content.progress.map((v) => v.text), ['Working', 'Continue']);
  });

  test(
    'failure and interruption never replace the answer or mark its turn failed',
    () {
      for (final finish in ['error', 'aborted']) {
        final messages = [
          answer(),
          compactRequest(),
          compactSummary(finish: finish),
        ];
        final row = buildChatRows(messages).single as AssistantTurnData;
        expect(row.error, isNull);
        expect(buildAssistantContentView(messages, false).finalText, 'Answer');
        expect(
          buildCompactionViews(messages, false).single.status,
          finish == 'error'
              ? CompactionStatus.failed
              : CompactionStatus.interrupted,
        );
      }
    },
  );

  test('history pages recognize lone summaries, requests and old rows', () {
    final legacy = compactSummary(finish: 'stop', legacy: true);
    expect(buildAssistantContentView([legacy], false).hasFinal, isFalse);
    expect(buildCompactionViews([legacy], false).single.id, 'm02');
    final request = compactRequest(replacement: 'replacement-1');
    expect(
      buildCompactionViews([request], false).single.status,
      CompactionStatus.completed,
    );
    expect(
      buildCompactionViews([compactRequest()], false).single.status,
      CompactionStatus.interrupted,
    );
    expect(buildCompactionViews([], true), isEmpty);
  });

  test(
    'more than 200 messages keep real user boundaries and the next answer',
    () {
      final history = [
        for (var i = 0; i < 125; i++) ...[
          ChatMessage(
            id: 'u$i',
            sessionId: 's1',
            role: 'user',
            parts: [TextPart(id: 'p$i', text: 'Task $i')],
          ),
          answer(id: 'a$i'),
        ],
        compactRequest(),
        compactSummary(finish: 'stop'),
        const ChatMessage(
          id: 'next-user',
          sessionId: 's1',
          role: 'user',
          parts: [TextPart(id: 'next-text', text: 'Next task')],
        ),
        answer(id: 'next-answer', text: 'Continue normally'),
      ];
      final rows = buildChatRows(history);
      expect(rows.whereType<UserRowData>(), hasLength(126));
      expect(rows.whereType<AssistantTurnData>(), hasLength(126));
      expect((rows.last as AssistantTurnData).bodyText, 'Continue normally');
    },
  );
}
