import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';
import '../platform/system_voice_call.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_state.dart';
import 'voice_call_banner.dart';
import 'voice_copy.dart';

/// Wraps every screen (`MaterialApp.builder`): while a call is on and its
/// page is closed, the call bar takes the top 44 pt and pushes the app down
/// — it never covers a control (mobile §1, §2).
///
/// The tree keeps one shape whether or not the bar shows, so the app below
/// is never rebuilt from scratch when a call starts or ends.
class VoiceCallHost extends ConsumerStatefulWidget {
  const VoiceCallHost({super.key, required this.child, required this.onOpen});

  final Widget child;

  /// Pushes the call page (the host sits above the Navigator).
  final VoidCallback onOpen;

  /// How long the grey "通话已结束" bar stays before it goes.
  static const endedBarTime = Duration(seconds: 3);

  @override
  ConsumerState<VoiceCallHost> createState() => _VoiceCallHostState();
}

class _VoiceCallHostState extends ConsumerState<VoiceCallHost> {
  Timer? _dismiss;
  StreamSubscription<SystemVoiceAction>? _systemActions;

  @override
  void initState() {
    super.initState();
    _systemActions = ref.read(systemVoiceCallProvider).actions.listen((action) {
      if (!mounted || action != SystemVoiceAction.open) return;
      final call = ref.read(voiceCallControllerProvider);
      if (call.active && !call.expanded) widget.onOpen();
    });
  }

  @override
  void dispose() {
    _dismiss?.cancel();
    unawaited(_systemActions?.cancel());
    super.dispose();
  }

  void _onCall(VoiceCallState? previous, VoiceCallState next) {
    if (next.status == VoiceCallStatus.ended &&
        previous?.status != VoiceCallStatus.ended &&
        !next.expanded) {
      _toastEnd(next.end);
      _dismiss?.cancel();
      _dismiss = Timer(VoiceCallHost.endedBarTime, () {
        if (!mounted) return;
        final call = ref.read(voiceCallControllerProvider);
        if (call.status == VoiceCallStatus.ended && !call.expanded) {
          ref.read(voiceCallControllerProvider.notifier).dismissEnded();
        }
      });
    }
    if (previous?.status == VoiceCallStatus.paused &&
        next.status == VoiceCallStatus.connected) {
      ref
          .read(toastProvider.notifier)
          .info(ref.read(i18nProvider).t('voice:interruption.resumed'));
    }
  }

  /// The call ended while its page was closed: say how, how long, what it
  /// cost, and what is still being worked on.
  void _toastEnd(VoiceCallEnd? end) {
    if (end == null) return;
    final i18n = ref.read(i18nProvider);
    final headline = VoiceCopy.endHeadline(i18n, end);
    final body = [
      VoiceCopy.endDetail(i18n, end),
      VoiceCopy.endFigures(i18n, end),
      VoiceCopy.pendingHint(i18n, end),
    ].whereType<String>().join('\n');
    final calm =
        end.reason == VoiceEndReason.hangup ||
        end.reason == VoiceEndReason.limit;
    ref
        .read(toastProvider.notifier)
        .push(
          calm ? ToastKind.info : ToastKind.warning,
          body.isEmpty ? headline : body,
          title: body.isEmpty ? null : headline,
        );
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<VoiceCallState>(voiceCallControllerProvider, _onCall);
    final show = ref.watch(
      voiceCallControllerProvider.select(
        (call) => call.status != VoiceCallStatus.idle && !call.expanded,
      ),
    );
    return Column(
      children: [
        AnimatedSize(
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
          alignment: Alignment.topCenter,
          child: show
              ? VoiceCallBanner(onOpen: widget.onOpen)
              : const SizedBox(width: double.infinity),
        ),
        Expanded(
          // The bar already sits under the status bar; the screens below
          // must not pad for it a second time.
          child: MediaQuery.removePadding(
            context: context,
            removeTop: show,
            child: widget.child,
          ),
        ),
      ],
    );
  }
}
