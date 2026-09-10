import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/utils/suggestions.dart';
import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

class _Pages extends ChatApi {
  _Pages(this.page) : super(Dio());
  final List<ChatMessage> Function(int) page;
  @override
  Future<List<ChatMessage>> listMessages(
    String sessionId, {
    int offset = 0,
    int limit = 200,
  }) async => page(offset);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('suggestion status preserves legacy results and parses deadlines', () {
    SuggestionsPart parse(Map<String, dynamic> data) =>
        MessagePart.fromJson({'id': 'p', 'type': 'suggestions', ...data})
            as SuggestionsPart;
    expect(parse({}).status, SuggestionStatus.completed);
    expect(
      parse({'status': 'unexpected'}).status,
      SuggestionStatus.unavailable,
    );
    final pending = parse({
      'status': 'pending',
      'expires_at': '2026-09-10T03:00:00+00:00',
    });
    expect(pending.status, SuggestionStatus.pending);
    expect(pending.expiresAt, DateTime.utc(2026, 9, 10, 3));
    expect(parse({'expires_at': 'invalid'}).expiresAt, isNull);
    expect(
      latestSuggestions(
        buildChatRows([
          answer(parts: [pending]),
        ]),
        SessionStatus.idle,
      ),
      pending,
    );
    expect(
      latestSuggestions(
        buildChatRows([
          answer(
            parts: [
              parse({'status': 'unavailable'}),
            ],
          ),
        ]),
        SessionStatus.idle,
      ),
      isNull,
    );
  });

  for (final terminal in [null, 'completed', 'unavailable']) {
    test(
      'terminal $terminal survives delayed pending events and snapshots',
      () async {
        final fixture = await SuggestionFixture.create();
        addTearDown(fixture.dispose);
        final store = fixture.container.read(chatStreamProvider.notifier);
        store.setMessages('s1', [answer(parts: [])]);
        final pending = {
          'id': 'p1',
          'type': 'suggestions',
          'status': 'pending',
          'expires_at': DateTime.now()
              .add(const Duration(seconds: 60))
              .toIso8601String(),
          'items': <dynamic>[],
        };
        // A reconnect can deliver the final update without its creation event.
        fixture.ws.frames.add(
          WsEvent('part.updated', {
            'sessionId': 's1',
            'messageId': 'm001',
            'part': {
              'id': 'p1',
              'type': 'suggestions',
              'status': ?terminal,
              'items': [
                if (terminal != 'unavailable')
                  {'label': 'Test', 'prompt': 'Add tests.', 'mode': 'send'},
              ],
            },
          }),
        );
        for (final event in ['part.created', 'part.updated']) {
          fixture.ws.frames.add(
            WsEvent(event, {
              'sessionId': 's1',
              'messageId': 'm001',
              'part': pending,
            }),
          );
        }
        store.setMessages('s1', [
          answer(parts: [MessagePart.fromJson(pending)]),
        ]);
        final result = fixture.container
            .read(chatStreamProvider)
            .messagesOf('s1')
            .single
            .parts
            .whereType<SuggestionsPart>()
            .single;
        expect(
          result.status,
          terminal == 'unavailable'
              ? SuggestionStatus.unavailable
              : SuggestionStatus.completed,
        );
        expect(result.items.length, terminal == 'unavailable' ? 0 : 1);
        expect(fixture.api.sends, isEmpty);
      },
    );
  }

  test('suggestions decode defensively, deduplicate, cap at three', () {
    Map<String, dynamic> item(String label, String prompt, Object mode) => {
      'label': label,
      'prompt': prompt,
      'mode': mode,
    };
    final part =
        MessagePart.fromJson({
              'id': 'p',
              'type': 'suggestions',
              'items': [
                null,
                'bad',
                <String, dynamic>{},
                item('a', 'p', 'execute'),
                item('', 'p', 'send'),
                item('a', '', 'draft'),
                item('a' * 33, 'p', 'send'),
                item('a', 'p' * 801, 'send'),
                item(' a ', ' p ', 'send'),
                item('a', 'other', 'send'),
                item('b', 'p', 'draft'),
                item('b', 'q', 'draft'),
                item('c', 'r', 'send'),
                item('d', 's', 'send'),
              ],
            })
            as SuggestionsPart;
    expect(part.items.map((i) => i.label), ['a', 'b', 'c']);
    expect(part.items.map((i) => i.prompt), ['p', 'q', 'r']);
    expect(part.items[1].mode, SuggestionMode.draft);
    expect(
      SuggestionsPart.fromJson({'items': <String, dynamic>{}}).items,
      isEmpty,
    );
  });

  test('only latest successful final message supplies suggestions', () {
    List<ChatRow> rows(List<ChatMessage> messages) => buildChatRows(messages);
    expect(
      latestSuggestions(rows([answer()]), SessionStatus.idle),
      testSuggestions,
    );
    expect((rows([answer()]).single as AssistantTurnData).bodyText, isEmpty);
    for (final next in [
      answer(id: 'm002', parts: []),
      answer(id: 'm002', finish: 'abort'),
      answer(id: 'm002', finish: null),
      answer(id: 'm002', error: {'message': 'failed'}),
      const ChatMessage(id: 'm002', sessionId: 's1', role: 'user', parts: []),
    ]) {
      expect(
        latestSuggestions(rows([answer(), next]), SessionStatus.idle),
        isNull,
      );
    }
    for (final status in [
      null,
      ...SessionStatus.values.where((s) => s != SessionStatus.idle),
    ]) {
      expect(latestSuggestions(rows([answer()]), status), isNull);
    }
    final ready = rows([answer()]);
    expect(
      latestSuggestions(ready, SessionStatus.idle, atBottom: false),
      isNull,
    );
    expect(
      latestSuggestions(ready, SessionStatus.idle, readOnly: true),
      isNull,
    );
    expect(
      latestSuggestions(ready, SessionStatus.idle, hasRunError: true),
      isNull,
    );
    expect(
      latestSuggestions(ready, SessionStatus.idle, hasPendingInput: true),
      isNull,
    );
    expect(
      latestSuggestions(
        rows([
          answer(
            parts: const [SuggestionsPart(id: 'empty', items: [])],
          ),
        ]),
        SessionStatus.idle,
      ),
      isNull,
    );
  });

  test(
    'late part.created is retained across older snapshots and reloads',
    () async {
      final fixture = await SuggestionFixture.create();
      addTearDown(fixture.dispose);
      final store = fixture.container.read(chatStreamProvider.notifier);
      store.setMessages('s1', [answer(parts: [])]);
      fixture.ws.frames.add(
        const WsEvent('part.created', {
          'sessionId': 's1',
          'messageId': 'm001',
          'part': {
            'id': 'p1',
            'type': 'suggestions',
            'items': [
              {'label': 'Test', 'prompt': 'Add tests.', 'mode': 'send'},
            ],
          },
        }),
      );
      store.setMessages('s1', [answer(parts: [])]);
      var messages = fixture.container
          .read(chatStreamProvider)
          .messagesOf('s1');
      expect(
        latestSuggestions(buildChatRows(messages), SessionStatus.idle)?.id,
        'p1',
      );
      final restored = ChatMessage.fromJson({
        'id': 'm002',
        'session_id': 's2',
        'role': 'assistant',
        'finish': 'stop',
        'parts': [
          {
            'id': 'p2',
            'type': 'suggestions',
            'items': [
              {'label': 'Edit', 'prompt': 'Add details.', 'mode': 'draft'},
            ],
          },
        ],
      });
      store.setMessages('s2', [restored]);
      messages = fixture.container.read(chatStreamProvider).messagesOf('s2');
      expect(
        latestSuggestions(buildChatRows(messages), SessionStatus.idle)?.id,
        'p2',
      );
      expect(fixture.api.sends, isEmpty);
    },
  );

  test(
    'snapshot includes current suggestions after more than 200 messages',
    () async {
      final fixture = await SuggestionFixture.create();
      addTearDown(fixture.dispose);
      fixture.api.messages = [
        for (var i = 0; i < 401; i++) answer(id: 'm$i', parts: []),
        answer(id: 'latest'),
      ];
      final snapshot = await fixture.api.messageSnapshot('s1');
      expect(snapshot.length, 402);
      expect(fixture.api.offsets, [0, 200, 400]);
      expect(
        latestSuggestions(buildChatRows(snapshot), SessionStatus.idle),
        testSuggestions,
      );
    },
  );

  test(
    'partial failed or repeated pages cannot become a fresh snapshot',
    () async {
      final page = [for (var i = 0; i < 200; i++) answer(id: 'm$i')];
      await expectLater(
        _Pages((_) => page).messageSnapshot('s1'),
        throwsStateError,
      );
      await expectLater(
        _Pages((offset) {
          if (offset > 0) throw StateError('offline');
          return page;
        }).messageSnapshot('s1'),
        throwsStateError,
      );
    },
  );
}
