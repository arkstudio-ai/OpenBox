import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/voice_call_controller.dart';
import '../state/voice_call_state.dart';
import 'voice_copy.dart';

/// "本次消耗 0.0035 积分" under the controls; a tap opens the breakdown.
class VoiceCostLine extends ConsumerWidget {
  const VoiceCostLine({super.key, required this.cost});

  final VoiceCost cost;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return TextButton(
      key: const Key('voice-cost'),
      onPressed: () => showVoiceCostSheet(context),
      style: TextButton.styleFrom(
        foregroundColor: t.n500,
        minimumSize: const Size(0, 36),
      ),
      child: Text(
        VoiceCopy.cost(i18n, cost),
        style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
      ),
    );
  }
}

Future<void> showVoiceCostSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      backgroundColor: context.tokens.card,
      builder: (_) => const _VoiceCostSheet(),
    );

/// The four billed parts, rounds settled, and whether the total may still
/// move. Live: it follows the call's `cost` events while open.
class _VoiceCostSheet extends ConsumerWidget {
  const _VoiceCostSheet();

  /// Wire part → (label key under `voice:cost.items`, icon).
  static const _items = {
    'input_text': ('inputText', Icons.notes),
    'input_audio': ('inputAudio', Icons.mic_none),
    'output_text': ('outputText', Icons.notes),
    'output_audio': ('outputAudio', Icons.graphic_eq),
  };

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final call = ref.watch(voiceCallControllerProvider);
    final cost = call.end?.cost ?? call.cost ?? const VoiceCost();
    // Calls are paid in credits (1 credit = 1 yuan): the meter's yuan show as credits.
    String yuan(double value) =>
        i18n.t('voice:cost.credits', vars: {'amount': formatYuan(value)});
    final notes = [
      i18n.t('voice:cost.settled', count: cost.settledRounds),
      if (cost.pending && !cost.isFinal) i18n.t('voice:cost.pending'),
      if (cost.partial) i18n.t('voice:cost.partial'),
    ];
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(24, 0, 24, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              i18n.t('voice:controls.cost'),
              style: TextStyle(fontSize: FontSizes.base, color: t.n600),
            ),
            const SizedBox(height: 4),
            Text(
              yuan(cost.totalYuan),
              style: TextStyle(
                fontSize: FontSizes.xl3,
                fontWeight: FontWeight.w600,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 16),
            for (final part in VoiceCost.parts)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Row(
                  children: [
                    Icon(_items[part]!.$2, size: 16, color: t.n600),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        i18n.t('voice:cost.items.${_items[part]!.$1}'),
                        style: TextStyle(fontSize: FontSizes.md, color: t.n800),
                      ),
                    ),
                    Text(
                      yuan(cost.costsYuan[part] ?? 0),
                      style: TextStyle(
                        fontSize: FontSizes.md,
                        color: t.ink,
                        fontFeatures: const [FontFeature.tabularFigures()],
                      ),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 12),
            Text(
              notes.join(' · '),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
          ],
        ),
      ),
    );
  }
}
