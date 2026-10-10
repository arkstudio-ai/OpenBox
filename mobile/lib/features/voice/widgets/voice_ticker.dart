import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../state/voice_call_controller.dart';

/// Rebuilds once a second with the call's clock: the timer, the remaining
/// minutes and the first-ten-seconds hint all read from it. Computed from
/// `connectedAt`, so a page coming back from the background is right at once.
class VoiceTicker extends ConsumerStatefulWidget {
  const VoiceTicker({super.key, required this.builder});

  final Widget Function(BuildContext context, DateTime now) builder;

  @override
  ConsumerState<VoiceTicker> createState() => _VoiceTickerState();
}

class _VoiceTickerState extends ConsumerState<VoiceTicker> {
  late final Timer _timer;

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _timer.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.builder(
    context,
    ref.read(voiceCallControllerProvider.notifier).now(),
  );
}
