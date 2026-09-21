import 'dart:async';
import 'dart:convert';

import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// A socket with no network behind it: [send] delivers a frame, [finish]
/// reports the close, and closing its sink is only recorded.
class _Channel implements WebSocketChannel {
  final _frames = StreamController<dynamic>();
  bool closed = false;
  final sent = <Map<String, dynamic>>[];

  void send(String type) =>
      _frames.add(jsonEncode({'type': type, 'data': <String, Object>{}}));

  Future<void> finish() => _frames.close();

  @override
  Future<void> get ready => Future.value();

  @override
  Stream<dynamic> get stream => _frames.stream;

  @override
  WebSocketSink get sink => _Sink(this);

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _Sink implements WebSocketSink {
  _Sink(this._channel);

  final _Channel _channel;

  @override
  void add(dynamic data) =>
      _channel.sent.add(jsonDecode(data as String) as Map<String, dynamic>);

  @override
  Future<void> close([int? closeCode, String? closeReason]) async {
    _channel.closed = true;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _Client extends AgentWsClient {
  _Client(WsChannelFactory openChannel)
    : super(Dio(), openChannel: openChannel);

  @override
  Future<String> fetchTicket() async => 'ticket';
}

/// A client whose sockets are [_Channel]s, and every event it emitted.
class _Socket {
  _Socket() {
    client = _Client((_) {
      final channel = _Channel();
      channels.add(channel);
      return channel;
    });
    _listening = client.events.listen((event) => events.add(event.type));
  }

  late final AgentWsClient client;
  final channels = <_Channel>[];
  final events = <String>[];
  late final StreamSubscription<WsEvent> _listening;

  /// Leaves no socket or timer behind. Nothing here is awaited: a cancelled
  /// subscription answers with a future from outside the test's fake clock,
  /// and a test that ends waiting on one never ends.
  void close() {
    client.disconnect();
    unawaited(_listening.cancel());
  }
}

Future<_Socket> _open(WidgetTester tester) async {
  final socket = _Socket();
  unawaited(socket.client.connect());
  await tester.pump();
  return socket;
}

void main() {
  testWidgets(
    'member subscriptions are scoped, reference counted and restored after reconnect',
    (tester) async {
      final socket = await _open(tester);
      final first = socket.client.subscribeSessions(['member-a']);
      final second = socket.client.subscribeSessions(['member-a', 'member-b']);
      expect(socket.channels.single.sent.last['sessionIds'], [
        'member-a',
        'member-b',
      ]);
      first();
      first();
      expect(socket.channels.single.sent.last['sessionIds'], [
        'member-a',
        'member-b',
      ]);
      await socket.channels.single.finish();
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));
      expect(socket.channels.last.sent.single, {
        'type': 'session.subscribe',
        'sessionIds': ['member-a', 'member-b'],
      });
      second();
      expect(socket.channels.last.sent.last['sessionIds'], isEmpty);
      socket.close();
    },
  );
  testWidgets('sign out discards old member subscriptions', (tester) async {
    final socket = await _open(tester);
    socket.client.subscribeSessions(['old-owner-member']);
    socket.client.disconnect();
    unawaited(socket.client.connect());
    await tester.pump();
    expect(socket.channels.last.sent, isEmpty);
    socket.close();
  });
  testWidgets('a socket silent for a minute is dropped and opened again', (
    tester,
  ) async {
    final socket = await _open(tester);
    expect(socket.client.connected, isTrue);
    expect(socket.events, ['__connected']);

    await tester.pump(AgentWsClient.silenceLimit - const Duration(seconds: 1));
    expect(socket.client.connected, isTrue);
    await tester.pump(const Duration(seconds: 1));
    expect(socket.client.connected, isFalse);
    expect(socket.channels.single.closed, isTrue);
    expect(socket.events, ['__connected', '__disconnected']);

    // The usual backoff, then a new socket.
    await tester.pump(const Duration(seconds: 1));
    expect(socket.channels, hasLength(2));
    expect(socket.client.connected, isTrue);
    expect(socket.events.last, '__connected');
    socket.close();
  });

  testWidgets('heartbeats keep a quiet socket open', (tester) async {
    final socket = await _open(tester);
    for (var beat = 0; beat < 4; beat++) {
      await tester.pump(const Duration(seconds: 25));
      socket.channels.single.send('server.heartbeat');
      await tester.pump();
    }
    // Open well over a minute, and 59 s since the last beat.
    await tester.pump(const Duration(seconds: 59));
    expect(socket.client.connected, isTrue);
    expect(socket.channels, hasLength(1));
    expect(socket.events, isNot(contains('__disconnected')));

    // Still watched: silence after the beats is dropped all the same.
    await tester.pump(const Duration(seconds: 1));
    expect(socket.client.connected, isFalse);
    socket.close();
  });

  testWidgets('the dropped socket closing late leaves its replacement alone', (
    tester,
  ) async {
    final socket = await _open(tester);
    await tester.pump(AgentWsClient.silenceLimit);
    await tester.pump(const Duration(seconds: 1));
    expect(socket.channels, hasLength(2));
    expect(socket.client.connected, isTrue);

    // The dead socket delivers one last frame, then reports its close.
    final dead = socket.channels.first;
    dead.send('session.status');
    await dead.finish();
    await tester.pump();
    expect(socket.client.connected, isTrue);
    expect(socket.events, ['__connected', '__disconnected', '__connected']);

    // Nor does it start a reconnect behind the live socket's back.
    await tester.pump(const Duration(seconds: 30));
    expect(socket.channels, hasLength(2));
    expect(socket.client.connected, isTrue);
    socket.close();
  });
}
