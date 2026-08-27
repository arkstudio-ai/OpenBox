import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import '../utils/group_skills.dart';
import 'entry_row.dart';
import 'skill_group_section.dart';

/// What this account has installed: skills, then the MCP servers behind them
/// (web `MineList`).
class MineList extends ConsumerWidget {
  const MineList({
    super.key,
    required this.skills,
    required this.servers,
    required this.unmetFor,
    required this.showSkills,
    required this.showMcp,
    required this.actions,
    required this.onBrowseStore,
    required this.onConnect,
    required this.onDisconnect,
    required this.onRemoveServer,
  });

  final List<InstalledSkill> skills;
  final List<McpServer> servers;
  final List<SkillDependency> Function(InstalledSkill skill) unmetFor;
  final bool showSkills;
  final bool showMcp;
  final SkillGroupActions actions;
  final VoidCallback onBrowseStore;
  final void Function(String name) onConnect;
  final void Function(String name) onDisconnect;
  final void Function(String name) onRemoveServer;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    if (skills.isEmpty && servers.isEmpty) {
      return Container(
        margin: const EdgeInsets.symmetric(vertical: 24),
        padding: const EdgeInsets.symmetric(vertical: 40, horizontal: 20),
        decoration: BoxDecoration(
          border: Border.all(color: t.hair, style: BorderStyle.solid),
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        child: Column(
          children: [
            Text(
              i18n.t('skills:mine.emptyTitle'),
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
            ),
            const SizedBox(height: 4),
            Text(
              i18n.t('skills:mine.emptyHint'),
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            const SizedBox(height: 12),
            FilledButton(
              onPressed: onBrowseStore,
              style: FilledButton.styleFrom(
                backgroundColor: t.ink,
                foregroundColor: t.bg,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              child: Text(
                i18n.t('skills:mine.browseStore'),
                style: const TextStyle(fontSize: FontSizes.sm),
              ),
            ),
          ],
        ),
      );
    }

    final groups = groupSkills(skills);
    final personal = groups.where((g) => g.isPersonal).toList();
    final installed = groups.where((g) => !g.isPersonal).toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (showSkills && personal.isNotEmpty)
          SkillGroupSection(
            title: i18n.t('skills:section.personal', count: personal.length),
            groups: personal,
            unmetFor: unmetFor,
            actions: actions,
          ),
        if (showSkills && installed.isNotEmpty)
          SkillGroupSection(
            title: i18n.t('skills:section.skills', count: installed.length),
            groups: installed,
            unmetFor: unmetFor,
            actions: actions,
          ),
        if (showMcp && servers.isNotEmpty) ...[
          Padding(
            padding: const EdgeInsets.only(bottom: 7),
            child: Text(
              i18n.t('skills:section.mcp', count: servers.length),
              style: TextStyle(
                fontSize: FontSizes.xs,
                fontWeight: FontWeight.w500,
                color: t.n600,
              ),
            ),
          ),
          for (final server in servers)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: EntryRow(
                name: server.name,
                description: server.subtitle,
                warning: server.status == 'error' ? server.error : null,
                badges: [
                  SkillBadge(
                    text: i18n.t('skills:status.${server.status}'),
                    tone: server.isConnected
                        ? BadgeTone.ok
                        : server.status == 'error'
                            ? BadgeTone.warn
                            : BadgeTone.muted,
                  ),
                  if (server.isConnected)
                    SkillBadge(
                      text: i18n.t('skills:badge.tools',
                          count: server.tools.length),
                    ),
                ],
                actions: [
                  if (server.isConnected)
                    IconAction(
                      icon: Icons.link_off,
                      tooltip: i18n.t('skills:action.disconnect'),
                      disabled: actions.busy,
                      onTap: () => onDisconnect(server.name),
                    )
                  else
                    IconAction(
                      icon: Icons.link,
                      tooltip: i18n.t('skills:action.connect'),
                      disabled: actions.busy,
                      onTap: () => onConnect(server.name),
                    ),
                  IconAction(
                    icon: Icons.delete_outline,
                    tooltip: i18n.t('skills:action.remove'),
                    danger: true,
                    disabled: actions.busy,
                    onTap: () => onRemoveServer(server.name),
                  ),
                ],
              ),
            ),
        ],
      ],
    );
  }
}
