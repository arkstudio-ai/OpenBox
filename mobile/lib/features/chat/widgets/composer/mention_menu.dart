import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';

/// One selectable row in the mention menu.
class MentionItem {
  const MentionItem({
    required this.kind,
    required this.label,
    required this.insert,
    this.description,
  });

  final String kind; // file | skill | command
  final String label;
  final String insert;
  final String? description;
}

class MentionSectionData {
  const MentionSectionData({
    required this.kind,
    required this.items,
    this.loading = false,
    this.needSandbox = false,
  });

  final String kind; // files | skills | commands (i18n suffix)
  final List<MentionItem> items;
  final bool loading;
  final bool needSandbox;
}

/// The `@` / `/` mention menu (web `MentionMenu.tsx`), rendered in-flow
/// above the composer input. Mobile: tap to select, no keyboard nav.
class MentionMenu extends ConsumerWidget {
  const MentionMenu({
    super.key,
    required this.sections,
    required this.onSelect,
    this.leading,
  });

  final List<MentionSectionData> sections;
  final void Function(MentionItem item) onSelect;

  /// Rendered above the sections — the resource-centre block, injected so the
  /// chat feature never imports the resources one.
  final Widget? leading;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      constraints: const BoxConstraints(maxHeight: 250),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: t.hair)),
      ),
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 6),
        children: [
          ?leading,
          for (final section in sections) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 6, 16, 2),
              child: Text(
                i18n.t('chat:composer.mention.${section.kind}'),
                style: TextStyle(
                  fontSize: FontSizes.xs2,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.4,
                  color: t.n500,
                ),
              ),
            ),
            if (section.needSandbox)
              _hint(t, i18n.t('chat:composer.mention.needSandbox'))
            else if (section.loading)
              _hint(t, i18n.t('chat:composer.mention.loading'))
            else if (section.items.isEmpty)
              _hint(t, i18n.t('chat:composer.mention.empty'))
            else
              for (final item in section.items)
                InkWell(
                  onTap: () => onSelect(item),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 7),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            item.label,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.ink,
                              fontFamily:
                                  item.kind == 'file' ? 'Menlo' : null,
                              fontFamilyFallback: item.kind == 'file'
                                  ? const ['monospace']
                                  : null,
                            ),
                          ),
                        ),
                        if (item.description != null &&
                            item.description!.isNotEmpty) ...[
                          const SizedBox(width: 10),
                          Flexible(
                            child: Text(
                              item.description!,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  fontSize: FontSizes.xs, color: t.n500),
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
          ],
        ],
      ),
    );
  }

  Widget _hint(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
        child: Text(
          text,
          style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
        ),
      );
}
