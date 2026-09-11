import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import 'suggestion_loading.dart';

/// Equal-width suggestions share the input card's edges. Labels wrap freely
/// and the tallest suggestion sets the row height, keeping every choice visible.
class SuggestionChips extends ConsumerWidget {
  const SuggestionChips({
    super.key,
    required this.suggestions,
    required this.onSelect,
  });

  final SuggestionsPart suggestions;
  final ValueChanged<NextStepSuggestion> onSelect;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (suggestions.status == SuggestionStatus.pending) {
      return SuggestionLoading(
        key: ValueKey(suggestions.id),
        expiresAt: suggestions.expiresAt,
        label: i18n.t('chat:suggestions.loading'),
      );
    }
    if (suggestions.status == SuggestionStatus.unavailable ||
        suggestions.items.isEmpty) {
      return const SizedBox.shrink();
    }
    return Semantics(
      container: true,
      label: i18n.t('chat:suggestions.title'),
      child: Padding(
        key: ValueKey(suggestions.id),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
        child: LayoutBuilder(
          builder: (context, constraints) {
            final count = suggestions.items.take(3).length;
            final labelWidth =
                (constraints.maxWidth - (count - 1) * 6) / count - 20;
            return IntrinsicHeight(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final (index, item)
                      in suggestions.items.take(3).indexed) ...[
                    if (index > 0) const SizedBox(width: 6),
                    Expanded(
                      child: Semantics(
                        button: true,
                        onTap: () => onSelect(item),
                        label: i18n.t(
                          item.mode == SuggestionMode.draft
                              ? 'chat:suggestions.edit'
                              : 'chat:suggestions.send',
                          vars: {'label': item.label},
                        ),
                        child: Tooltip(
                          excludeFromSemantics: true,
                          message: i18n.t(
                            item.mode == SuggestionMode.draft
                                ? 'chat:suggestions.editHint'
                                : 'chat:suggestions.sendHint',
                            vars: {'prompt': item.prompt},
                          ),
                          child: ExcludeSemantics(
                            child: OutlinedButton(
                              onPressed: () => onSelect(item),
                              style: OutlinedButton.styleFrom(
                                minimumSize: const Size(44, 44),
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 10,
                                  vertical: 6,
                                ),
                                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                                backgroundColor: t.card,
                                foregroundColor: t.n700,
                                overlayColor: t.n500,
                                side: BorderSide(color: t.hair),
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(Radii.xl),
                                ),
                                textStyle: const TextStyle(
                                  fontSize: FontSizes.sm,
                                  height: 1.3,
                                ),
                              ),
                              child: SizedBox(
                                width: _balancedWidth(
                                  context,
                                  item.label,
                                  labelWidth,
                                ),
                                child: Text(
                                  item.label,
                                  textAlign: TextAlign.center,
                                  softWrap: true,
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ],
              ),
            );
          },
        ),
      ),
    );
  }

  // Use the narrowest line box that keeps the natural line count. This avoids
  // an orphan character on CJK labels while preserving whole English words.
  double _balancedWidth(BuildContext context, String label, double width) {
    if (width <= 0) return 0;
    final painter = TextPainter(
      text: TextSpan(
        text: label,
        style: Theme.of(
          context,
        ).textTheme.labelLarge?.copyWith(fontSize: FontSizes.sm, height: 1.3),
      ),
      textDirection: Directionality.of(context),
      textScaler: MediaQuery.textScalerOf(context),
    )..layout(maxWidth: width);
    final lines = painter.computeLineMetrics().length;
    if (lines <= 1) {
      painter.dispose();
      return width;
    }
    var low = 0.0;
    var high = width;
    for (var i = 0; i < 10; i++) {
      final middle = (low + high) / 2;
      painter.layout(maxWidth: middle);
      if (painter.computeLineMetrics().length > lines) {
        low = middle;
      } else {
        high = middle;
      }
    }
    painter.dispose();
    return (high + 0.5).clamp(0, width);
  }
}
