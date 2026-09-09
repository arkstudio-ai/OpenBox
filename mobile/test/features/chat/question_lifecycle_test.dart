import 'dart:async';

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _LocalWs extends AgentWsClient {
  _LocalWs() : super(Dio());

  @override
  Future<void> connect() async {}

  @override
  Stream<WsEvent> get events => const Stream.empty();
}

class _Api extends ChatApi {
  _Api() : super(Dio());

  List<QuestionRequest> questions = [];
  final abortGate = Completer<void>();
  bool holdReads = false;
  final reads = <Completer<List<QuestionRequest>>>[];

  @override
  Future<void> abort(String sessionId) => abortGate.future;

  @override
  Future<Session> getSession(String sessionId) async =>
      Session.fromJson({'id': sessionId, 'status': 'waiting_input'});

  @override
  Future<List<ChatMessage>> listMessages(
    String sessionId, {
    int offset = 0,
    int limit = 200,
  }) async => [];

  @override
  Future<List<PermissionRequest>> listPermissions() async => [];

  @override
  Future<List<QuestionRequest>> listQuestions() async {
    if (!holdReads) return [...questions];
    final read = Completer<List<QuestionRequest>>();
    reads.add(read);
    return read.future;
  }
}

QuestionRequest _question(String id) => QuestionRequest(
  id: id,
  sessionId: 's1',
  questions: const [QuestionItem(question: 'Confirm this version?')],
);

Future<ProviderContainer> _container(_Api api) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final container = ProviderContainer(
    overrides: [
      chatApiProvider.overrideWithValue(api),
      apiDioProvider.overrideWithValue(Dio()),
      prefsProvider.overrideWithValue(prefs),
      wsClientProvider.overrideWithValue(_LocalWs()),
    ],
  );
  addTearDown(container.dispose);
  return container;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'a pending HTTP snapshot cannot erase an ask received during the read',
    () async {
      final api = _Api()..holdReads = true;
      final container = await _container(api);
      final pending = container.read(pendingProvider.notifier);
      final read = pending.refreshQuestions();
      pending.addQuestion(_question('arrived-over-ws'));
      api.reads.single.complete([]);
      await read;
      expect(
        container.read(pendingProvider).questionsOf('s1').single.id,
        'arrived-over-ws',
      );
    },
  );

  test(
    'a fresh empty snapshot closes offline-resolved asks against late events',
    () async {
      final api = _Api();
      final container = await _container(api);
      final pending = container.read(pendingProvider.notifier);
      pending.addQuestion(_question('old'));
      await pending.refreshQuestions();
      pending.addQuestion(_question('old'));
      expect(container.read(pendingProvider).questionsOf('s1'), isEmpty);
    },
  );

  test('failed refresh preserves pending input and its draft', () async {
    final api = _Api()..holdReads = true;
    final container = await _container(api);
    final pending = container.read(pendingProvider.notifier);
    pending.addQuestion(_question('kept'));
    final read = pending.refreshQuestions();
    api.reads.single.completeError(Exception('offline'));
    await read;
    expect(container.read(pendingProvider).questionsOf('s1').single.id, 'kept');
  });

  test('a delayed stop acknowledgement cannot remove a newer ask', () async {
    final api = _Api()..questions = [_question('old')];
    final container = await _container(api);
    final controller = container.read(chatSessionProvider('s1').notifier);
    await Future<void>.delayed(Duration.zero);
    final pending = container.read(pendingProvider.notifier);
    expect(container.read(pendingProvider).questionsOf('s1').single.id, 'old');

    final stop = controller.stop();
    expect(container.read(pendingProvider).questionsOf('s1').single.id, 'old');
    final newer = _question('new');
    api.questions = [newer];
    pending.addQuestion(newer);
    api.abortGate.complete();
    await stop;
    await Future<void>.delayed(Duration.zero);

    expect(container.read(pendingProvider).questionsOf('s1').map((q) => q.id), [
      'new',
    ]);
  });

  test(
    'an older reconnect snapshot cannot overwrite a newer refresh',
    () async {
      final api = _Api()..holdReads = true;
      final container = await _container(api);
      final pending = container.read(pendingProvider.notifier);
      final older = pending.refreshAll();
      final newer = pending.refreshQuestions();
      expect(api.reads, hasLength(2));

      api.reads[1].complete([_question('new')]);
      await newer;
      api.reads[0].complete([]);
      await older;

      expect(
        container.read(pendingProvider).questionsOf('s1').single.id,
        'new',
      );
    },
  );
}
