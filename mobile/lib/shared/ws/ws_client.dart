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
/// - synthetic local events: `__connected` / `__disconnected`
class WsEvent {
  const WsEvent(this.type, this.data);

  final String type;
  final Map<String, dynamic> data;

  String? get sessionId => asString(data['sessionId']);
}

class AgentWsClient {
  AgentWsClient(this._dio);

  final Dio _dio;
  final _events = StreamController<WsEvent>.broadcast();

  WebSocketChannel? _channel;
  int _generation = 0;
  int _attempt = 0;
  Timer? _reconnectTimer;
  Future<void>? _connecting;
  bool _closed = false;

  Stream<WsEvent> get events => _events.stream;

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
      final channel = WebSocketChannel.connect(
        Uri.parse('${Env.wsBase}/ws/agent?ticket=$ticket'),
      );
      await channel.ready;
      if (generation != _generation || _closed) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      _attempt = 0;
      _events.add(const WsEvent('__connected', {}));
      channel.stream.listen(
        _onFrame,
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

  void _onClosed(int generation) {
    if (generation != _generation) return;
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
    _channel?.sink.close();
    _channel = null;
  }
}

final wsClientProvider = Provider<AgentWsClient>(
  (ref) => AgentWsClient(ref.watch(apiDioProvider)),
);
