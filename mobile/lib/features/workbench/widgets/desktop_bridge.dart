// The page hosting Alibaba's Wuying Web SDK inside the desktop tab's WebView,
// plus the small JS surface Flutter drives it through.
//
// The SDK is browser-only, so the phone runs the same bootstrap the web client
// does (`frontend-v2 DesktopTab.tsx`). What differs is that the SDK's own
// mobile chrome — toolbar, floating ball, menu — stays hidden, because on this
// side the UI is Flutter's. The entry points that chrome would have called are
// reached instead through two seams:
//
//   * `session.<method>()` — the public Web SDK surface (`enableKeyBoard`,
//     `openSoftKeyboard`, `setTouchEnabled`, `setMouseMode`, …).
//   * `session.customASPAction(action, args)` — invokes `action` on the ASP
//     streaming engine with `args` spread. Only what the engine hangs off that
//     object is reachable, so every call is wrapped: a build without a given
//     entry point must degrade to "that gesture does nothing", never to a dead
//     stream.
//
// What the SDK does *not* offer is a way to type a string: its whole message
// surface is input gates, the pointer, resolution, files and the clipboard.
// Text therefore goes in through the SDK's own on-stream keyboard
// ([jsSetKeyboard]), which sends real scan codes and carries the keys a phone
// keyboard lacks — Esc, F1-F12, Ctrl/Alt, and the 中/En key that switches the
// guest's own input method.
library;

import 'dart:convert';

const desktopSdkUrl =
    'https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.13.9-asp3.18.11/WuyingWebSDK/WuyingWebSDK.js';
const desktopSdkPath =
    'https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.13.9-asp3.18.11/WuyingWebSDK/sdk/ASP/container.html';

/// The desktop is pinned to XGA server-side (`backend/sandbox/desktop.py`
/// `obx-display`), so the agent, its screenshots and this viewer all share one
/// coordinate space.
const desktopRemoteWidth = 1024;
const desktopRemoteHeight = 768;

/// The WebView document. [ticket] is the one-time connection ticket from
/// `GET /api/desktop/ticket`.
String desktopBootstrapHtml(Map<String, dynamic> ticket) {
  final ticketJson = jsonEncode({
    'ticket': ticket['ticket'],
    'desktopId': ticket['desktopId'],
    'regionId': ticket['regionId'],
  });
  return _html
      .replaceFirst('__TICKET__', ticketJson)
      .replaceFirst('__SDK_URL__', desktopSdkUrl)
      .replaceFirst('__SDK_PATH__', desktopSdkPath)
      .replaceFirst('__REMOTE_W__', '$desktopRemoteWidth')
      .replaceFirst('__REMOTE_H__', '$desktopRemoteHeight');
}

/// Take or hand back keyboard and pointer control of the remote desktop.
String jsSetControl(bool on) => 'window.__setControl(${on ? 'true' : 'false'})';

/// Show or hide the desktop's own on-stream keyboard.
String jsSetKeyboard(bool on) =>
    'window.__setKeyboard(${on ? 'true' : 'false'})';

const _html = r'''
<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<style>html,body{margin:0;height:100%;background:transparent;overflow:hidden}
iframe{position:absolute;display:block;border:0}</style></head>
<body><iframe id="wuying-desktop-frame" tabindex="0" allowfullscreen
 allow="clipboard-read; clipboard-write; fullscreen"></iframe>
<script>
var TICKET = __TICKET__;
var W = __REMOTE_W__, H = __REMOTE_H__;
var session = null, control = false, keyboard = false;
function post(ev, detail) { try { Bossip.postMessage(JSON.stringify({event: ev, detail: detail || ''})); } catch (e) {} }
function frame() { return document.getElementById('wuying-desktop-frame'); }
// Call one method on the ASP streaming engine. Wrapped because a build
// without that entry point must cost us a gesture, not the session.
function asp(action, args) {
  try {
    if (!session || !session.customASPAction) return false;
    session.customASPAction(action, args || []);
    return true;
  } catch (e) { return false; }
}
// Size the iframe itself. Scaling it with a CSS transform would make the SDK
// observe a different input coordinate space than the one being touched.
function fit() {
  var f = frame(), s = Math.min(innerWidth / W, innerHeight / H);
  var w = Math.max(1, Math.floor(W * s)), h = Math.max(1, Math.floor(H * s));
  f.style.width = w + 'px'; f.style.height = h + 'px';
  f.style.left = Math.floor((innerWidth - w) / 2) + 'px';
  f.style.top = Math.floor((innerHeight - h) / 2) + 'px';
}
addEventListener('resize', fit); fit();
window.__setControl = function (on) {
  control = on;
  try {
    if (!session) return;
    // Prefer the current input API, keep the legacy method as fallback.
    if (session.setInputEnabled) session.setInputEnabled(on);
    else if (session.enableInput) session.enableInput(on);
    // Keyboard activation is a separate gate from general input: without it
    // the on-stream keyboard draws its keys but never delivers them.
    if (session.enableKeyBoard) session.enableKeyBoard(on);
    if (session.setTouchEnabled) session.setTouchEnabled(on);
    // Absolute coordinates: the finger is the pointer, so touching a spot
    // clicks that spot. Relative (Server) motion is for captured-pointer
    // workloads and needs a pointer lock a WebView cannot give — in it a tap
    // stops landing anywhere at all.
    if (on && session.setMouseMode) session.setMouseMode('Client');
    // Handing the pointer back must also take the keyboard off the stream,
    // or it sits there over a desktop nobody can type on.
    if (!on && keyboard) window.__setKeyboard(false);
    if (on && frame().focus) frame().focus();
  } catch (e) {}
};
window.__setKeyboard = function (on) {
  keyboard = !!on;
  try {
    if (!session) return;
    if (session.enableKeyBoard) session.enableKeyBoard(keyboard || control);
    if (session.openSoftKeyboard) session.openSoftKeyboard(keyboard);
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
      desktopInfo: {
        desktopId: TICKET.desktopId, loginRegionId: TICKET.regionId,
        connConfig: {
          // Let composition text reach the guest instead of being reduced to
          // physical key scan codes.
          useCustomIme: true, disableIME: false,
          // Never let a viewport change renegotiate the remote framebuffer:
          // the agent, screenshots and Wuying policy all use XGA.
          resolutionAdaptive: false,
          enableAutoSwitchMouseMode: true,
          // Media-resume hints without swallowing the tap that also targets
          // the remote desktop (1 + 2 + 8 + 16).
          mediaSuspendedTipFlag: 27
        }
      },
      // "B" multiplies by devicePixelRatio and changes across clients. The
      // fixed server-side policy is authoritative. The SDK's own mobile
      // chrome stays off: this app supplies those controls in Flutter.
      uiConfig: {
        toolbar: {visible: false, isShowKeyboard: false, isShowFullScreen: false},
        exitCheck: false, reconnectType: 'simple', defaultResolution: 'A',
        useMobileMenu: false, rotateDegree: 0,
        // A phone changes viewport constantly — rotation, fullscreen, the
        // keyboard rising. Without this the SDK renegotiates the remote
        // framebuffer to match, and the desktop the agent is looking at
        // changes shape under it. fixedResolution wins over every later
        // setResolution call, which is exactly the guarantee wanted here.
        fixedResolution: {width: W, height: H},
        maxResolution: {width: W, height: H}
      }
    });
    if (!session) { post('error', 'sdk'); return; }
    session.addHandle('onConnected', function () {
      post('connected');
      try { session.setClipboardEnabled && session.setClipboardEnabled(true); } catch (e) {}
      // Read-only unless the viewer already asked to take over.
      window.__setControl(control);
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
''';
