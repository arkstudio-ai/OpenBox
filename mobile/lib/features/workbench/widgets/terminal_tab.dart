import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:xterm/xterm.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/config/env.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/ws/ws_client.dart';
import '../utils/terminal_protocol.dart';

/// PTY terminal (web `TerminalView.tsx`): binary frames = 1-byte tag +
/// payload — `0x00` DATA (both directions), `0x01` RESIZE (cols, rows as
/// big-endian uint16). Text frames are JSON `{type:"error",data}`.
/// Connects to the project-scoped `/ws/terminal/{containerId}` endpoint.
class TerminalTab extends ConsumerStatefulWidget {
  const TerminalTab({
    super.key,
    required this.containerId,
    required this.sessionId,
    this.projectId,
  });

  final String containerId;
  final String sessionId;
  final String? projectId;

  TerminalConnectionIdentity get connectionIdentity =>
      TerminalConnectionIdentity(
        containerId: containerId,
        sessionId: sessionId,
        projectId: projectId,
      );

  @override
  ConsumerState<TerminalTab> createState() => _TerminalTabState();
}

class _TerminalTabState extends ConsumerState<TerminalTab> {
  late Terminal _terminal;
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  TerminalUtf8StreamDecoder? _decoder;
  int _connectionGeneration = 0;
  bool _connecting = true;
  bool _disconnected = false;

  @override
  void initState() {
    super.initState();
    _terminal = Terminal(maxLines: 10000);
    unawaited(_connect(++_connectionGeneration));
  }

  @override
  void didUpdateWidget(covariant TerminalTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.connectionIdentity != widget.connectionIdentity) {
      _restartConnection();
    }
  }

  void _restartConnection() {
    final oldChannel = _channel;
    final oldSubscription = _subscription;
    _connectionGeneration += 1;
    _channel = null;
    _subscription = null;
    _decoder?.close();
    _decoder = null;
    _terminal = Terminal(maxLines: 10000);
    setState(() {
      _connecting = true;
      _disconnected = false;
    });
    unawaited(oldSubscription?.cancel());
    unawaited(oldChannel?.sink.close());
    unawaited(_connect(_connectionGeneration));
  }

  Future<void> _connect(int generation) async {
    final identity = widget.connectionIdentity;
    try {
      final ticket = await ref.read(wsClientProvider).fetchTicket();
      if (!mounted || generation != _connectionGeneration) return;
      final channel = WebSocketChannel.connect(
        terminalWebSocketUri(
          wsBase: Env.wsBase,
          containerId: identity.containerId,
          ticket: ticket,
          sessionId: identity.sessionId,
          projectId: identity.projectId,
        ),
      );
      await channel.ready;
      if (!mounted || generation != _connectionGeneration) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      final terminal = _terminal;
      _decoder = TerminalUtf8StreamDecoder(terminal.write);
      setState(() => _connecting = false);

      terminal.onOutput = (data) {
        final bytes = utf8.encode(data);
        channel.sink.add(Uint8List.fromList([0x00, ...bytes]));
      };
      terminal.onResize = (cols, rows, _, _) {
        final frame = ByteData(5)
          ..setUint8(0, 0x01)
          ..setUint16(1, cols)
          ..setUint16(3, rows);
        channel.sink.add(frame.buffer.asUint8List());
      };
      // Announce the initial size.
      terminal.onResize?.call(terminal.viewWidth, terminal.viewHeight, 0, 0);

      _subscription = channel.stream.listen(
        (frame) {
          if (!mounted || generation != _connectionGeneration) return;
          if (frame is List<int>) {
            if (frame.isNotEmpty && frame.first == 0x00) {
              _decoder?.add(frame.sublist(1));
            }
          } else if (frame is String) {
            // Text frames are JSON `{type:"error", data}` only.
            try {
              final parsed = jsonDecode(frame);
              if (parsed is Map<String, dynamic> && parsed['type'] == 'error') {
                _markDisconnected(generation);
              }
            } on FormatException {
              terminal.write(frame);
            }
          }
        },
        onDone: () => _markDisconnected(generation),
        onError: (Object _) => _markDisconnected(generation),
      );
    } catch (_) {
      if (mounted && generation == _connectionGeneration) {
        setState(() {
          _connecting = false;
          _disconnected = true;
        });
      }
    }
  }

  void _markDisconnected(int generation) {
    if (!mounted || generation != _connectionGeneration) return;
    _decoder?.close();
    _decoder = null;
    setState(() {
      _connecting = false;
      _disconnected = true;
    });
  }

  @override
  void dispose() {
    _connectionGeneration += 1;
    unawaited(_subscription?.cancel());
    _decoder?.close();
    unawaited(_channel?.sink.close());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (_connecting) {
      return Center(
        child: Text(
          i18n.t('workbench:terminal.connecting'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      );
    }
    return Container(
      margin: const EdgeInsets.all(10),
      padding: const EdgeInsets.all(8),
      decoration: BoxDecoration(
        color: t.term,
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Column(
        children: [
          if (_disconnected)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text(
                i18n.t('workbench:terminal.disconnected'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.dangerInk),
              ),
            ),
          Expanded(
            child: TerminalView(
              _terminal,
              textStyle: const TerminalStyle(
                fontSize: 12.5,
                fontFamily: 'Menlo',
              ),
              theme: TerminalThemes.defaultTheme,
              backgroundOpacity: 0,
            ),
          ),
        ],
      ),
    );
  }
}
