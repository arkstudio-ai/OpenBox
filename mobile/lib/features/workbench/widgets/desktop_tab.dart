import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/widgets/spinner.dart';

const _sdkUrl =
    'https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.12.5-asp3.18.7/WuyingWebSDK/WuyingWebSDK.js';
const _sdkPath =
    'https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.12.5-asp3.18.7/WuyingWebSDK/sdk/ASP/container.html';

enum _Phase { loading, connected, error, closed }

/// 云桌面 tab (web `DesktopTab.tsx`): the sandbox's Wuying cloud desktop.
/// The Web SDK is JS-only, so mobile hosts the same bootstrap in a WebView;
/// the one-time ticket (202-pending polled) is fetched natively. Read-only
/// by default — taking the mouse is an explicit toggle, and no machine ids
/// ever reach the UI.
class DesktopTab extends ConsumerStatefulWidget {
  const DesktopTab({super.key});

  @override
  ConsumerState<DesktopTab> createState() => _DesktopTabState();
}

class _DesktopTabState extends ConsumerState<DesktopTab> {
  _Phase _phase = _Phase.loading;
  String _detail = '';
  bool _control = false;
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
    super.dispose();
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
      await controller.loadHtmlString(_bootstrapHtml(ticket),
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

  String _bootstrapHtml(Map<String, dynamic> ticket) {
    final ticketJson = jsonEncode({
      'ticket': ticket['ticket'],
      'desktopId': ticket['desktopId'],
      'regionId': ticket['regionId'],
    });
    return r'''
<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<style>html,body{margin:0;height:100%;background:transparent;overflow:hidden}
#wrap{position:absolute;top:0;left:0;width:1920px;height:1080px;transform-origin:0 0}
iframe{width:100%;height:100%;border:0}</style></head>
<body><div id="wrap"><iframe id="wuying-desktop-frame" allow="clipboard-read; clipboard-write; fullscreen"></iframe></div>
<script>
var TICKET = __TICKET__;
var W = 1920, H = 1080, session = null;
function post(ev, detail) { try { Bossip.postMessage(JSON.stringify({event: ev, detail: detail || ''})); } catch (e) {} }
function fit() {
  var s = Math.min(innerWidth / W, innerHeight / H);
  var x = (innerWidth - W * s) / 2, y = (innerHeight - H * s) / 2;
  document.getElementById('wrap').style.transform =
    'translate(' + x + 'px,' + y + 'px) scale(' + s + ')';
}
addEventListener('resize', fit); fit();
window.__setControl = function (on) {
  try {
    if (!session) return;
    session.enableInput && session.enableInput(on);
    session.setInputEnabled && session.setInputEnabled(on);
    session.setTouchEnabled && session.setTouchEnabled(on);
  } catch (e) {}
};
var s = document.createElement('script');
s.src = '__SDK_URL__'; s.async = true;
s.onload = function () {
  try {
    var sdk = window.Wuying && window.Wuying.WebSDK;
    if (!sdk) { post('error', 'sdk'); return; }
    session = sdk.createSession('bossip-desktop-' + Date.now(), {
      openType: 'inline', iframeId: 'wuying-desktop-frame', sdkPath: '__SDK_PATH__',
      resourceType: 'local', connectType: 'desktop', regionId: TICKET.regionId,
      userInfo: {ticket: TICKET.ticket},
      desktopInfo: {desktopId: TICKET.desktopId, loginRegionId: TICKET.regionId},
      uiConfig: {toolbar: {visible: false}, exitCheck: false, reconnectType: 'simple', resolutionType: 'B'}
    });
    session.addHandle('onConnected', function () {
      post('connected');
      try { session.setClipboardEnabled && session.setClipboardEnabled(true); } catch (e) {}
    });
    session.addHandle('onDisConnected', function () { post('disconnected'); });
    session.addHandle('onError', function (err) {
      post('error', String((err && (err.message || err.code)) || ''));
    });
    session.start();
  } catch (e) { post('error', String(e)); }
};
s.onerror = function () { post('error', 'sdk'); };
document.head.appendChild(s);
</script></body></html>
'''
        .replaceFirst('__TICKET__', ticketJson)
        .replaceFirst('__SDK_URL__', _sdkUrl)
        .replaceFirst('__SDK_PATH__', _sdkPath);
  }

  void _toggleControl(bool on) {
    setState(() => _control = on);
    _webView?.runJavaScript('window.__setControl(${on ? 'true' : 'false'})');
  }

  void _reconnect() {
    setState(() {
      _phase = _Phase.loading;
      _detail = '';
      _webView = null;
    });
    unawaited(_connect());
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final statusText = switch (_phase) {
      _Phase.connected => _control
          ? i18n.t('workbench:desktop.controlOn')
          : i18n.t('workbench:desktop.readonly'),
      _Phase.loading => i18n.t('workbench:desktop.loading'),
      _Phase.error => i18n.t('workbench:desktop.error'),
      _Phase.closed => i18n.t('workbench:desktop.closed'),
    };
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(14, 8, 14, 10),
          child: Row(
            children: [
              Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: switch (_phase) {
                    _Phase.connected => t.s600,
                    _Phase.loading => t.a700,
                    _ => t.n400,
                  },
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  statusText,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                ),
              ),
              if (_phase == _Phase.connected)
                GestureDetector(
                  onTap: () => _toggleControl(!_control),
                  child: Row(
                    children: [
                      Icon(
                        _control
                            ? Icons.check_box
                            : Icons.check_box_outline_blank,
                        size: 17,
                        color: _control ? t.a700 : t.n500,
                      ),
                      const SizedBox(width: 5),
                      Text(
                        i18n.t('workbench:desktop.allowControl'),
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.n700),
                      ),
                    ],
                  ),
                ),
              if (_phase == _Phase.error || _phase == _Phase.closed)
                OutlinedButton(
                  onPressed: _reconnect,
                  style: OutlinedButton.styleFrom(
                    side: BorderSide(color: t.hair),
                    foregroundColor: t.n800,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 4),
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
        Expanded(
          child: Container(
            margin: const EdgeInsets.fromLTRB(10, 0, 10, 10),
            clipBehavior: Clip.antiAlias,
            decoration: BoxDecoration(
              color: t.card,
              borderRadius: BorderRadius.circular(Radii.xl2),
              border: Border.all(color: t.hair),
            ),
            child: Stack(
              children: [
                if (_webView != null)
                  WebViewWidget(controller: _webView!),
                if (_phase != _Phase.connected)
                  Container(
                    color: t.card,
                    alignment: Alignment.center,
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (_phase == _Phase.loading) ...[
                          const Spinner(size: 20),
                          const SizedBox(height: 10),
                          Text(
                            i18n.t('workbench:desktop.loading'),
                            style: TextStyle(
                                fontSize: FontSizes.sm, color: t.n600),
                          ),
                        ] else ...[
                          Text(
                            statusText,
                            style: TextStyle(
                                fontSize: FontSizes.base, color: t.n800),
                          ),
                          if (_detail.isNotEmpty) ...[
                            const SizedBox(height: 6),
                            Padding(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 24),
                              child: Text(
                                _detail,
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                    fontSize: FontSizes.sm, color: t.n600),
                              ),
                            ),
                          ],
                        ],
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
