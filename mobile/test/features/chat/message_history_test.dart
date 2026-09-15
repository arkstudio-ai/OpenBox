import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/state/subagent_progress.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/events/app_lifecycle.dart';
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
  String agent = 'build';
  bool hold = false;
  final reads = <_Read>[];
  final _waiting = <(_Read, Completer<HistoryPage>)>[];

  /// Session and question-list reads, counted.
  int sessionReads = 0;
  int questionReads = 0;

  /// While set, session reads wait for it before answering.
  Completer<void>? sessionGate;

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
  Future<Session> getSession(String sessionId) async {
    sessionReads++;
    await sessionGate?.future;
    return Session.fromJson({
      'id': sessionId,
      'status': status,
      'agent': agent,
    });
  }

  @override
  Future<List<PermissionRequest>> listPermissions() async => [];

  @override
  Future<List<QuestionRequest>> listQuestions() async {
    questionReads++;
    return [];
  }
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
      // The server's copy as it stands. Merged with the echo, it kept the
      // echo's part as well, and the bubble said the message twice.
      expect([for (final p in held()[2].parts) p.id], ['m02-text']);
    });

    test('the socket confirming a send replaces its echo whole', () {
      store.mergeHistory('s1', _turns(1));
      store.addMessage('s1', _echo('c1'));
      store.addMessage('s1', _user(_id(2), clientId: 'c1'));
      expect(_ids(container), ['m00', 'm01', 'm02']);
      expect([for (final p in held()[2].parts) p.id], ['m02-text']);
    });

    test('a late message.created merges into what a read already brought', () {
      store.mergeHistory('s1', [
        _user(_id(0)),
        _reply(_id(1), text: 'reply, streamed in full', finish: 'stop'),
      ]);
      final brought = held()[1];

      // Its creation frame, sent before any of that streamed.
      store.addMessage(
        's1',
        _reply(_id(1), text: 'rep', tool: ToolStatus.running),
      );
      expect(held()[1], same(brought));
      expect((held()[1].parts[0] as TextPart).text, 'reply, streamed in full');
      expect((held()[1].parts[1] as ToolPart).status, ToolStatus.completed);
      expect(held()[1].finish, 'stop');
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

  testWidgets('a chat nobody watches fetches nothing until watched again', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'busy';
    final (container, ws) = await _open(server);
    try {
      final screen = container.listen(chatSessionProvider('s1'), (_, _) {});
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));
      expect(server.reads, [
        (turns: chatHistoryTurns, before: null, after: null),
        (turns: null, before: null, after: 'm05'),
      ]);

      // The chat screen went away: no ticks, and no frame starts a read.
      screen.close();
      server.reads.clear();
      server.sessionReads = 0;
      final questionReads = server.questionReads;
      await tester.pump(const Duration(seconds: 3));
      ws.frames
        ..add(const WsEvent('__connected', {}))
        ..add(
          const WsEvent('session.status', {
            'sessionId': 's1',
            'status': 'idle',
          }),
        )
        ..add(
          const WsEvent('question.replied', {'session_id': 's1', 'id': 'q1'}),
        )
        ..add(const WsEvent('session.updated', {'sessionId': 's1'}));
      await tester.pump();
      expect(server.reads, isEmpty);
      expect(server.sessionReads, 0);
      expect(server.questionReads, questionReads);

      // Back on screen: the newest turns and the pending cards once, then the
      // catch-up again.
      final back = container.listen(chatSessionProvider('s1'), (_, _) {});
      await tester.pump();
      expect(server.reads, [
        (turns: chatHistoryTurns, before: null, after: null),
      ]);
      expect(server.questionReads, questionReads + 1);
      await tester.pump(const Duration(seconds: 1));
      expect(server.reads.last, (turns: null, before: null, after: 'm05'));
      back.close();
    } finally {
      container.dispose();
      await ws.close();
    }
  });

  testWidgets(
    'a live chat reads its session every fifth tick with the socket up',
    (tester) async {
      final server = _Server()
        ..messages = _turns(3)
        ..status = 'busy';
      await _withController(tester, server, (container, ws) async {
        ws.open = true;
        server.reads.clear();
        server.sessionReads = 0;
        await tester.pump(const Duration(seconds: 4));
        expect(server.reads, hasLength(4));
        expect(server.sessionReads, 0);

        // The fifth carries it: web's recovery for frames that never came.
        await tester.pump(const Duration(seconds: 1));
        expect(server.reads, hasLength(5));
        expect(server.sessionReads, 1);

        // Down, every tick carries one.
        ws.open = false;
        await tester.pump(const Duration(seconds: 2));
        expect(server.reads, hasLength(7));
        expect(server.sessionReads, 3);
      });
    },
  );

  testWidgets(
    'a tick skipped behind a read in flight leaves its session read due',
    (tester) async {
      final server = _Server()
        ..messages = _turns(3)
        ..status = 'busy';
      await _withController(tester, server, (container, ws) async {
        ws.open = true;
        server.reads.clear();
        server.sessionReads = 0;
        server.hold = true;
        await tester.pump(const Duration(seconds: 1));
        expect(server.reads, hasLength(1));

        // Ticks two to five find that read still out; the fifth was due.
        await tester.pump(const Duration(seconds: 4));
        expect(server.reads, hasLength(1));
        expect(server.sessionReads, 0);

        server.hold = false;
        server.release();
        await tester.pump();
        await tester.pump(const Duration(seconds: 1));
        expect(server.reads, hasLength(2));
        expect(server.sessionReads, 1);
      });
    },
  );

  testWidgets(
    'a lost end of run is found by the session read, which reloads once',
    (tester) async {
      final server = _Server()
        ..messages = _turns(3)
        ..status = 'busy';
      await _withController(tester, server, (container, ws) async {
        ws.open = true;
        server.reads.clear();
        // The run ends, and its session.status frame never arrives.
        server.status = 'idle';
        await tester.pump(const Duration(seconds: 5));

        expect(
          container.read(chatStreamProvider).statusOf('s1'),
          SessionStatus.idle,
        );
        expect(server.reads.skip(3), [
          (turns: null, before: null, after: 'm05'),
          (turns: null, before: null, after: 'm05'),
          (turns: chatHistoryTurns, before: null, after: null),
        ]);

        // Over: nothing more to poll.
        await tester.pump(const Duration(seconds: 5));
        expect(server.reads, hasLength(6));
      });
    },
  );

  testWidgets('a queued chat reads only its session until its run starts', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'queued';
    await _withController(tester, server, (container, ws) async {
      ws.open = true;
      server.reads.clear();
      server.sessionReads = 0;
      await tester.pump(const Duration(seconds: 5));
      expect(server.sessionReads, 1);
      expect(server.reads, isEmpty);

      // Down, every tick asks.
      ws.open = false;
      await tester.pump(const Duration(seconds: 2));
      expect(server.sessionReads, 3);
      expect(server.reads, isEmpty);

      // Its run starts and that frame is lost too: the next read finds it,
      // and the tick after that polls history.
      server.status = 'busy';
      await tester.pump(const Duration(seconds: 1));
      expect(
        container.read(chatStreamProvider).statusOf('s1'),
        SessionStatus.busy,
      );
      expect(server.reads, isEmpty);
      await tester.pump(const Duration(seconds: 1));
      expect(server.reads, [(turns: null, before: null, after: 'm05')]);
    });
  });

  testWidgets('session.updated reads the record once; a newer status stands', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'busy';
    await _withController(tester, server, (container, ws) async {
      ws.open = true;
      server.reads.clear();
      server.sessionReads = 0;

      // The model switched to plan mode, and while the record was out the
      // run went on to a retry over the socket.
      server
        ..agent = 'plan'
        ..sessionGate = Completer<void>();
      ws.frames
        ..add(
          const WsEvent('session.updated', {
            'sessionId': 's1',
            'agent': 'plan',
          }),
        )
        ..add(
          const WsEvent('session.status', {
            'sessionId': 's1',
            'status': 'retry',
          }),
        );
      server.sessionGate!.complete();
      server.sessionGate = null;
      await tester.pump();

      expect(server.sessionReads, 1);
      expect(server.reads, isEmpty);
      expect(container.read(chatSessionProvider('s1')).session?.agent, 'plan');
      expect(
        container.read(chatStreamProvider).statusOf('s1'),
        SessionStatus.retry,
      );
    });
  });

  testWidgets('a session.updated read that finds the run over reloads once', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'busy';
    await _withController(tester, server, (container, ws) async {
      ws.open = true;
      server.reads.clear();
      server
        ..status = 'idle'
        ..agent = 'plan';
      ws.frames.add(
        const WsEvent('session.updated', {'sessionId': 's1', 'agent': 'plan'}),
      );
      await tester.pump();

      expect(
        container.read(chatStreamProvider).statusOf('s1'),
        SessionStatus.idle,
      );
      expect(server.reads, [
        (turns: chatHistoryTurns, before: null, after: null),
      ]);
    });
  });

  testWidgets('session.updated reads coalesce: one out, one waiting', (
    tester,
  ) async {
    final server = _Server()..messages = _turns(3);
    await _withController(tester, server, (container, ws) async {
      server
        ..sessionReads = 0
        ..sessionGate = Completer<void>();
      for (final agent in ['plan', 'build', 'plan']) {
        ws.frames.add(
          WsEvent('session.updated', {'sessionId': 's1', 'agent': agent}),
        );
      }
      await tester.pump();
      expect(server.sessionReads, 1);

      final gate = server.sessionGate!;
      server
        ..agent = 'plan'
        ..sessionGate = null;
      gate.complete();
      await tester.pump();
      expect(server.sessionReads, 2);
      expect(container.read(chatSessionProvider('s1')).session?.agent, 'plan');
    });
  });

  testWidgets(
    'title and usage frames patch the record; plan frames read nothing',
    (tester) async {
      final server = _Server()..messages = _turns(3);
      await _withController(tester, server, (container, ws) async {
        server.sessionReads = 0;
        ws.frames
          ..add(
            const WsEvent('session.title', {
              'userId': 'u1',
              'sessionId': 's1',
              'title': 'Renamed',
            }),
          )
          ..add(
            const WsEvent('session.updated', {
              'userId': 'u1',
              'sessionId': 's1',
              'token_usage': {'context': 4200, 'limit': 200000},
            }),
          )
          ..add(
            const WsEvent('session.updated', {
              'userId': 'u1',
              'sessionId': 's1',
              'planUpdated': true,
            }),
          );
        await tester.pump();

        final session = container.read(chatSessionProvider('s1')).session;
        expect(session?.title, 'Renamed');
        // The composer's context ring reads it.
        expect(session?.tokenUsage?.context, 4200);
        expect(server.sessionReads, 0);
        expect(server.reads, hasLength(1));
      });
    },
  );

  for (final storeFirst in [false, true]) {
    testWidgets(
      'the end of a run re-reads the newest turns (store first: $storeFirst)',
      (tester) async {
        final server = _Server()
          ..messages = _turns(3)
          ..status = 'busy';
        final (container, ws) = await _open(server);
        try {
          // The chat screen builds its controller before anything has read
          // the store, and that controller hears every frame first.
          if (storeFirst) container.read(chatStreamProvider);
          container.read(chatSessionProvider('s1'));
          await tester.pump();
          server.reads.clear();

          server.status = 'idle';
          ws.frames.add(
            const WsEvent('session.status', {
              'sessionId': 's1',
              'status': 'idle',
            }),
          );
          await tester.pump();
          expect(server.reads, [
            (turns: chatHistoryTurns, before: null, after: null),
          ]);

          // A run starting is no consistency barrier.
          server.reads.clear();
          server.status = 'busy';
          ws.frames.add(
            const WsEvent('session.status', {
              'sessionId': 's1',
              'status': 'busy',
            }),
          );
          await tester.pump();
          expect(server.reads, isEmpty);
        } finally {
          container.dispose();
          await ws.close();
        }
      },
    );
  }

  testWidgets('a queued run is not polled until the socket starts it', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'queued';
    await _withController(tester, server, (container, ws) async {
      await tester.pump(const Duration(seconds: 3));
      expect(server.reads, [
        (turns: chatHistoryTurns, before: null, after: null),
      ]);

      server.status = 'busy';
      ws.frames.add(
        const WsEvent('session.status', {'sessionId': 's1', 'status': 'busy'}),
      );
      await tester.pump(const Duration(seconds: 1));
      expect(server.reads.last, (turns: null, before: null, after: 'm05'));
    });
  });

  testWidgets('only an answered question re-reads the transcript', (
    tester,
  ) async {
    final server = _Server()..messages = _turns(3);
    await _withController(tester, server, (container, ws) async {
      await tester.pump();
      server.reads.clear();
      final questionReads = server.questionReads;

      // The pending store puts the card up and keeps it current by itself.
      for (final type in ['question.asked', 'question.updated']) {
        ws.frames.add(WsEvent(type, const {'session_id': 's1', 'id': 'q1'}));
      }
      await tester.pump();
      expect(server.reads, isEmpty);

      ws.frames.add(
        const WsEvent('question.replied', {'session_id': 's1', 'id': 'q1'}),
      );
      await tester.pump();
      expect(server.reads, [
        (turns: chatHistoryTurns, before: null, after: null),
      ]);
      // And takes the card down by itself: the list is not read again.
      expect(server.questionReads, questionReads);
    });
  });

  testWidgets('an app off screen skips its ticks and catches up on the next', (
    tester,
  ) async {
    final server = _Server()
      ..messages = _turns(3)
      ..status = 'busy';
    await _withController(tester, server, (container, ws) async {
      server.reads.clear();
      final visible = container.read(appVisibleProvider.notifier);

      visible.state = false;
      await tester.pump(const Duration(seconds: 3));
      expect(server.reads, isEmpty);

      // On screen again: no burst on the way back, the next tick reads on.
      visible.state = true;
      await tester.pump();
      expect(server.reads, isEmpty);
      await tester.pump(const Duration(seconds: 1));
      expect(server.reads, [(turns: null, before: null, after: 'm05')]);
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
