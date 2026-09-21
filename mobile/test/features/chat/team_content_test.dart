import 'dart:convert';

import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

ChatMessage finish({
  String? output,
  String state = 'completing',
  String status = 'completed',
  String? error,
  List<MessagePart> prefix = const [],
  List<MessagePart> suffix = const [],
}) => ChatMessage(
  id: 'finish',
  sessionId: 's1',
  role: 'assistant',
  finish: 'tool_calls',
  parts: [
    ...prefix,
    ToolPart(
      id: 'receipt',
      tool: 'team_finish',
      status: toolStatusFrom(status),
      error: error,
      output:
          output ??
          jsonEncode({
            'state': state,
            'final_status': 'completed',
            'summary': '17+29=46',
            'turn_yield': true,
          }),
    ),
    ...suffix,
  ],
);

void main() {
  test(
    'completion receipt restores final prose without an extra model turn',
    () {
      final view = buildAssistantContentView([
        finish(
          prefix: [const TextPart(id: 'pre', text: '现在收尾', channel: 'final')],
        ),
      ], false);
      expect(view.finalText, '17+29=46');
      expect(view.finalMessageId, 'finish');
      expect(view.incomplete, isFalse);
      expect(view.progress, isEmpty);
    },
  );
  test('failed and nonterminal receipts never become an answer', () {
    for (final message in [
      finish(status: 'error'),
      finish(state: 'running'),
      finish(output: '{partial'),
    ]) {
      expect(buildAssistantContentView([message], false).hasFinal, isFalse);
    }
  });
  test('canonical full summary survives a truncated tool receipt', () {
    final view = buildAssistantContentView([
      finish(
        output: '{partial',
        suffix: [
          const TextPart(id: 'final', text: '完整交付：17+29=46', channel: 'final'),
        ],
      ),
    ], false);
    expect(view.finalText, '完整交付：17+29=46');
    expect(view.progress, isEmpty);
  });
  test('yield while members work is not a missing final answer', () {
    final view = buildAssistantContentView([
      answer(
        finish: 'tool_calls',
        parts: [
          const ToolPart(
            id: 'wait',
            tool: 'team_wait',
            status: ToolStatus.completed,
            metadata: {'turn_yield': true},
          ),
        ],
      ),
    ], false);
    expect(view.incomplete, isFalse);
  });
  test(
    'successful continuation resolves an earlier turn error without changing history',
    () {
      final failed = answer(error: {'message': '429'}, finish: 'error');
      final rows = buildChatRows([
        failed,
        answer(
          id: 'recovered',
          parts: [const TextPart(id: 'final', text: '完成', channel: 'final')],
        ),
      ]);
      expect((rows.single as AssistantTurnData).error, isNull);
      expect(failed.error, {'message': '429'});
    },
  );
  testWidgets('own member conversation is read only and links to its root', (
    tester,
  ) async {
    final fixture = await SuggestionFixture.create();
    addTearDown(fixture.dispose);
    fixture.api.sessionKind = 'team_member';
    fixture.api.messages = [
      answer(error: {'message': '429: slow down'}, finish: 'error'),
    ];
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    expect(find.byType(Composer), findsNothing);
    expect(find.text('返回团队对话 · 成员会话只读'), findsOneWidget);
    expect(find.text('429: slow down'), findsOneWidget);
    expect(find.text('重新生成'), findsNothing);
  });
}
