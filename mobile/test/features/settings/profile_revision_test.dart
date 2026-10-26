import 'dart:async';

import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

final _owner = StateProvider<String>((ref) => 'a');

class _Api extends AssistantProfileApi {
  _Api() : super(Dio());
  AssistantProfile profile = const AssistantProfile(revision: 8, address: 'A');
  Completer<AssistantProfile>? pendingGet;
  Completer<AssistantProfile>? pendingSave;
  Map<String, Object>? saved;

  @override
  Future<AssistantProfile> get() => pendingGet?.future ?? Future.value(profile);

  @override
  Future<AssistantProfile> save(Map<String, Object> patch) {
    saved = patch;
    return pendingSave!.future;
  }
}

class _Ws extends AgentWsClient {
  _Ws() : super(Dio());
  final frames = StreamController<WsEvent>.broadcast(sync: true);
  @override
  Stream<WsEvent> get events => frames.stream;

  Future<void> closeFrames() => frames.close();
}

void main() {
  late _Api api;
  late _Ws ws;
  late ProviderContainer container;
  setUp(() {
    api = _Api();
    ws = _Ws();
    container = ProviderContainer(
      overrides: [
        assistantProfileOwnerProvider.overrideWith((ref) => ref.watch(_owner)),
        assistantProfileApiProvider.overrideWithValue(api),
        wsClientProvider.overrideWithValue(ws),
      ],
    );
    container.listen(assistantProfileProvider, (_, _) {});
  });
  tearDown(() async {
    container.dispose();
    await ws.closeFrames();
  });

  test('late GET and older events cannot replace an applied version', () async {
    api.pendingGet = Completer();
    final loaded = container.read(assistantProfileProvider.future);
    ws.frames.add(
      const WsEvent(assistantProfileEvent, {
        'userId': 'a',
        'profile': {'revision': 10, 'address': '新称呼', 'language': '英语'},
      }),
    );
    api.pendingGet!.complete(
      const AssistantProfile(revision: 8, address: '旧称呼'),
    );
    await loaded;
    ws.frames.add(
      const WsEvent(assistantProfileEvent, {
        'userId': 'a',
        'profile': {'revision': 9, 'address': '过期通知', 'language': '中文'},
      }),
    );
    expect(
      container.read(assistantProfileProvider).requireValue.address,
      '新称呼',
    );
    expect(
      container.read(assistantProfileProvider).requireValue.language,
      '英语',
    );
    expect(
      container
          .read(assistantProfileProvider)
          .requireValue
          .toJson()['language'],
      '英语',
    );
  });

  test(
    'account change accepts its lower revision and rejects late old save',
    () async {
      await container.read(assistantProfileProvider.future);
      api.pendingSave = Completer();
      final saved = container.read(assistantProfileProvider.notifier).save({
        'address': 'A2',
      });
      final rejected = expectLater(saved, throwsStateError);
      expect(api.saved!['expected_revision'], 8);
      api.profile = const AssistantProfile(revision: 1, address: 'B');
      container.read(_owner.notifier).state = 'b';
      await container.read(assistantProfileProvider.future);
      expect(
        container.read(assistantProfileProvider).requireValue.address,
        'B',
      );
      api.pendingSave!.complete(
        const AssistantProfile(revision: 9, address: 'A2'),
      );
      await rejected;
      expect(
        container.read(assistantProfileProvider).requireValue.address,
        'B',
      );
    },
  );

  test(
    'another account notification never overwrites the current profile',
    () async {
      await container.read(assistantProfileProvider.future);
      ws.frames.add(
        const WsEvent(assistantProfileEvent, {
          'userId': 'b',
          'profile': {'revision': 100, 'address': 'B'},
        }),
      );
      expect(
        container.read(assistantProfileProvider).requireValue.address,
        'A',
      );
    },
  );

  test(
    'reconnect reloads missed changes and keeps receiving newer events',
    () async {
      await container.read(assistantProfileProvider.future);
      api.profile = const AssistantProfile(revision: 10, address: '重新连接');
      ws.frames.add(const WsEvent('__connected', {}));
      await container.pump();
      await container.read(assistantProfileProvider.future);
      expect(
        container.read(assistantProfileProvider).requireValue.address,
        '重新连接',
      );
      ws.frames.add(
        const WsEvent(assistantProfileEvent, {
          'userId': 'a',
          'profile': {'revision': 11, 'address': '继续同步'},
        }),
      );
      expect(
        container.read(assistantProfileProvider).requireValue.address,
        '继续同步',
      );
    },
  );
}
