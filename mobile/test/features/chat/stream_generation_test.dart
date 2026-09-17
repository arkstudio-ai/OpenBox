import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'message_history_fixtures.dart';
import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ProviderContainer container;
  late SuggestionWs ws;
  late ChatStreamStore store;

  setUp(() {
    ws = SuggestionWs();
    container = ProviderContainer(
      overrides: [wsClientProvider.overrideWithValue(ws)],
    );
    store = container.read(chatStreamProvider.notifier);
  });
  tearDown(() async {
    container.dispose();
    await ws.close();
  });

  void status(String value, [int? generation]) => ws.frames.add(
    WsEvent('session.status', {
      'sessionId': 's1',
      'status': value,
      'generation': generation,
    }),
  );

  test('late terminal and unversioned statuses cannot settle a newer run', () {
    status('busy');
    status('idle');
    expect(
      container.read(chatStreamProvider).statusOf('s1'),
      SessionStatus.idle,
    );
    status('busy', 2);
    status('idle', 1);
    status('error');
    expect(
      container.read(chatStreamProvider).statusOf('s1'),
      SessionStatus.busy,
    );
    status('idle', 2);
    status('retry', 2);
    expect(
      container.read(chatStreamProvider).statusOf('s1'),
      SessionStatus.idle,
    );
    status('busy', 3);
    expect(
      container.read(chatStreamProvider).statusOf('s1'),
      SessionStatus.busy,
    );
  });

  test(
    'old output is rejected while waiting and answered states still advance',
    () {
      store.addMessage('s1', historyReply('m1', text: 'current'));
      status('busy', 3);
      for (final generation in [2, 3]) {
        ws.frames.add(
          WsEvent('message.text_delta', {
            'sessionId': 's1',
            'messageId': 'm1',
            'partId': 'm1-text',
            'text': ' $generation',
            'generation': generation,
          }),
        );
      }
      final part =
          container.read(chatStreamProvider).messagesOf('s1').first.parts.first
              as TextPart;
      expect(part.text, 'current 3');
      status('waiting_input', 3);
      status('queued', 3);
      expect(
        container.read(chatStreamProvider).statusOf('s1'),
        SessionStatus.queued,
      );
      status('busy', 4);
      expect(
        container.read(chatStreamProvider).statusOf('s1'),
        SessionStatus.busy,
      );
    },
  );
}
