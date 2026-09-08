import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import '../utils/store_sections.dart';
import 'entry_row.dart';

/// 技能商店 — the catalogue laid out by shelf (web `StoreList`).
///
/// Sections are cut by who published a thing, not by what it technically is,
/// so a row has to say its own kind rather than inherit it from the heading.
class StoreList extends ConsumerWidget {
  const StoreList({
    super.key,
    required this.shelves,
    required this.onInstall,
  });

  final StoreShelves shelves;
  final void Function(CatalogEntry entry) onInstall;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    if (shelves.sections.isEmpty) {
      // An empty store and a search that matched nothing are different
      // problems, and only one of them is the person's own doing.
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 48),
        child: Center(
          child: Text(
            i18n.t(shelves.total == 0
                ? 'skills:store.empty'
                : 'skills:store.noMatch'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final section in shelves.sections) ...[
          _heading(t, i18n.t(_headingKey(section.origin))),
          for (final entry in section.entries)
            _row(context, i18n, entry),
          const SizedBox(height: 12),
        ],
      ],
    );
  }

  static String _headingKey(String origin) => switch (origin) {
        'official' => 'skills:section.storeOfficial',
        'third_party' => 'skills:section.storeThirdParty',
        _ => 'skills:section.storeCommunity',
      };

  Widget _heading(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 7),
        child: Text(
          text,
          style: TextStyle(
            fontSize: FontSizes.xs,
            fontWeight: FontWeight.w500,
            color: t.n600,
          ),
        ),
      );

  List<Widget> _badges(I18nState i18n, CatalogEntry entry) {
    final isMcp = entry.kind == 'mcp';
    return [
      // Pinned by an operator, so the row says why it is at the top instead
      // of looking like an accident of sorting.
      if (entry.featured)
        SkillBadge(text: i18n.t('skills:badge.featured'), tone: BadgeTone.warn),
      SkillBadge(
        text: i18n.t(isMcp ? 'skills:badge.kindMcp' : 'skills:badge.kindSkill'),
      ),
      if (entry.publisher != null && entry.publisher!.isNotEmpty)
        SkillBadge(text: entry.publisher!),
      if (isMcp) ...[
        SkillBadge(
          text: i18n
              .t('skills:upload.transport.${entry.config?.type ?? 'stdio'}'),
        ),
        if (entry.requiredEnv.isNotEmpty)
          SkillBadge(
            text: i18n.t('skills:badge.needsKey'),
            tone: BadgeTone.warn,
          ),
      ] else if (entry.requiresMcp.isNotEmpty)
        // Stated on the card, not just in the sheet: whether a skill drags a
        // server along changes whether someone wants it at all.
        SkillBadge(
          text: i18n.t('skills:badge.needsMcp',
              vars: {'names': entry.requiresMcp.join(', ')}),
          tone: BadgeTone.warn,
        ),
      // Zero is not evidence of anything — a fresh entry and an ignored one
      // look identical — so the count appears once it means something.
      if (entry.installsCount > 0)
        SkillBadge(
          text: i18n.t('skills:badge.installs', count: entry.installsCount),
        ),
    ];
  }

  Widget _row(BuildContext context, I18nState i18n, CatalogEntry entry) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: EntryRow(
        icon: entry.icon,
        name: entry.title,
        description: entry.description,
        badges: _badges(i18n, entry),
        actions: [
          if (entry.homepage != null && entry.homepage!.isNotEmpty)
            IconAction(
              icon: Icons.open_in_new,
              tooltip: i18n.t('skills:action.homepage'),
              onTap: () => launchUrl(
                Uri.parse(entry.homepage!),
                mode: LaunchMode.externalApplication,
              ),
            ),
          Padding(
            padding: const EdgeInsets.only(left: 2, right: 4),
            child: FilledButton(
              onPressed: entry.installed ? null : () => onInstall(entry),
              style: FilledButton.styleFrom(
                backgroundColor: t.ink,
                foregroundColor: t.bg,
                disabledBackgroundColor: t.n200,
                disabledForegroundColor: t.n700,
                visualDensity: VisualDensity.compact,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              child: Text(
                i18n.t(entry.installed
                    ? 'skills:action.installed'
                    : 'skills:action.install'),
                style: const TextStyle(fontSize: FontSizes.xs),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
