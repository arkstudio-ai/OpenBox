import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/widgets/spinner.dart';
import 'desktop_bridge.dart';

enum _Phase { loading, connected, error, closed }

/// 云桌面 tab (web `DesktopTab.tsx`): the sandbox's Wuying cloud desktop.
/// The Web SDK is JS-only, so mobile hosts the same bootstrap in a WebView
/// ([desktopBootstrapHtml]); the one-time ticket (202-pending polled) is
/// fetched natively. Read-only by default — taking the pointer is an explicit
/// toggle, and no machine ids ever reach the UI.
///
/// Two things a phone needs that the desktop client does not: landscape
/// fullscreen, without which a 1024×768 stream is unreadable, and the
/// desktop's own on-stream keyboard, because the phone's keyboard cannot
/// reach the guest.
class DesktopTab extends ConsumerStatefulWidget {
  const DesktopTab({super.key, this.onImmersive});

  /// Raised while the viewer is in landscape fullscreen so the hosting page
  /// can drop its app bar. The WebView keeps its place in the tree either way
  /// — re-parenting it would tear the stream down and reconnect.
  final ValueChanged<bool>? onImmersive;

  @override
  ConsumerState<DesktopTab> createState() => _DesktopTabState();
}

class _DesktopTabState extends ConsumerState<DesktopTab> {
  _Phase _phase = _Phase.loading;
  String _detail = '';
  bool _control = false;
  bool _fullscreen = false;
  bool _keyboard = false;
  bool _alive = true;
  WebViewController? _webView;

  @override
  void initState() {
    super.initState();
    unawaited(_connect());
  }

  @override
  void dispose() {
    _alive = false;
    // Leaving mid-fullscreen must not strand the rest of the app sideways.
    if (_fullscreen) unawaited(_restoreChrome());
    super.dispose();
  }

  Future<void> _restoreChrome() async {
    await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
  }

  /// Poll `/api/desktop/ticket` through its 202-pending window
  /// (web `fetchTicket`: 30 attempts × 3s).
  Future<Map<String, dynamic>> _fetchTicket() async {
    final dio = ref.read(apiDioProvider);
    var taskId = '';
    for (var attempt = 0; attempt < 30 && _alive; attempt++) {
      final resp = await dio.get<Map<String, dynamic>>(
        '/api/desktop/ticket',
        queryParameters: taskId.isEmpty ? null : {'task_id': taskId},
      );
      final data = resp.data ?? const {};
      if (asString(data['ticket']) != null) return data;
      taskId = asString(data['taskId']) ?? taskId;
      await Future<void>.delayed(const Duration(seconds: 3));
    }
    throw TimeoutException('desktop ticket');
  }

  Future<void> _connect() async {
    try {
      final ticket = await _fetchTicket();
      if (!_alive) return;
      final controller = WebViewController();
      await controller.setJavaScriptMode(JavaScriptMode.unrestricted);
      await controller.setBackgroundColor(Colors.transparent);
      await controller.addJavaScriptChannel('Bossip',
          onMessageReceived: (msg) {
        if (!_alive) return;
        final data = jsonDecode(msg.message) as Map<String, dynamic>;
        setState(() {
          switch (asString(data['event'])) {
            case 'connected':
              _phase = _Phase.connected;
            case 'disconnected':
              _phase = _Phase.closed;
            case 'error':
              _phase = _Phase.error;
              final detail = asString(data['detail']) ?? '';
              _detail = detail == 'sdk'
                  ? ref.read(i18nProvider).t('workbench:desktop.sdkFailed')
                  : detail;
          }
        });
      });
      await controller.loadHtmlString(desktopBootstrapHtml(ticket),
          baseUrl: 'https://bossip.desktop');
      if (!_alive) return;
      setState(() => _webView = controller);
    } on TimeoutException {
      if (_alive) {
        setState(() {
          _phase = _Phase.error;
          _detail = ref.read(i18nProvider).t('workbench:desktop.error');
        });
      }
    } catch (e) {
      if (!_alive) return;
      setState(() {
        _phase = _Phase.error;
        _detail = e is ApiError || e.toString().contains('503')
            ? ref.read(i18nProvider).t('workbench:desktop.unavailable')
            : ref.read(i18nProvider).t('workbench:desktop.error');
      });
    }
  }

  void _run(String js) =>
      unawaited(_webView?.runJavaScript(js) ?? Future.value());

  void _toggleControl(bool on) {
    // The bridge drops the keyboard along with the pointer; mirror that here
    // so the button does not stay lit over a stream that is read-only again.
    setState(() {
      _control = on;
      if (!on) _keyboard = false;
    });
    _run(jsSetControl(on));
  }

  /// The desktop's own keyboard, drawn over the stream. It is the only way
  /// text reaches the guest, and it carries what a phone keyboard cannot:
  /// Esc, F1-F12, Ctrl/Alt, and 中/En for the guest's own input method.
  void _toggleKeyboard() {
    final next = !_keyboard;
    setState(() => _keyboard = next);
    _run(jsSetKeyboard(next));
  }

  Future<void> _setFullscreen(bool on) async {
    setState(() => _fullscreen = on);
    widget.onImmersive?.call(on);
    if (on) {
      await SystemChrome.setPreferredOrientations(
        [DeviceOrientation.landscapeLeft, DeviceOrientation.landscapeRight],
      );
      await SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    } else {
      await _restoreChrome();
    }
  }

  void _reconnect() {
    setState(() {
      _phase = _Phase.loading;
      _detail = '';
      _webView = null;
    });
    unawaited(_connect());
  }

  String _statusText(I18nState i18n) => switch (_phase) {
        _Phase.connected => _control
            ? i18n.t('workbench:desktop.controlOn')
            : i18n.t('workbench:desktop.readonly'),
        _Phase.loading => i18n.t('workbench:desktop.loading'),
        _Phase.error => i18n.t('workbench:desktop.error'),
        _Phase.closed => i18n.t('workbench:desktop.closed'),
      };

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (_fullscreen) return _fullscreenView(t, i18n);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(14, 8, 10, 6),
          child: Row(
            children: [
              _StatusDot(phase: _phase),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  _statusText(i18n),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                ),
              ),
              if (_phase == _Phase.connected) ...[
                // Only while the viewer holds the pointer: an on-stream
                // keyboard over a read-only desktop types nowhere.
                if (_control)
                  _IconAction(
                    icon: Icons.keyboard_outlined,
                    label: i18n.t('workbench:desktop.keyboard'),
                    active: _keyboard,
                    onTap: _toggleKeyboard,
                  ),
                _IconAction(
                  icon: Icons.fullscreen,
                  label: i18n.t('workbench:desktop.fullscreen'),
                  onTap: () => unawaited(_setFullscreen(true)),
                ),
                const SizedBox(width: 2),
                _ControlCheckbox(
                  on: _control,
                  label: i18n.t('workbench:desktop.allowControl'),
                  onTap: () => _toggleControl(!_control),
                ),
              ],
              if (_phase == _Phase.error || _phase == _Phase.closed)
                OutlinedButton(
                  onPressed: _reconnect,
                  style: OutlinedButton.styleFrom(
                    side: BorderSide(color: t.hair),
                    foregroundColor: t.n800,
                    padding:
                        const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(i18n.t('workbench:desktop.reconnect'),
                      style: const TextStyle(fontSize: FontSizes.sm)),
                ),
            ],
          ),
        ),
        Expanded(child: _stage(t, i18n)),
      ],
    );
  }

  Widget _stage(BossipTokens t, I18nState i18n, {bool bare = false}) {
    final stack = Stack(
      children: [
        if (_webView != null)
          Positioned.fill(child: WebViewWidget(controller: _webView!)),
        if (_phase != _Phase.connected)
          Positioned.fill(
            child: Container(
              color: bare ? Colors.black : t.card,
              alignment: Alignment.center,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (_phase == _Phase.loading) ...[
                    const Spinner(size: 20),
                    const SizedBox(height: 10),
                    Text(
                      i18n.t('workbench:desktop.loading'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                    ),
                  ] else ...[
                    Text(
                      _statusText(i18n),
                      style: TextStyle(fontSize: FontSizes.base, color: t.n800),
                    ),
                    if (_detail.isNotEmpty) ...[
                      const SizedBox(height: 6),
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 24),
                        child: Text(
                          _detail,
                          textAlign: TextAlign.center,
                          style:
                              TextStyle(fontSize: FontSizes.sm, color: t.n600),
                        ),
                      ),
                    ],
                  ],
                ],
              ),
            ),
          ),
      ],
    );
    if (bare) return stack;
    return Container(
      margin: const EdgeInsets.fromLTRB(10, 0, 10, 10),
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl2),
        border: Border.all(color: t.hair),
      ),
      child: stack,
    );
  }

  /// Landscape, edge to edge, no app chrome: a 4:3 stream on a phone only
  /// becomes legible when the screen's long side is the wide one.
  Widget _fullscreenView(BossipTokens t, I18nState i18n) {
    return ColoredBox(
      color: Colors.black,
      child: Stack(
        children: [
          Positioned.fill(child: _stage(t, i18n, bare: true)),
          Positioned(
            top: 4,
            right: 8,
            child: SafeArea(
              child: _FloatingControls(
                control: _control,
                keyboard: _keyboard,
                i18n: i18n,
                onExit: () => unawaited(_setFullscreen(false)),
                onControl: () => _toggleControl(!_control),
                onKeyboard: _toggleKeyboard,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.phase});

  final _Phase phase;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      width: 8,
      height: 8,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: switch (phase) {
          _Phase.connected => t.s600,
          _Phase.loading => t.a700,
          _ => t.n400,
        },
      ),
    );
  }
}

class _ControlCheckbox extends StatelessWidget {
  const _ControlCheckbox({
    required this.on,
    required this.label,
    required this.onTap,
  });

  final bool on;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return GestureDetector(
      onTap: onTap,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            on ? Icons.check_box : Icons.check_box_outline_blank,
            size: 17,
            color: on ? t.a700 : t.n500,
          ),
          const SizedBox(width: 5),
          Text(label, style: TextStyle(fontSize: FontSizes.sm, color: t.n700)),
        ],
      ),
    );
  }
}

class _IconAction extends StatelessWidget {
  const _IconAction({
    required this.icon,
    required this.label,
    required this.onTap,
    this.active = false,
    this.onDark = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool active;

  /// On the fullscreen overlay the background is the stream, not a token
  /// surface, so these two colours are deliberately not from the palette.
  final bool onDark;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final color = onDark
        ? (active ? Colors.white : Colors.white70)
        : (active ? t.a700 : t.n600);
    return Semantics(
      label: label,
      button: true,
      child: IconButton(
        onPressed: onTap,
        icon: Icon(icon, size: 19, color: color),
        visualDensity: VisualDensity.compact,
        constraints: const BoxConstraints.tightFor(width: 34, height: 34),
        padding: EdgeInsets.zero,
      ),
    );
  }
}

/// The fullscreen controls. Deliberately one small cluster: every pixel it
/// takes is stream the viewer came here to see.
class _FloatingControls extends StatelessWidget {
  const _FloatingControls({
    required this.control,
    required this.keyboard,
    required this.i18n,
    required this.onExit,
    required this.onControl,
    required this.onKeyboard,
  });

  final bool control;
  final bool keyboard;
  final I18nState i18n;
  final VoidCallback onExit;
  final VoidCallback onControl;
  final VoidCallback onKeyboard;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.55),
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          _IconAction(
            icon: control ? Icons.mouse : Icons.mouse_outlined,
            label: i18n.t('workbench:desktop.allowControl'),
            active: control,
            onDark: true,
            onTap: onControl,
          ),
          if (control)
            _IconAction(
              icon: Icons.keyboard_outlined,
              label: i18n.t('workbench:desktop.keyboard'),
              active: keyboard,
              onDark: true,
              onTap: onKeyboard,
            ),
          _IconAction(
            icon: Icons.fullscreen_exit,
            label: i18n.t('workbench:desktop.exitFullscreen'),
            onDark: true,
            onTap: onExit,
          ),
        ],
      ),
    );
  }
}
