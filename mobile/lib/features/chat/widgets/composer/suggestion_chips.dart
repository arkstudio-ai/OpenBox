import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';

/// One scrollable native row above the input card. Each target is at least
/// 44 logical pixels high, including when its label fits on one short line.
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
    return Semantics(
      container: true,
      label: i18n.t('chat:suggestions.title'),
      child: SingleChildScrollView(
        key: ValueKey(suggestions.id),
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 30, vertical: 2),
        child: Row(
          children: [
            for (final (index, item) in suggestions.items.take(3).indexed) ...[
              if (index > 0) const SizedBox(width: 8),
              Semantics(
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
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        foregroundColor: t.n700,
                        side: BorderSide(color: t.hair),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(Radii.full),
                        ),
                        textStyle: const TextStyle(fontSize: FontSizes.sm),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (item.mode == SuggestionMode.draft) ...[
                            const Icon(Icons.edit_outlined, size: 14),
                            const SizedBox(width: 5),
                          ],
                          Text(item.label, maxLines: 1),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
