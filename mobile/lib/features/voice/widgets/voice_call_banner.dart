import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_reducer.dart';
import '../state/voice_call_state.dart';
import 'voice_copy.dart';
import 'voice_ticker.dart';

/// The 44 pt call bar under the status bar while the call page is closed
/// (mobile §3): "● 通话中 · 02:14 · 我在听" and a hang-up button. Grey with
/// "通话已结束" for a moment after a call ends away from the page.
///
/// It lives in `MaterialApp.builder`, above the Navigator: no Overlay, so
/// no tooltips; semantics labels carry the names instead.
class VoiceCallBanner extends ConsumerWidget {
  const VoiceCallBanner({super.key, required this.onOpen});

  static const height = 44.0;

  /// Pushes the call page.
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final call = ref.watch(voiceCallControllerProvider);
    final ended = call.status == VoiceCallStatus.ended;
    const ink = Colors.white;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: SystemUiOverlayStyle.light,
      child: Material(
        key: const Key('voice-banner'),
        color: ended ? t.n600 : t.s700,
        child: Padding(
          // The bar owns the status bar area too, like the system's own.
          padding: EdgeInsets.only(top: MediaQuery.paddingOf(context).top),
          child: SizedBox(
            height: height,
            child: Row(
              children: [
                Expanded(
                  child: Semantics(
                    button: true,
                    label: i18n.t('voice:banner.tapToReturn'),
                    child: InkWell(
                      key: const Key('voice-banner-open'),
                      onTap: onOpen,
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Row(
                          children: [
                            _Dot(pulsing: !ended),
                            const SizedBox(width: 10),
                            Expanded(
                              child: VoiceTicker(
                                builder: (context, now) => Text(
                                  _line(i18n, call, now),
                                  key: const Key('voice-banner-text'),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontSize: FontSizes.md,
                                    fontWeight: FontWeight.w500,
                                    color: ink,
                                    fontFeatures: [
                                      FontFeature.tabularFigures(),
                                    ],
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                if (!ended)
                  Padding(
                    padding: const EdgeInsets.only(right: 12),
                    child: Semantics(
                      button: true,
                      label: i18n.t('voice:controls.hangUp'),
                      excludeSemantics: true,
                      child: Material(
                        color: t.danger,
                        shape: const CircleBorder(),
                        clipBehavior: Clip.antiAlias,
                        child: InkWell(
                          key: const Key('voice-banner-hang-up'),
                          onTap: call.status == VoiceCallStatus.ending
                              ? null
                              : () => unawaited(
                                  ref
                                      .read(
                                        voiceCallControllerProvider.notifier,
                                      )
                                      .hangUp(),
                                ),
                          child: const SizedBox.square(
                            dimension: 32,
                            child: Icon(Icons.call_end, size: 18, color: ink),
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  static String _line(I18nState i18n, VoiceCallState call, DateTime now) {
    if (call.status == VoiceCallStatus.ended) {
      return i18n.t('voice:ended.title');
    }
    return [
      i18n.t('voice:banner.inCall'),
      if (call.connectedAt != null)
        formatCallDuration(callElapsedSeconds(call, now)),
      VoiceCopy.status(i18n, call),
    ].join(' · ');
  }
}

/// The live dot; a slow pulse while the call is on.
class _Dot extends StatefulWidget {
  const _Dot({required this.pulsing});

  final bool pulsing;

  @override
  State<_Dot> createState() => _DotState();
}

class _DotState extends State<_Dot> with SingleTickerProviderStateMixin {
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
    value: 1,
  );

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _sync();
  }

  @override
  void didUpdateWidget(_Dot oldWidget) {
    super.didUpdateWidget(oldWidget);
    _sync();
  }

  void _sync() {
    if (widget.pulsing && !MediaQuery.of(context).disableAnimations) {
      if (!_pulse.isAnimating) _pulse.repeat(reverse: true);
    } else {
      _pulse
        ..stop()
        ..value = 1;
    }
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(
    opacity: Tween<double>(begin: 0.35, end: 1).animate(_pulse),
    child: Container(
      width: 8,
      height: 8,
      decoration: const BoxDecoration(
        color: Colors.white,
        shape: BoxShape.circle,
      ),
    ),
  );
}
