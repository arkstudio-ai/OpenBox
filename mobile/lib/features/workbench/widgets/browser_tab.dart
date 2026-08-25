import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/config/env.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/ws/ws_client.dart';

enum _Phase { connecting, streaming, failed, noSandbox }

/// Dev-browser tab (web `BrowserTab.tsx`): address bar + a live screenshot
/// stream over `/ws/dev-browser/auto?ticket=`. Binary frames are JPEG page
/// images; text frames are JSON `{type:"url"|"navigated", url}`; we send
/// navigate/back/reload/click/scroll. Close 4004 → no sandbox.
class BrowserTab extends ConsumerStatefulWidget {
  const BrowserTab({super.key});

  @override
  ConsumerState<BrowserTab> createState() => _BrowserTabState();
}

class _BrowserTabState extends ConsumerState<BrowserTab> {
  final _url = TextEditingController();
  _Phase _phase = _Phase.connecting;
  WebSocketChannel? _channel;
  Uint8List? _frame;
  int _frameW = 0;
  int _frameH = 0;
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    unawaited(_connect());
  }

  @override
  void dispose() {
    _disposed = true;
    _channel?.sink.close();
    _url.dispose();
    super.dispose();
  }

  Future<void> _connect() async {
    try {
      final ticket = await ref.read(wsClientProvider).fetchTicket();
      final channel = WebSocketChannel.connect(
        Uri.parse('${Env.wsBase}/ws/dev-browser/auto?ticket=$ticket'),
      );
      await channel.ready;
      if (_disposed) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      channel.stream.listen(
        _onFrame,
        onDone: () {
          if (_disposed) return;
          setState(() => _phase =
              channel.closeCode == 4004 ? _Phase.noSandbox : _Phase.failed);
        },
        onError: (Object _) {
          if (!_disposed) setState(() => _phase = _Phase.failed);
        },
      );
    } catch (_) {
      if (!_disposed) setState(() => _phase = _Phase.failed);
    }
  }

  void _onFrame(dynamic data) {
    if (data is List<int>) {
      final bytes = Uint8List.fromList(data);
      // Frame dimensions drive click-coordinate mapping; decode once.
      if (_frameW == 0) {
        ui.decodeImageFromList(bytes, (image) {
          if (!_disposed) {
            setState(() {
              _frameW = image.width;
              _frameH = image.height;
            });
          }
        });
      }
      setState(() {
        _frame = bytes;
        _phase = _Phase.streaming;
      });
    } else if (data is String) {
      try {
        final parsed = jsonDecode(data) as Map<String, dynamic>;
        final type = asString(parsed['type']);
        final url = asString(parsed['url']);
        if ((type == 'url' || type == 'navigated') && url != null) {
          _url.text = url;
        }
      } on FormatException {
        // ignore non-JSON text
      }
    }
  }

  void _send(Map<String, Object?> message) {
    _channel?.sink.add(jsonEncode(message));
  }

  /// Map a tap inside the contain-fit image back to page pixels.
  void _onTapUp(TapUpDetails details, BoxConstraints constraints) {
    if (_frameW == 0 || _frameH == 0) return;
    final scale = [
      constraints.maxWidth / _frameW,
      constraints.maxHeight / _frameH,
    ].reduce((a, b) => a < b ? a : b);
    final drawW = _frameW * scale;
    final drawH = _frameH * scale;
    final offsetX = (constraints.maxWidth - drawW) / 2;
    final offsetY = (constraints.maxHeight - drawH) / 2;
    final local = details.localPosition;
    if (local.dx < offsetX ||
        local.dy < offsetY ||
        local.dx > offsetX + drawW ||
        local.dy > offsetY + drawH) {
      return;
    }
    _send({
      'type': 'click',
      'x': ((local.dx - offsetX) / scale).round(),
      'y': ((local.dy - offsetY) / scale).round(),
      'button': 0,
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (_phase == _Phase.noSandbox) {
      return Center(
        child: Text(i18n.t('workbench:sandbox.none'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(10, 6, 10, 8),
          child: Row(
            children: [
              IconButton(
                visualDensity: VisualDensity.compact,
                icon: Icon(Icons.chevron_left, size: 19, color: t.n700),
                onPressed: () => _send({'type': 'back'}),
              ),
              Expanded(
                child: SizedBox(
                  height: 34,
                  child: TextField(
                    controller: _url,
                    onSubmitted: (value) {
                      final url = value.trim();
                      if (url.isNotEmpty) _send({'type': 'navigate', 'url': url});
                    },
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      color: t.n700,
                      fontFamily: 'Menlo',
                      fontFamilyFallback: const ['monospace'],
                    ),
                    decoration: InputDecoration(
                      hintText: i18n.t('workbench:browser.placeholder'),
                      hintStyle:
                          TextStyle(fontSize: FontSizes.xs, color: t.n500),
                      isDense: true,
                      filled: true,
                      fillColor: t.card,
                      contentPadding: const EdgeInsets.symmetric(
                          horizontal: 14, vertical: 8),
                      enabledBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(Radii.full),
                        borderSide: BorderSide(color: t.hair),
                      ),
                      focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(Radii.full),
                        borderSide: BorderSide(color: t.n400),
                      ),
                    ),
                  ),
                ),
              ),
              IconButton(
                visualDensity: VisualDensity.compact,
                icon: Icon(Icons.refresh, size: 17, color: t.n700),
                onPressed: () => _send({'type': 'reload'}),
              ),
            ],
          ),
        ),
        Expanded(
          child: Container(
            margin: const EdgeInsets.fromLTRB(10, 0, 10, 10),
            clipBehavior: Clip.antiAlias,
            decoration: BoxDecoration(
              color: t.card,
              borderRadius: BorderRadius.circular(Radii.xl2),
              border: Border.all(color: t.hair),
            ),
            child: _phase == _Phase.streaming && _frame != null
                ? LayoutBuilder(
                    builder: (context, constraints) => GestureDetector(
                      onTapUp: (d) => _onTapUp(d, constraints),
                      onPanUpdate: (d) => _send({
                        'type': 'scroll',
                        'dx': (-d.delta.dx).round(),
                        'dy': (-d.delta.dy).round(),
                      }),
                      child: Center(
                        child: Image.memory(
                          _frame!,
                          gaplessPlayback: true,
                          fit: BoxFit.contain,
                        ),
                      ),
                    ),
                  )
                : Center(
                    child: Text(
                      _phase == _Phase.failed
                          ? i18n.t('workbench:browser.failed')
                          : i18n.t('workbench:browser.loading'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                    ),
                  ),
          ),
        ),
      ],
    );
  }
}
