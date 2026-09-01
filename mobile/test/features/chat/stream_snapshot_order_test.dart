import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

ChatMessage _message(String id, {String? finish}) => ChatMessage(
  id: id,
  sessionId: 'session-1',
  role: 'assistant',
  parts: const [],
  finish: finish,
);

void main() {
  test('newest-page polling keeps older cached history before the tail', () {
    final container = ProviderContainer(
      overrides: [wsClientProvider.overrideWithValue(AgentWsClient(Dio()))],
    );
    addTearDown(container.dispose);
    final store = container.read(chatStreamProvider.notifier);

    store.setMessages('session-1', [
      _message('message_001'),
      _message('message_002'),
      _message('message_003'),
    ]);
    store.setMessages('session-1', [
      _message('message_003', finish: 'stop'),
      _message('message_004'),
    ]);

    expect(
      container
          .read(chatStreamProvider)
          .messagesOf('session-1')
          .map((message) => message.id),
      const ['message_001', 'message_002', 'message_003', 'message_004'],
    );
    expect(
      container.read(chatStreamProvider).messagesOf('session-1')[2].finish,
      'stop',
    );
  });
}
