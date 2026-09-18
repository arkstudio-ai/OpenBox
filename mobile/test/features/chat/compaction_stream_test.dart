import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/utils/compaction_view.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'compaction_fixtures.dart' show compactRequest, compactSummary;
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

  test(
    'summary flag and terminal status survive delayed history and duplicate creation',
    () {
      final request = compactRequest();
      final pending = compactSummary();
      store.mergeHistory('s1', [request, pending]);
      ws.frames.add(
        WsEvent('message.updated', {
          'sessionId': 's1',
          'generation': 1,
          'message': {
            'id': pending.id,
            'role': 'assistant',
            'summary': true,
            'finish': 'stop',
          },
        }),
      );
      expect(
        container.read(chatStreamProvider).messagesOf('s1').last.summary,
        isTrue,
      );
      store.updatePart(
        's1',
        request.id,
        const CompactionPart(
          id: 'm02-part',
          auto: true,
          replacementId: 'replacement',
        ),
      );
      store.mergeHistory('s1', [request, pending]);
      store.addMessage('s1', pending);
      final messages = container.read(chatStreamProvider).messagesOf('s1');
      expect(messages.last.summary, isTrue);
      expect(messages.last.finish, 'stop');
      expect(
        (messages.first.parts.single as CompactionPart).replacementId,
        'replacement',
      );
      expect(
        buildCompactionViews(messages, true).single.status,
        CompactionStatus.completed,
      );
    },
  );

  test(
    'descriptor-only commit refreshes observers without requiring summary text',
    () {
      final request = compactRequest(replacement: '');
      store.mergeHistory('s1', [request]);
      final before = container.read(chatStreamProvider);
      store.mergeHistory('s1', [compactRequest(replacement: 'replacement')]);
      final after = container.read(chatStreamProvider);
      expect(identical(before, after), isFalse);
      expect(
        buildCompactionViews(after.messagesOf('s1'), true).single.status,
        CompactionStatus.completed,
      );
    },
  );
}
