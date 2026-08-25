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

/// PTY terminal (web `TerminalView.tsx`): binary frames = 1-byte tag +
/// payload — `0x00` DATA (both directions), `0x01` RESIZE (cols, rows as
/// big-endian uint16). Text frames are JSON `{type:"error",data}`.
/// Connects to `/ws/terminal/{containerId}?ticket=`.
class TerminalTab extends ConsumerStatefulWidget {
  const TerminalTab({super.key, required this.containerId});

  final String containerId;

  @override
  ConsumerState<TerminalTab> createState() => _TerminalTabState();
}

class _TerminalTabState extends ConsumerState<TerminalTab> {
  final _terminal = Terminal(maxLines: 10000);
  WebSocketChannel? _channel;
  bool _connecting = true;
  bool _disconnected = false;

  @override
  void initState() {
    super.initState();
    unawaited(_connect());
  }

  Future<void> _connect() async {
    try {
      final ticket = await ref.read(wsClientProvider).fetchTicket();
      final channel = WebSocketChannel.connect(
        Uri.parse(
            '${Env.wsBase}/ws/terminal/${widget.containerId}?ticket=$ticket'),
      );
      await channel.ready;
      if (!mounted) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      setState(() => _connecting = false);

      _terminal.onOutput = (data) {
        final bytes = utf8.encode(data);
        channel.sink.add(Uint8List.fromList([0x00, ...bytes]));
      };
      _terminal.onResize = (cols, rows, _, _) {
        final frame = ByteData(5)
          ..setUint8(0, 0x01)
          ..setUint16(1, cols)
          ..setUint16(3, rows);
        channel.sink.add(frame.buffer.asUint8List());
      };
      // Announce the initial size.
      _terminal.onResize
          ?.call(_terminal.viewWidth, _terminal.viewHeight, 0, 0);

      channel.stream.listen(
        (frame) {
          if (frame is List<int>) {
            if (frame.isNotEmpty && frame.first == 0x00) {
              _terminal.write(utf8.decode(frame.sublist(1), allowMalformed: true));
            }
          } else if (frame is String) {
            _terminal.write(frame);
          }
        },
        onDone: () {
          if (mounted) setState(() => _disconnected = true);
        },
        onError: (Object _) {
          if (mounted) setState(() => _disconnected = true);
        },
      );
    } catch (_) {
      if (mounted) {
        setState(() {
          _connecting = false;
          _disconnected = true;
        });
      }
    }
  }

  @override
  void dispose() {
    _channel?.sink.close();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (_connecting) {
      return Center(
        child: Text(i18n.t('workbench:terminal.connecting'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
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
