import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import '../../../shared/widgets/fold.dart';
import '../utils/group_skills.dart';
import '../utils/listing.dart';
import 'entry_row.dart';

/// Actions a skill row offers (web `SkillGroupActions`).
class SkillGroupActions {
  const SkillGroupActions({
    required this.uninstall,
    required this.fixDependencies,
    required this.publish,
    required this.withdraw,
    required this.download,
    required this.busy,
  });

  /// Called with the install directory and how many skills live in it.
  final void Function(String dir, int count) uninstall;
  final void Function(InstalledSkill skill) fixDependencies;
  final void Function(SkillGroup group) publish;
  final void Function(SkillGroup group) withdraw;
  final void Function(String dir) download;
  final bool busy;
}

class SkillGroupSection extends ConsumerStatefulWidget {
  const SkillGroupSection({
    super.key,
    required this.title,
    required this.groups,
    required this.unmetFor,
    required this.actions,
  });

  final String title;
  final List<SkillGroup> groups;

  /// Declared servers that are not usable yet, per skill.
  final List<SkillDependency> Function(InstalledSkill skill) unmetFor;
  final SkillGroupActions actions;

  @override
  ConsumerState<SkillGroupSection> createState() => _SkillGroupSectionState();
}

class _SkillGroupSectionState extends ConsumerState<SkillGroupSection> {
  final _open = <String>{};

  /// Expanded reasons are tracked apart from expanded packs: a rejected pack
  /// can have both open at once, and one toggle closing the other reads as a
  /// bug.
  final _reasons = <String>{};

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.only(bottom: 7),
          child: Text(
            widget.title,
            style: TextStyle(
              fontSize: FontSizes.xs,
              fontWeight: FontWeight.w500,
              color: t.n600,
            ),
          ),
        ),
        for (final group in widget.groups) _group(i18n, group),
        const SizedBox(height: 12),
      ],
    );
  }

  Widget _group(I18nState i18n, SkillGroup group) {
    final t = context.tokens;
    final missing = <String>{
      for (final member in group.members)
        for (final dep in widget.unmetFor(member)) dep.name,
    }.toList();
    final expanded = _open.contains(group.id);
    final actions = widget.actions;
    final chip = group.isPersonal
        ? listingChipFor(group.publicationStatus, group.listing)
        : null;
    final reason = chip != null && explainsItself(chip) ? group.listingNote : null;
    final reasonOpen = _reasons.contains(group.id);

    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          EntryRow(
            icon: group.icon,
            name: group.name,
            description: group.isPack
                ? group.members.map((m) => m.name).join(', ')
                : group.description,
            warning: missing.isEmpty
                ? null
                : i18n.t('skills:mine.missingDependency',
                    vars: {'names': missing.join(', ')}),
            onFixWarning: missing.isEmpty
                ? null
                : () => actions.fixDependencies(group.members.first),
            fixLabel: i18n.t('skills:deps.fixNow'),
            fixDisabled: actions.busy,
            badges: [
              if (group.isPack)
                SkillBadge(
                  text: i18n.t('skills:badge.packCount',
                      count: group.members.length),
                ),
              if (group.isPersonal) ...[
                SkillBadge(text: i18n.t('skills:badge.personal')),
                if (group.isOfficial)
                  SkillBadge(text: i18n.t('skills:badge.official')),
                // One chip, not two: `publicationStatus` and `listing` are
                // separate facts, but a withdrawn-yet-approved package read as
                // still on sale when they were rendered side by side.
                if (chip != null)
                  SkillBadge(
                    text: i18n.t('skills:badge.listing.${chip.key}'),
                    tone: listingTones[chip]!,
                  )
                else
                  SkillBadge(
                    text: i18n.t('skills:badge.unpublished'),
                    tone: BadgeTone.warn,
                  ),
              ] else if (group.category == 'store')
                SkillBadge(text: i18n.t('skills:badge.storeInstalled'))
              else if (group.origin != 'container')
                SkillBadge(text: i18n.t('skills:badge.${group.origin}')),
            ],
            actions: [
              if (group.isPersonal) ...[
                IconAction(
                  icon: Icons.cloud_upload_outlined,
                  tooltip: i18n.t(isResubmission(chip)
                      ? 'skills:action.resubmit'
                      : group.isPublished
                          ? 'skills:action.updatePublish'
                          : 'skills:action.publish'),
                  disabled: actions.busy,
                  onTap: () => actions.publish(group),
                ),
                if (canWithdraw(chip))
                  IconAction(
                    icon: Icons.cloud_off_outlined,
                    tooltip: i18n.t('skills:action.withdraw'),
                    disabled: actions.busy,
                    onTap: () => actions.withdraw(group),
                  ),
                IconAction(
                  icon: Icons.file_download_outlined,
                  tooltip: i18n.t('skills:action.download'),
                  disabled: actions.busy,
                  onTap: () => actions.download(group.id),
                ),
              ],
              if (group.isPack)
                IconAction(
                  icon: expanded ? Icons.expand_less : Icons.chevron_right,
                  tooltip: i18n.t(expanded
                      ? 'skills:action.collapse'
                      : 'skills:action.expand'),
                  onTap: () => setState(() {
                    expanded ? _open.remove(group.id) : _open.add(group.id);
                  }),
                ),
              if (group.removable)
                IconAction(
                  icon: Icons.delete_outline,
                  tooltip: i18n.t('skills:action.uninstall'),
                  danger: true,
                  disabled: actions.busy,
                  onTap: () =>
                      actions.uninstall(group.id, group.members.length),
                ),
            ],
          ),
          // A refusal truncated into a badge is a refusal nobody reads. The
          // reason gets its own line, in full, one tap away.
          if (reason != null && reason.isNotEmpty) ...[
            Padding(
              padding: const EdgeInsets.only(left: 22, top: 4),
              child: Align(
                alignment: AlignmentDirectional.centerStart,
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTap: () => setState(() {
                    reasonOpen
                        ? _reasons.remove(group.id)
                        : _reasons.add(group.id);
                  }),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: Text(
                      i18n.t(reasonOpen
                          ? 'skills:mine.hideReason'
                          : 'skills:mine.showReason'),
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        color: t.n600,
                        decoration: TextDecoration.underline,
                      ),
                    ),
                  ),
                ),
              ),
            ),
            Fold(
              open: reasonOpen,
              child: Container(
                margin: const EdgeInsets.only(left: 22, top: 2),
                padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
                decoration: BoxDecoration(
                  color: t.hairSoft.withValues(alpha: 0.6),
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: Text(
                  i18n.t('skills:mine.listingReason', vars: {'note': reason}),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n700,
                  ),
                ),
              ),
            ),
          ],
          if (group.isPack)
            Fold(
              open: expanded,
              child: Container(
                margin: const EdgeInsets.only(left: 22, top: 4),
                padding: const EdgeInsets.only(left: 11),
                decoration: BoxDecoration(
                  border: Border(left: BorderSide(color: t.hair)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    for (final member in group.members)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 2),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.baseline,
                          textBaseline: TextBaseline.alphabetic,
                          children: [
                            Text(
                              member.name,
                              style: TextStyle(
                                  fontSize: FontSizes.xs, color: t.ink),
                            ),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Text(
                                member.description ?? '',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                    fontSize: FontSizes.xs, color: t.n600),
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}
