import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:flutter_test/flutter_test.dart';

ChatMessage message(String finish, {String status = 'completed'}) =>
    ChatMessage.fromJson({
      'id': 'm',
      'session_id': 's',
      'role': 'assistant',
      'finish': finish,
      'parts': [
        {'id': 'p', 'type': 'tool', 'tool': 'question', 'status': status},
      ],
    });

void main() {
  test('waiting and queued asks are not incomplete final answers', () {
    expect(
      buildAssistantContentView([
        message('waiting_input', status: 'waiting_input'),
      ], false).incomplete,
      isFalse,
    );
    expect(
      buildAssistantContentView(
        [message('tool_calls')],
        false,
        awaitingInput: true,
      ).incomplete,
      isFalse,
    );
  });
  test('terminal suspended history does not claim a missing final answer', () {
    expect(
      buildAssistantContentView([message('waiting_input')], false).incomplete,
      isFalse,
    );
  });
  test('waiting tool handles message-finish event ordering', () {
    expect(
      buildAssistantContentView([
        message('tool_calls', status: 'waiting_input'),
      ], false).incomplete,
      isFalse,
    );
  });
  test('a genuinely finished turn without an answer still warns', () {
    expect(
      buildAssistantContentView([message('stop')], false).incomplete,
      isTrue,
    );
  });
}
