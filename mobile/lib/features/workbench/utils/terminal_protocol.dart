import 'dart:convert';

/// Everything that identifies one interactive terminal connection.
///
/// Keeping the session in the identity is important: the WUYING container is
/// shared during development, while each session still belongs to one exact
/// project directory.
class TerminalConnectionIdentity {
  const TerminalConnectionIdentity({
    required this.containerId,
    required this.sessionId,
    this.projectId,
  });

  final String containerId;
  final String sessionId;
  final String? projectId;

  @override
  bool operator ==(Object other) =>
      other is TerminalConnectionIdentity &&
      other.containerId == containerId &&
      other.sessionId == sessionId &&
      other.projectId == projectId;

  @override
  int get hashCode => Object.hash(containerId, sessionId, projectId);
}

/// Builds the terminal URL without string concatenation so tickets and
/// Unicode identifiers are encoded exactly once.
Uri terminalWebSocketUri({
  required String wsBase,
  required String containerId,
  required String ticket,
  required String sessionId,
  String? projectId,
}) {
  final base = Uri.parse(wsBase);
  final query = <String, String>{
    'ticket': ticket,
    'session_id': sessionId,
    if (projectId != null && projectId.isNotEmpty) 'project_id': projectId,
  };
  return base.replace(
    pathSegments: [
      ...base.pathSegments.where((segment) => segment.isNotEmpty),
      'ws',
      'terminal',
      containerId,
    ],
    queryParameters: query,
    fragment: '',
  );
}

/// Incremental UTF-8 decoder for PTY frames.
///
/// A single Unicode scalar may be split between two WebSocket binary frames.
/// Decoding each frame independently turns that valid text into replacement
/// characters. Dart's chunked decoder holds an incomplete suffix until the
/// following frame arrives.
class TerminalUtf8StreamDecoder {
  TerminalUtf8StreamDecoder(void Function(String text) onText) {
    _sink = const Utf8Decoder(
      allowMalformed: true,
    ).startChunkedConversion(_CallbackStringSink(onText));
  }

  late final ByteConversionSink _sink;
  bool _closed = false;

  void add(List<int> bytes) {
    if (_closed || bytes.isEmpty) return;
    _sink.add(bytes);
  }

  void close() {
    if (_closed) return;
    _closed = true;
    _sink.close();
  }
}

class _CallbackStringSink extends StringConversionSinkBase {
  _CallbackStringSink(this._onText);

  final void Function(String text) _onText;

  @override
  void add(String str) => _onText(str);

  @override
  void addSlice(String chunk, int start, int end, bool isLast) {
    if (start < end) _onText(chunk.substring(start, end));
    if (isLast) close();
  }

  @override
  void close() {}
}
