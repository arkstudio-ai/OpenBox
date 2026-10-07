import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import 'state/voice_call_controller.dart';
import 'state/voice_call_reducer.dart';
import 'state/voice_call_state.dart';
import 'widgets/voice_call_controls.dart';
import 'widgets/voice_copy.dart';
import 'widgets/voice_cost_sheet.dart';
import 'widgets/voice_ended_panel.dart';
import 'widgets/voice_orb.dart';
import 'widgets/voice_ticker.dart';

/// The full-screen call (mobile §3). Opening it on no call dials; leaving it
/// any way — the chevron, back, a downward swipe — only collapses the call
/// into the top call bar. Hanging up ends the call and leaves too.
///
/// "只听不看字": nothing said in the call is shown here, only a one-line
/// hint for the first ten seconds.
class VoiceCallPage extends ConsumerStatefulWidget {
  const VoiceCallPage({super.key});

  /// How long `voice:hint.start` stays after connecting.
  static const hintDuration = Duration(seconds: 10);

  @override
  ConsumerState<VoiceCallPage> createState() => _VoiceCallPageState();
}

class _VoiceCallPageState extends ConsumerState<VoiceCallPage> {
  /// Pages alive right now. Reopening from the call bar while the last page
  /// is still sliding away briefly makes two; only the last one to go may
  /// hand the call back to the bar.
  static int _open = 0;

  late final VoiceCallController _controller;

  @override
  void initState() {
    super.initState();
    _open++;
    _controller = ref.read(voiceCallControllerProvider.notifier);
    unawaited(_controller.keepScreenOn(true));
    // Providers may not change while widgets are being built.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _controller.setExpanded(true);
      if (ref.read(voiceCallControllerProvider).status ==
          VoiceCallStatus.idle) {
        unawaited(_controller.start());
      }
    });
  }

  @override
  void dispose() {
    _open--;
    final controller = _controller;
    unawaited(controller.keepScreenOn(false));
    // ...nor while they are being torn down: the call bar takes over on the
    // next frame (a system back gesture ends up here without [_leave]).
    WidgetsBinding.instance
      ..addPostFrameCallback((_) {
        if (_open > 0) return;
        try {
          controller.pageGone();
        } catch (_) {
          // The whole app went away with the page (a test tearing down).
        }
      })
      ..ensureVisualUpdate();
    super.dispose();
  }

  /// Collapse, hang up, close. A live call goes to the call bar at once,
  /// not after the page has slid away: an `ended` arriving mid-slide must
  /// still bring its summary. A finished call is cleared when the page is
  /// gone, so the closing page never flips to an empty state.
  void _leave() {
    // Already leaving (a tap, then a swipe): never pop what lies beneath.
    if (!(ModalRoute.of(context)?.isCurrent ?? true)) return;
    if (ref.read(voiceCallControllerProvider).status != VoiceCallStatus.ended) {
      _controller.setExpanded(false);
    }
    final navigator = Navigator.of(context);
    if (navigator.canPop()) {
      navigator.pop();
    } else {
      context.go(Paths.assistant);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final call = ref.watch(voiceCallControllerProvider);
    final end = call.status == VoiceCallStatus.ended ? call.end : null;
    final detail = VoiceCopy.detail(i18n, call);
    final cost = end?.cost ?? call.cost;
    final dark = Theme.of(context).brightness == Brightness.dark;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: dark ? SystemUiOverlayStyle.light : SystemUiOverlayStyle.dark,
      child: Scaffold(
        backgroundColor: t.bg,
        body: GestureDetector(
          // A firm downward swipe collapses, like the system's own sheets.
          onVerticalDragEnd: (details) {
            if ((details.primaryVelocity ?? 0) > 700) _leave();
          },
          child: SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 20, 16),
              child: Column(
                children: [
                  _TopBar(call: call, onCollapse: _leave),
                  const SizedBox(height: 20),
                  Text(
                    i18n.t('voice:title'),
                    style: TextStyle(
                      fontSize: FontSizes.xl2,
                      fontWeight: FontWeight.w600,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      VoiceCopy.status(i18n, call),
                      key: const Key('voice-status'),
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        height: 1.5,
                        color: t.n600,
                      ),
                    ),
                  ),
                  SizedBox(
                    height: 22,
                    child: detail == null
                        ? null
                        : Text(
                            detail,
                            key: const Key('voice-detail'),
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.n500,
                            ),
                          ),
                  ),
                  Expanded(
                    child: Center(
                      child: VoiceOrb(
                        call: call,
                        micLevel: _controller.micLevel,
                        outputLevel: _controller.outputLevel,
                      ),
                    ),
                  ),
                  if (end != null)
                    VoiceEndedPanel(end: end, onClose: _leave)
                  else ...[
                    _Hint(call: call),
                    const SizedBox(height: 20),
                    VoiceCallControls(call: call, onHangUp: _leave),
                  ],
                  SizedBox(
                    height: 40,
                    child: cost == null || end != null
                        ? null
                        : Center(child: VoiceCostLine(cost: cost)),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _TopBar extends ConsumerWidget {
  const _TopBar({required this.call, required this.onCollapse});

  final VoiceCallState call;
  final VoidCallback onCollapse;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Row(
      children: [
        IconButton(
          key: const Key('voice-collapse'),
          onPressed: onCollapse,
          tooltip: i18n.t('voice:controls.collapse'),
          icon: Icon(Icons.keyboard_arrow_down, size: 28, color: t.n700),
        ),
        const Spacer(),
        if (call.connectedAt != null && call.status != VoiceCallStatus.ended)
          VoiceTicker(
            builder: (context, now) {
              final elapsed = callElapsedSeconds(call, now);
              final remaining = VoiceCopy.remaining(i18n, call, elapsed);
              return Text(
                [formatCallDuration(elapsed), ?remaining].join(' · '),
                key: const Key('voice-timer'),
                style: TextStyle(
                  fontSize: FontSizes.md,
                  color: t.n600,
                  fontFeatures: const [FontFeature.tabularFigures()],
                ),
              );
            },
          ),
      ],
    );
  }
}

/// "直接说话就好，随时可以打断。" for the first ten seconds, then blank.
class _Hint extends ConsumerWidget {
  const _Hint({required this.call});

  final VoiceCallState call;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final since = call.connectedAt;
    return SizedBox(
      height: 22,
      child: since == null || !call.live
          ? null
          : VoiceTicker(
              builder: (context, now) =>
                  now.difference(since) < VoiceCallPage.hintDuration
                  ? Text(
                      i18n.t('voice:hint.start'),
                      key: const Key('voice-hint'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
                    )
                  : const SizedBox.shrink(),
            ),
    );
  }
}
