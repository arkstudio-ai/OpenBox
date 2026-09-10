import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/auth_store.dart';
import '../../../shared/api/desktop_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/desktop.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/workspace.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../../workspace/state/active_workspace_store.dart';
import 'desktop_bridge.dart';
import 'desktop_subscription_notice.dart';

enum _Phase {
  loading,
  connected,
  error,
  closed,
  pending,
  attention,
  subscriptionRequired,
}

/// A scope change unmounts the old SDK, its outstanding tickets and expiry timer.
class DesktopTab extends ConsumerWidget {
  const DesktopTab({super.key, this.onImmersive, this.autoControl = false});
  final ValueChanged<bool>? onImmersive;

  /// Take input control on the first connect — a takeover card in the chat
  /// sent the user here to solve something by hand (web `desktopControl`).
  final bool autoControl;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final user = ref.watch(authProvider).user;
    final workspace = ref.watch(activeWorkspaceProvider).valueOrNull?.current;
    if (user == null || workspace == null) {
      return const Center(child: Spinner());
    }
    final scope = (userId: user.id, workspaceId: workspace.id);
    return ScopedDesktopViewer(
      key: ValueKey(scope),
      scope: scope,
      canManage: workspace.role.canManage,
      onImmersive: onImmersive,
      autoControl: autoControl,
    );
  }
}

/// Native ticket transport + Wuying Web SDK. Opening the viewer is read-only:
/// provisioning belongs to the payment-triggered, durable backend activation.
class ScopedDesktopViewer extends ConsumerStatefulWidget {
  const ScopedDesktopViewer({
    super.key,
    required this.scope,
    required this.canManage,
    this.onImmersive,
    this.autoControl = false,
  });
  final DesktopScope scope;
  final bool canManage;
  final ValueChanged<bool>? onImmersive;
  final bool autoControl;

  @override
  ConsumerState<ScopedDesktopViewer> createState() => _DesktopViewerState();
}

class _DesktopViewerState extends ConsumerState<ScopedDesktopViewer>
    with WidgetsBindingObserver {
  _Phase _phase = _Phase.loading;
  String _detail = '';
  bool _control = false;
  // Honoured once: a reconnect after the user switched control back off
  // must not re-enable it.
  bool _autoControlApplied = false;
  bool _fullscreen = false;
  bool _keyboard = false;
  bool _alive = true;
  bool _connecting = false;
  bool _retrying = false;
  int _generation = 0;
  DesktopStatus? _status;
  Timer? _expiryTimer;
  CancelToken? _ticketCancel;
  WebViewController? _webView;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    ref.listenManual(desktopStatusProvider(widget.scope), (_, next) {
      if (!_alive) return;
      next.whenData(_onStatus);
      if (next.hasError && _webView == null && !_connecting) {
        setState(() {
          _phase = _Phase.error;
          _detail = errorText(ref.read(i18nProvider), next.error!);
        });
      }
    }, fireImmediately: true);
  }

  @override
  void dispose() {
    _alive = false;
    WidgetsBinding.instance.removeObserver(this);
    _expiryTimer?.cancel();
    _dropViewer();
    if (_fullscreen) unawaited(_restoreChrome());
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      if (_status != null && !_status!.hasAccess) _suspend();
      ref.invalidate(desktopStatusProvider(widget.scope));
    }
  }

  Future<void> _restoreChrome() async {
    await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
  }

  bool _isCurrent(int generation) =>
      _alive && generation == _generation && (_status?.hasAccess ?? false);

  void _dropViewer() {
    _generation += 1;
    _connecting = false;
    _ticketCancel?.cancel();
    _ticketCancel = null;
    final controller = _webView;
    _webView = null;
    _control = false;
    _keyboard = false;
    if (controller != null) unawaited(_stopViewer(controller));
  }

  Future<void> _stopViewer(WebViewController controller) async {
    try {
      await controller.runJavaScript(jsStopDesktop);
      await controller.loadRequest(Uri.parse('about:blank'));
    } catch (_) {
      // Removing the platform view is the fallback if its JS context is gone.
    }
  }

  void _suspend() {
    _dropViewer();
    if (!_alive) return;
    if (_fullscreen) {
      _fullscreen = false;
      widget.onImmersive?.call(false);
      unawaited(_restoreChrome());
    }
    setState(() {
      _phase = _Phase.subscriptionRequired;
      _detail = '';
    });
  }

  void _onStatus(DesktopStatus status) {
    _status = status;
    _expiryTimer?.cancel();
    if (!status.hasAccess) {
      _suspend();
      return;
    }
    final expires = status.subscriptionEndsAt;
    if (expires != null) {
      _expiryTimer = Timer(expires.difference(DateTime.now()), () {
        if (!_alive) return;
        _suspend();
        ref.invalidate(desktopStatusProvider(widget.scope));
      });
    }
    if (!status.ready) {
      _dropViewer();
      setState(() {
        _phase = status.needsAttention || status.state == 'failed'
            ? _Phase.attention
            : _Phase.pending;
        _detail = '';
      });
      return;
    }
    if (_webView == null &&
        !_connecting &&
        [
          _Phase.loading,
          _Phase.pending,
          _Phase.attention,
          _Phase.subscriptionRequired,
        ].contains(_phase)) {
      unawaited(_connect());
    } else {
      setState(() {});
    }
  }

  Future<Map<String, dynamic>> _fetchTicket(
    int generation,
    CancelToken cancel,
  ) async {
    String? taskId;
    for (var attempt = 0; attempt < 30 && _isCurrent(generation); attempt++) {
      final data = await ref
          .read(desktopApiProvider)
          .ticket(widget.scope, taskId: taskId, cancel: cancel);
      if (asString(data['ticket']) != null) return data;
      taskId = asString(data['taskId']) ?? taskId;
      await Future<void>.delayed(const Duration(seconds: 3));
    }
    throw TimeoutException('desktop ticket');
  }

  Future<void> _connect() async {
    if (_connecting || !(_status?.ready ?? false)) return;
    _connecting = true;
    final generation = ++_generation;
    final cancel = _ticketCancel = CancelToken();
    setState(() {
      _phase = _Phase.loading;
      _detail = '';
    });
    try {
      final ticket = await _fetchTicket(generation, cancel);
      if (!_isCurrent(generation)) return;
      final controller = WebViewController();
      await controller.setJavaScriptMode(JavaScriptMode.unrestricted);
      await controller.setBackgroundColor(Colors.transparent);
      await controller.addJavaScriptChannel(
        'Bossip',
        onMessageReceived: (msg) {
          if (!_isCurrent(generation)) return;
          final dynamic decoded;
          try {
            decoded = jsonDecode(msg.message);
          } on FormatException {
            return;
          }
          if (decoded is! Map<String, dynamic>) return;
          final data = decoded;
          setState(() {
            switch (asString(data['event'])) {
              case 'connected':
                _phase = _Phase.connected;
                _detail = '';
              case 'disconnected':
                _phase = _Phase.closed;
              case 'error':
                _phase = _Phase.error;
                _detail = ref
                    .read(i18nProvider)
                    .t(
                      data['detail'] == 'sdk'
                          ? 'workbench:desktop.sdkFailed'
                          : 'workbench:desktop.error',
                    );
            }
          });
          if (_phase == _Phase.connected &&
              widget.autoControl &&
              !_autoControlApplied) {
            _autoControlApplied = true;
            _toggleControl(true);
          }
        },
      );
      if (!_isCurrent(generation)) {
        await _stopViewer(controller);
        return;
      }
      // Retain the controller before loading so expiry/dispose can stop it
      // even while the platform is still bootstrapping the SDK.
      setState(() => _webView = controller);
      await controller.loadHtmlString(
        desktopBootstrapHtml(ticket),
        baseUrl: 'https://bossip.desktop',
      );
      if (!_isCurrent(generation)) await _stopViewer(controller);
    } catch (error) {
      if (!_isCurrent(generation)) return;
      final code = apiErrorOf(error)?.code;
      if (code == 'SANDBOX_SUBSCRIPTION_REQUIRED') {
        _suspend();
        ref.invalidate(desktopStatusProvider(widget.scope));
      } else {
        setState(() {
          _phase = _Phase.error;
          _detail = error is TimeoutException
              ? ref.read(i18nProvider).t('workbench:desktop.error')
              : errorText(ref.read(i18nProvider), error);
        });
      }
    } finally {
      if (_isCurrent(generation)) _connecting = false;
    }
  }

  Future<void> _retryActivation() async {
    if (_retrying ||
        !widget.canManage ||
        !(_status?.hasAccess ?? false) ||
        _status?.activation?.canRetry != true) {
      return;
    }
    setState(() => _retrying = true);
    try {
      await ref.read(desktopApiProvider).retry(widget.scope);
    } catch (_) {
      if (mounted) {
        setState(
          () => _detail = ref
              .read(i18nProvider)
              .t('workbench:activation.retryFailed'),
        );
      }
    } finally {
      if (mounted) {
        setState(() => _retrying = false);
        ref.invalidate(desktopStatusProvider(widget.scope));
      }
    }
  }

  void _reconnect() {
    _dropViewer();
    setState(() {
      _phase = _Phase.loading;
      _detail = '';
    });
    // A fresh server permission check always precedes a new ticket.
    ref.invalidate(desktopStatusProvider(widget.scope));
  }

  void _run(String js) {
    if (_phase != _Phase.connected || !(_status?.hasAccess ?? false)) return;
    unawaited(
      _webView?.runJavaScript(js).catchError((Object _) {}) ??
          Future<void>.value(),
    );
  }

  void _toggleControl(bool on) {
    setState(() {
      _control = on;
      if (!on) _keyboard = false;
    });
    _run(jsSetControl(on));
  }

  void _toggleKeyboard() {
    setState(() => _keyboard = !_keyboard);
    _run(jsSetKeyboard(_keyboard));
  }

  Future<void> _setFullscreen(bool on) async {
    setState(() => _fullscreen = on);
    widget.onImmersive?.call(on);
    if (on) {
      await SystemChrome.setPreferredOrientations([
        DeviceOrientation.landscapeLeft,
        DeviceOrientation.landscapeRight,
      ]);
      await SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    } else {
      await _restoreChrome();
    }
  }

  String _statusText(I18nState i18n) => i18n.t(switch (_phase) {
    _Phase.connected =>
      _control ? 'workbench:desktop.controlOn' : 'workbench:desktop.readonly',
    _Phase.loading => 'workbench:desktop.loading',
    _Phase.error => 'workbench:desktop.error',
    _Phase.closed => 'workbench:desktop.closed',
    _Phase.pending => 'workbench:activation.title',
    _Phase.attention => 'workbench:activation.attention',
    _Phase.subscriptionRequired => 'workbench:activation.subscriptionRequired',
  });

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (_phase == _Phase.subscriptionRequired) {
      return const DesktopSubscriptionNotice();
    }
    if (_fullscreen) return _fullscreenView(t, i18n);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(14, 8, 10, 6),
          child: Column(
            children: [
              Row(
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
                  if (_status?.channel?.state.isNotEmpty ?? false)
                    _ChannelPill(state: _status!.channel!.state),
                  if (_phase == _Phase.error || _phase == _Phase.closed)
                    TextButton(
                      onPressed: _reconnect,
                      child: Text(i18n.t('workbench:desktop.reconnect')),
                    ),
                ],
              ),
              if (_phase == _Phase.connected)
                SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  reverse: true,
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
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
                      _ControlCheckbox(
                        on: _control,
                        label: i18n.t('workbench:desktop.allowControl'),
                        onTap: () => _toggleControl(!_control),
                      ),
                    ],
                  ),
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
              child: SingleChildScrollView(
                padding: const EdgeInsets.all(28),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (_phase == _Phase.loading ||
                        _phase == _Phase.pending) ...[
                      const Spinner(size: 20),
                      const SizedBox(height: 14),
                    ],
                    Text(
                      _statusText(i18n),
                      textAlign: TextAlign.center,
                      style: TextStyle(color: t.n800, fontSize: FontSizes.base),
                    ),
                    if (_phase == _Phase.pending ||
                        _phase == _Phase.attention) ...[
                      const SizedBox(height: 12),
                      Text(
                        i18n.t(
                          _status?.needsAttention == true
                              ? 'workbench:activation.attentionHint'
                              : 'workbench:activation.hint',
                        ),
                        textAlign: TextAlign.center,
                        style: TextStyle(color: t.n600, height: 1.5),
                      ),
                      if (_status?.activation?.canRetry == true &&
                          widget.canManage) ...[
                        const SizedBox(height: 16),
                        OutlinedButton(
                          onPressed: _retrying ? null : _retryActivation,
                          child: Text(i18n.t('workbench:activation.retry')),
                        ),
                      ],
                    ],
                    if (_detail.isNotEmpty) ...[
                      const SizedBox(height: 12),
                      Text(
                        _detail,
                        textAlign: TextAlign.center,
                        style: TextStyle(color: t.n600),
                      ),
                    ],
                  ],
                ),
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

  Widget _fullscreenView(BossipTokens t, I18nState i18n) => ColoredBox(
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

class _ChannelPill extends ConsumerWidget {
  const _ChannelPill({required this.state});

  final String state;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final key = 'workbench:desktop.channel.$state';
    final translated = i18n.t(key);
    return Container(
      margin: const EdgeInsets.only(right: 4),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: t.n100,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        translated == key ? state : translated,
        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
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
          Text(
            label,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
          ),
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
