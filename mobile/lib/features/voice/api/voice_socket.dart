import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../../../shared/api/workspace_scope.dart';
import '../../../shared/config/env.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';
import '../../chat/api/assistant_api.dart';
import 'voice_events.dart';

/// Opens `/ws/assistant/voice` (docs/VOICE_CALL_SPEC.md §5.1): a one-time
/// voice ticket from `POST /api/auth/ticket {"audience": "voice"}` (dio
/// retries once after a token refresh), then `wss://…?ticket=`.
///
/// Not the app's [AgentWsClient]: that one reconnects forever; a call is one
/// socket that ends with the call.
class VoiceConnector {
  VoiceConnector(this._dio, {WsChannelFactory? openChannel})
    : _openChannel = openChannel ?? WebSocketChannel.connect;

  final Dio _dio;
  final WsChannelFactory _openChannel;

  /// The ticket is issued for the call's own user and workspace, never
  /// whatever the app has switched to in the meantime.
  Future<String> fetchTicket(AssistantScope scope) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '/api/auth/ticket',
      data: {'audience': 'voice'},
      options: Options(
        headers: {'X-Workspace-Id': scope.workspaceId},
        extra: {
          requestScopeUserKey: scope.userId,
          requestScopeWorkspaceKey: scope.workspaceId,
        },
      ),
    );
    final ticket = asString(response.data?['ticket']);
    if (ticket == null || ticket.isEmpty) {
      throw StateError('The server issued no voice ticket.');
    }
    return ticket;
  }

  static Uri socketUri(String ticket) => Uri.parse(
    '${Env.wsBase}/ws/assistant/voice',
  ).replace(queryParameters: {'ticket': ticket});

  /// Resolves once the WebSocket handshake is done. The server may still
  /// close straight away with one of the §5.5 codes.
  Future<VoiceSocket> open(AssistantScope scope) async {
    final ticket = await fetchTicket(scope);
    final channel = _openChannel(socketUri(ticket));
    await channel.ready;
    return VoiceSocket(channel);
  }
}

/// One call's socket: JSON events and binary audio in, audio packets and
/// `stop` out.
class VoiceSocket {
  VoiceSocket(this._channel);

  final WebSocketChannel _channel;
  StreamSubscription<dynamic>? _subscription;
  bool _closed = false;

  bool get closed => _closed;

  void listen({
    required void Function(VoiceServerEvent event) onEvent,
    required void Function(Uint8List audio) onAudio,
    required void Function(int? closeCode) onClosed,
  }) {
    _subscription = _channel.stream.listen(
      (data) {
        if (data is String) {
          final event = VoiceServerEvent.decode(data);
          if (event != null) onEvent(event);
        } else if (data is Uint8List) {
          onAudio(data);
        } else if (data is List<int>) {
          onAudio(Uint8List.fromList(data));
        }
      },
      // The close that follows says how it ended.
      onError: (Object _) {},
      onDone: () {
        _closed = true;
        onClosed(_channel.closeCode);
      },
    );
  }

  void sendAudio(Uint8List packet) {
    if (!_closed) _channel.sink.add(packet);
  }

  void sendStop() {
    if (!_closed) _channel.sink.add(jsonEncode({'type': 'stop'}));
  }

  /// Our side is done; no close callback follows. Nothing is awaited: a
  /// server that never answers the close handshake must not hold the call.
  void close() {
    if (_closed) return;
    _closed = true;
    unawaited(
      Future<void>.sync(
        () => _channel.sink.close(1000),
      ).catchError((Object _) {}),
    );
    unawaited(_subscription?.cancel());
    _subscription = null;
  }
}
