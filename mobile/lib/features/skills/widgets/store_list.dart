import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import 'entry_row.dart';

/// 技能商店 — the catalogue, with what it depends on stated up front
/// (web `StoreList`).
class StoreList extends ConsumerWidget {
  const StoreList({
    super.key,
    required this.skills,
    required this.mcp,
    required this.showSkills,
    required this.showMcp,
    required this.onInstall,
  });

  final List<CatalogEntry> skills;
  final List<CatalogEntry> mcp;
  final bool showSkills;
  final bool showMcp;
  final void Function(CatalogEntry entry) onInstall;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    if (skills.isEmpty && mcp.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 48),
        child: Center(
          child: Text(
            i18n.t('skills:store.noMatch'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (showSkills && skills.isNotEmpty) ...[
          _heading(t, i18n.t('skills:section.storeSkills')),
          for (final entry in skills)
            _row(
              context,
              i18n,
              entry,
              badges: [
                if (entry.publisher != null && entry.publisher!.isNotEmpty)
                  SkillBadge(text: entry.publisher!),
                if (entry.community)
                  SkillBadge(
                    text: i18n.t('skills:badge.community'),
                    tone: BadgeTone.ok,
                  ),
                // Stated on the card, not just in the sheet: whether a skill
                // drags a server along changes whether someone wants it.
                if (entry.requiresMcp.isNotEmpty)
                  SkillBadge(
                    text: i18n.t('skills:badge.needsMcp',
                        vars: {'names': entry.requiresMcp.join(', ')}),
                    tone: BadgeTone.warn,
                  ),
              ],
            ),
          const SizedBox(height: 12),
        ],
        if (showMcp && mcp.isNotEmpty) ...[
          _heading(t, i18n.t('skills:section.storeMcp')),
          for (final entry in mcp)
            _row(
              context,
              i18n,
              entry,
              badges: [
                if (entry.publisher != null && entry.publisher!.isNotEmpty)
                  SkillBadge(text: entry.publisher!),
                SkillBadge(
                  text: i18n.t(
                      'skills:upload.transport.${entry.config?.type ?? 'stdio'}'),
                ),
                if (entry.requiredEnv.isNotEmpty)
                  SkillBadge(
                    text: i18n.t('skills:badge.needsKey'),
                    tone: BadgeTone.warn,
                  ),
              ],
            ),
        ],
      ],
    );
  }

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

  Widget _row(
    BuildContext context,
    I18nState i18n,
    CatalogEntry entry, {
    required List<Widget> badges,
  }) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: EntryRow(
        icon: entry.icon,
        name: entry.title,
        description: entry.description,
        badges: badges,
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
