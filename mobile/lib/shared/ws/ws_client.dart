import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../api/providers.dart';
import '../config/env.dart';
import '../models/json.dart';

/// One app-global agent WebSocket, mirroring frontend-v2
/// `shared/ws/client.ts`:
/// - handshake: `POST /api/auth/ticket` (bearer; dio already retries once
///   after refresh on 401) → `ws(s)://…/ws/agent?ticket=<t>`
/// - frames: `{type, data}` (`event` tolerated as alias); unknown ignored
/// - reconnect: exponential backoff `min(30s, 1s·2^attempt)`, reset on open
/// - watchdog: a socket with no frame for [AgentWsClient.silenceLimit] is
///   dropped and reconnected like one that closed
/// - synthetic local events: `__connected` / `__disconnected`
class WsEvent {
  const WsEvent(this.type, this.data);

  final String type;
  final Map<String, dynamic> data;

  String? get sessionId =>
      asString(data['sessionId']) ?? asString(data['session_id']);
}

/// Opens a socket to [uri]: [WebSocketChannel.connect], unless a test hands
/// in one that needs no network.
typedef WsChannelFactory = WebSocketChannel Function(Uri uri);

class AgentWsClient {
  AgentWsClient(this._dio, {WsChannelFactory? openChannel})
    : _openChannel = openChannel ?? WebSocketChannel.connect;

  /// The backend sends `server.heartbeat` every 25 s (`api/ws.py`). A minute
  /// without any frame means the socket died without closing — a network
  /// switch, a proxy that dropped it — which nothing else notices: it stayed
  /// connected while every event, a run's end among them, went nowhere.
  static const silenceLimit = Duration(seconds: 60);

  final Dio _dio;
  final WsChannelFactory _openChannel;
  final _events = StreamController<WsEvent>.broadcast();

  WebSocketChannel? _channel;
  int _generation = 0;
  int _attempt = 0;
  Timer? _reconnectTimer;
  Timer? _watchdog;
  Future<void>? _connecting;
  bool _closed = false;

  Stream<WsEvent> get events => _events.stream;

  /// A socket is open right now. `__connected` only reaches listeners that
  /// were already subscribed when the handshake finished; a later one asks.
  bool get connected => _channel != null;

  Future<String> fetchTicket() async {
    final resp = await _dio.post<Map<String, dynamic>>('/api/auth/ticket');
    return asString(resp.data?['ticket']) ?? '';
  }

  /// Idempotent; shares one in-flight handshake.
  Future<void> connect() {
    _closed = false;
    if (_channel != null) return Future.value();
    return _connecting ??= _doConnect().whenComplete(() => _connecting = null);
  }

  Future<void> _doConnect() async {
    final generation = ++_generation;
    try {
      final ticket = await fetchTicket();
      if (generation != _generation || _closed) return;
      final channel = _openChannel(
        Uri.parse('${Env.wsBase}/ws/agent?ticket=$ticket'),
      );
      await channel.ready;
      if (generation != _generation || _closed) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      _attempt = 0;
      _watch(generation);
      _events.add(const WsEvent('__connected', {}));
      channel.stream.listen(
        (raw) {
          // A dropped socket's late frames belong to no one.
          if (generation != _generation) return;
          _watch(generation);
          _onFrame(raw);
        },
        onDone: () => _onClosed(generation),
        onError: (Object _) => _onClosed(generation),
        cancelOnError: true,
      );
    } catch (_) {
      if (generation == _generation && !_closed) _scheduleReconnect();
    }
  }

  void _onFrame(dynamic raw) {
    if (raw is! String) return;
    dynamic parsed;
    try {
      parsed = jsonDecode(raw);
    } on FormatException {
      return; // non-JSON frames ignored
    }
    if (parsed is! Map<String, dynamic>) return;
    final type = asString(parsed['type']) ?? asString(parsed['event']);
    if (type == null) return;
    final data = parsed['data'] is Map<String, dynamic>
        ? parsed['data'] as Map<String, dynamic>
        : parsed;
    _events.add(WsEvent(type, data));
  }

  /// Start the silence countdown again for the socket of [generation].
  void _watch(int generation) {
    _watchdog?.cancel();
    _watchdog = Timer(silenceLimit, () => _drop(generation));
  }

  /// The socket went silent. Let it go without waiting on a close handshake
  /// it cannot finish, and reconnect as though it had closed.
  void _drop(int generation) {
    final channel = _channel;
    if (generation != _generation || channel == null) return;
    // Its onDone, onError and frames, whenever they come, now answer to an
    // older generation, and a replacement never hears them.
    _generation++;
    _channel = null;
    channel.sink.close().ignore();
    _events.add(const WsEvent('__disconnected', {}));
    if (!_closed) _scheduleReconnect();
  }

  void _onClosed(int generation) {
    if (generation != _generation) return;
    _watchdog?.cancel();
    _channel = null;
    _events.add(const WsEvent('__disconnected', {}));
    if (!_closed) _scheduleReconnect();
  }

  void _scheduleReconnect() {
    _reconnectTimer?.cancel();
    final delay = math.min(30000, 1000 * math.pow(2, _attempt).toInt());
    _attempt += 1;
    _reconnectTimer = Timer(Duration(milliseconds: delay), () {
      if (!_closed && _channel == null) connect();
    });
  }

  /// Sign-out teardown; no auto-reconnect afterwards.
  void disconnect() {
    _closed = true;
    _generation += 1;
    _reconnectTimer?.cancel();
    _watchdog?.cancel();
    _channel?.sink.close();
    _channel = null;
  }
}

final wsClientProvider = Provider<AgentWsClient>(
  (ref) => AgentWsClient(ref.watch(apiDioProvider)),
);
