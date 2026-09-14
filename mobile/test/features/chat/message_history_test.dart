import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/state/subagent_progress.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'suggestion_fixtures.dart';

/// Ids sort the way the server's do: in creation order.
String _id(int n) => 'm${n.toString().padLeft(2, '0')}';

ChatMessage _user(String id, {String? clientId}) => ChatMessage(
  id: id,
  sessionId: 's1',
  role: 'user',
  clientMessageId: clientId,
  parts: [TextPart(id: '$id-text', text: 'ask $id')],
);

ChatMessage _reply(
  String id, {
  String text = 'reply',
  ToolStatus tool = ToolStatus.completed,
  String? finish,
}) => ChatMessage(
  id: id,
  sessionId: 's1',
  role: 'assistant',
  finish: finish,
  parts: [
    TextPart(id: '$id-text', text: text),
    ToolPart(id: '$id-tool', tool: 'bash', status: tool),
  ],
);

/// What `send` puts up before the server confirms the message.
ChatMessage _echo(String clientId) => ChatMessage(
  id: 'tmp-$clientId',
  sessionId: 's1',
  role: 'user',
  clientMessageId: clientId,
  parts: [TextPart(id: 'tmp-part-$clientId', text: 'next')],
);

/// [count] turns, a user message and its reply each, from turn [from].
List<ChatMessage> _turns(int count, {int from = 0}) => [
  for (var i = from; i < from + count; i++) ...[
    _user(_id(2 * i)),
    _reply(_id(2 * i + 1)),
  ],
];

typedef _Read = ({int? turns, String? before, String? after});

/// A backend holding one transcript. Reads answer at once unless [hold] is
/// set; held reads wait until [release].
class _Server extends ChatApi {
  _Server() : super(Dio());

  List<ChatMessage> messages = [];
  String status = 'idle';
  bool hold = false;
  final reads = <_Read>[];
  final _waiting = <(_Read, Completer<HistoryPage>)>[];

  @override
  Future<HistoryPage> history(
    String sessionId, {
    int? turns,
    String? before,
    String? after,
  }) {
    final read = (turns: turns, before: before, after: after);
    reads.add(read);
    if (!hold) return Future.sync(() => _answer(read));
    final reply = Completer<HistoryPage>();
    _waiting.add((read, reply));
    return reply.future;
  }

  HistoryPage _answer(_Read read) => SuggestionApi.window(
    messages,
    turns: read.turns,
    before: read.before,
    after: read.after,
  );

  /// Answer the oldest held read from the transcript as it is now.
  void release() {
    final (read, reply) = _waiting.removeAt(0);
    try {
      reply.complete(_answer(read));
    } on DioException catch (error) {
      reply.completeError(error);
    }
  }

  @override
  Future<Session> getSession(String sessionId) async =>
      Session.fromJson({'id': sessionId, 'status': status});

  @override
  Future<List<PermissionRequest>> listPermissions() async => [];

  @override
  Future<List<QuestionRequest>> listQuestions() async => [];
}

Future<(ProviderContainer, SuggestionWs)> _open(ChatApi api) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final ws = SuggestionWs();
  final container = ProviderContainer(
    overrides: [
      chatApiProvider.overrideWithValue(api),
      apiDioProvider.overrideWithValue(Dio()),
      prefsProvider.overrideWithValue(prefs),
      wsClientProvider.overrideWithValue(ws),
    ],
  );
  return (container, ws);
}

List<String> _ids(ProviderContainer container) => [
  for (final m in container.read(chatStreamProvider).messagesOf('s1')) m.id,
];

/// Opens session s1's controller on [server] and disposes it inside the test
/// body, so its poll timer is gone before the fake clock is checked.
Future<void> _withController(
  WidgetTester tester,
  _Server server,
  Future<void> Function(ProviderContainer, SuggestionWs) body,
) async {
  final (container, ws) = await _open(server);
  try {
    container.read(chatSessionProvider('s1'));
    await tester.pump();
    await body(container, ws);
  } finally {
    container.dispose();
    await ws.close();
  }
}

Dio _recordingDio(
  List<RequestOptions> requests,
  Object? Function(RequestOptions) answer,
) => Dio()
  ..interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        requests.add(options);
        final data = answer(options);
        if (data is DioException) return handler.reject(data);
        handler.resolve(Response<dynamic>(requestOptions: options, data: data));
      },
    ),
  );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('mergeHistory', () {
    late ProviderContainer container;
    late ChatStreamStore store;
    List<ChatMessage> held() =>
        container.read(chatStreamProvider).messagesOf('s1');

    setUp(() async {
      final (opened, ws) = await _open(_Server());
      container = opened;
      addTearDown(() async {
        container.dispose();
        await ws.close();
      });
      store = container.read(chatStreamProvider.notifier);
    });

    test('keeps older pages, drops what the range lost, keeps newer ones', () {
      store.mergeHistory('s1', _turns(2, from: 2));
      store.mergeHistory('s1', _turns(2));
      expect(_ids(container), [for (var i = 0; i < 8; i++) _id(i)]);
      final older = held().sublist(0, 4);

      // Over the socket while a window read was out.
      store.addMessage('s1', _reply(_id(8)));
      store.addMessage('s1', _echo('c1'));
      // The window m04..m07 comes back without m05 (deleted on the server)
      // and with m07 grown.
      store.mergeHistory('s1', [
        _user(_id(4)),
        _user(_id(6)),
        _reply(_id(7), text: 'reply, and more'),
      ]);

      expect(_ids(container), [
        'm00', 'm01', 'm02', 'm03', 'm04', 'm06', 'm07', 'm08', 'tmp-c1', //
      ]);
      for (var i = 0; i < older.length; i++) {
        expect(held()[i], same(older[i]));
      }
      expect((held()[6].parts.first as TextPart).text, 'reply, and more');
    });

    test(
      'a read that changed nothing keeps instances and publishes nothing',
      () {
        store.mergeHistory('s1', _turns(1));
        final before = held();
        var published = 0;
        container.listen(chatStreamProvider, (_, _) => published++);

        // A fresh decode of the same data, then a stale one — shorter text and
        // a tool still running. Streamed state never moves backwards.
        store.mergeHistory('s1', _turns(1));
        store.mergeHistory('s1', [
          _user(_id(0)),
          _reply(_id(1), text: 're', tool: ToolStatus.running),
        ]);
        expect(held(), same(before));
        expect(published, 0);

        // A real change replaces only the message it touched.
        store.mergeHistory('s1', [
          _user(_id(0)),
          _reply(_id(1), finish: 'stop'),
        ]);
        expect(published, 1);
        expect(held()[0], same(before[0]));
        expect(held()[1], isNot(same(before[1])));
        expect(held()[1].finish, 'stop');
        expect(held()[1].parts, [
          same(before[1].parts[0]),
          same(before[1].parts[1]),
        ]);
      },
    );

    test('an optimistic echo stays until a read confirms its client id', () {
      store.mergeHistory('s1', _turns(1));
      store.addMessage('s1', _echo('c1'));
      expect(container.read(chatStreamProvider).newestHistoryId('s1'), 'm01');

      store.mergeHistory('s1', [_reply(_id(1), text: 'reply!')]);
      expect(_ids(container), ['m00', 'm01', 'tmp-c1']);

      store.mergeHistory('s1', [
        _reply(_id(1), text: 'reply!'),
        _user(_id(2), clientId: 'c1'),
        _reply(_id(3)),
      ]);
      expect(_ids(container), ['m00', 'm01', 'm02', 'm03']);
    });
  });

  testWidgets(
    'opens on the newest window, then polls after it, one at a time',
    (tester) async {
      final server = _Server()
        ..messages = _turns(12)
        ..status = 'busy';
      await _withController(tester, server, (container, ws) async {
        expect(server.reads, [
          (turns: chatHistoryTurns, before: null, after: null),
        ]);
        expect(_ids(container), [for (var i = 8; i < 24; i++) _id(i)]);
        expect(container.read(chatSessionProvider('s1')).hasMore, isTrue);

        server.hold = true;
        container
            .read(chatStreamProvider.notifier)
            .addMessage('s1', _echo('c1'));
        await tester.pump(const Duration(seconds: 1));
        // The echo means nothing to the server: read on from its newest.
        expect(server.reads.last, (turns: null, before: null, after: 'm23'));

        // While that read hangs, ticks start nothing and a reconnect waits.
        await tester.pump(const Duration(seconds: 3));
        ws.frames.add(const WsEvent('__connected', {}));
        await tester.pump();
        expect(server.reads, hasLength(2));

        server.messages = [...server.messages, _reply(_id(24))];
        server.release();
        await tester.pump();
        expect(_ids(container).sublist(14), ['m22', 'm23', 'm24', 'tmp-c1']);
        // Only now does the reconnect's window read start.
        expect(server.reads, hasLength(3));
        expect(server.reads.last.turns, chatHistoryTurns);
        server.release();
        await tester.pump();
        expect(_ids(container), hasLength(18));
      });
    },
  );

  testWidgets(
    'loading older prepends the turns before and stops at the start',
    (tester) async {
      final server = _Server()..messages = _turns(12);
      await _withController(tester, server, (container, ws) async {
        final controller = container.read(chatSessionProvider('s1').notifier);
        final newest = container.read(chatStreamProvider).messagesOf('s1');

        server.hold = true;
        unawaited(controller.loadOlder());
        unawaited(controller.loadOlder());
        await tester.pump();
        expect(server.reads.skip(1), [
          (turns: chatHistoryTurns, before: 'm08', after: null),
        ]);
        expect(container.read(chatSessionProvider('s1')).loadingOlder, isTrue);

        server.release();
        await tester.pump();
        final state = container.read(chatSessionProvider('s1'));
        expect(_ids(container), [for (var i = 0; i < 24; i++) _id(i)]);
        expect(
          container.read(chatStreamProvider).messagesOf('s1').sublist(8),
          newest,
        );
        expect(state.hasMore, isFalse);
        expect(state.loadingOlder, isFalse);

        unawaited(controller.loadOlder());
        await tester.pump();
        expect(server.reads, hasLength(2));
      });
    },
  );

  testWidgets('a vanished anchor drops held history and reloads the newest', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(12)
      ..status = 'busy';
    await _withController(tester, server, (container, ws) async {
      await container.read(chatSessionProvider('s1').notifier).loadOlder();
      expect(_ids(container), hasLength(24));

      // Regenerated elsewhere: the reply the view last holds is gone.
      server.messages = [..._turns(12).take(23), _reply(_id(24))];
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();

      expect(server.reads.skip(2), [
        (turns: null, before: null, after: 'm23'),
        (turns: chatHistoryTurns, before: null, after: null),
      ]);
      // The older page went with the reset; the window starts over.
      expect(_ids(container), [for (var i = 8; i < 23; i++) _id(i), 'm24']);
      expect(container.read(chatSessionProvider('s1')).hasMore, isTrue);
    });
  });

  test(
    'history sends only the cursor asked for and spots a gone anchor',
    () async {
      final requests = <RequestOptions>[];
      final api = ChatApi(
        _recordingDio(
          requests,
          (options) => options.queryParameters['after'] == 'gone'
              ? SuggestionApi.cursorGone()
              : {'messages': <Object>[], 'has_more': true},
        ),
      );

      expect((await api.history('s1', turns: 8)).hasMore, isTrue);
      await api.history('s1', turns: 8, before: 'm01');
      expect(requests.map((r) => r.path).toSet(), {
        '/api/agent/session/s1/history',
      });
      expect(requests.map((r) => r.queryParameters), [
        {'turns': 8},
        {'turns': 8, 'before': 'm01'},
      ]);

      Object? failure;
      try {
        await api.history('s1', after: 'gone');
      } catch (error) {
        failure = error;
      }
      expect(failure, isNotNull);
      expect(isHistoryCursorGone(failure!), isTrue);
    },
  );

  test('subagent backfill reads the child session as one turn', () async {
    final requests = <RequestOptions>[];
    final dio = _recordingDio(
      requests,
      (_) => {
        'messages': [
          {
            'id': 'm01',
            'session_id': 'child',
            'role': 'assistant',
            'parts': [
              {'id': 'p1', 'type': 'tool', 'tool': 'bash', 'status': 'running'},
            ],
          },
        ],
        'has_more': false,
      },
    );
    final (container, ws) = await _open(ChatApi(dio));
    addTearDown(() async {
      container.dispose();
      await ws.close();
    });
    const task = ToolPart(
      id: 'task',
      tool: 'task',
      status: ToolStatus.running,
      metadata: {'child_session_id': 'child'},
    );

    container.read(subagentProgressProvider(task));
    // Dio hands a request through its interceptors over several event-loop
    // turns; wait for the child's part to land rather than guess how many.
    for (var i = 0; i < 100; i++) {
      if (container.read(subagentProgressProvider(task)).current != null) {
        break;
      }
      await Future<void>.delayed(Duration.zero);
    }

    expect(requests.single.path, '/api/agent/session/child/history');
    expect(requests.single.queryParameters, {'turns': 1});
    expect(container.read(subagentProgressProvider(task)).current?.id, 'p1');
  });
}
