import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
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
String historyId(int n) => 'm${n.toString().padLeft(2, '0')}';

ChatMessage historyUser(String id, {String? clientId}) => ChatMessage(
  id: id,
  sessionId: 's1',
  role: 'user',
  clientMessageId: clientId,
  parts: [TextPart(id: '$id-text', text: 'ask $id')],
);

ChatMessage historyReply(
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
ChatMessage historyEcho(String clientId) => ChatMessage(
  id: 'tmp-$clientId',
  sessionId: 's1',
  role: 'user',
  clientMessageId: clientId,
  parts: [TextPart(id: 'tmp-part-$clientId', text: 'next')],
);

/// [count] turns, a user message and its reply each, from turn [from].
List<ChatMessage> historyTurns(int count, {int from = 0}) => [
  for (var i = from; i < from + count; i++) ...[
    historyUser(historyId(2 * i)),
    historyReply(historyId(2 * i + 1)),
  ],
];

typedef _Read = ({int? turns, String? before, String? after});

/// A backend holding one transcript. Reads answer at once unless [hold] is
/// set; held reads wait until [release].
class HistoryServer extends ChatApi {
  HistoryServer() : super(Dio());

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

Future<(ProviderContainer, SuggestionWs)> openHistory(ChatApi api) async {
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

List<String> historyIds(ProviderContainer container) => [
  for (final m in container.read(chatStreamProvider).messagesOf('s1')) m.id,
];

/// Opens session s1's controller on [server] and disposes it inside the test
/// body, so its poll timer is gone before the fake clock is checked.
Future<void> withHistoryController(
  WidgetTester tester,
  HistoryServer server,
  Future<void> Function(ProviderContainer, SuggestionWs) body,
) async {
  final (container, ws) = await openHistory(server);
  try {
    container.read(chatSessionProvider('s1'));
    await tester.pump();
    await body(container, ws);
  } finally {
    container.dispose();
    await ws.close();
  }
}

Dio recordingHistoryDio(
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
