import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../state/team_providers.dart';
import 'team_bits.dart';

/// The library's Agent and template tabs (web `DefinitionList`). Read-only on
/// a phone: §13.6 keeps creating and editing definitions on the Web, so a row
/// reports what a definition is and — for a team — offers to run it.
class DefinitionList extends ConsumerWidget {
  const DefinitionList({
    super.key,
    required this.scope,
    required this.kind,
    required this.onRun,
    required this.onOpenSource,
  });

  final TeamScope scope;

  /// `agent` or `team`.
  final String kind;
  final ValueChanged<TeamDefinition> onRun;
  final ValueChanged<String> onOpenSource;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final key = (scope: scope, kind: kind);
    final entries = ref.watch(teamCatalogProvider(key));
    final rows = entries.valueOrNull;

    if (rows == null) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 32),
        child: Center(
          child: entries.hasError
              ? Column(
                  children: [
                    Text(
                      errorText(i18n, entries.error!),
                      textAlign: TextAlign.center,
                      style: TextStyle(fontSize: FontSizes.sm, color: t.danger),
                    ),
                    const SizedBox(height: 8),
                    TeamActionLink(
                      label: i18n.t('common:action.retry'),
                      onTap: () => ref.invalidate(teamCatalogProvider(key)),
                    ),
                  ],
                )
              : const Spinner(size: 20),
        ),
      );
    }
    if (rows.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 28),
        child: Column(
          children: [
            Text(
              i18n.t('teams:emptyDefinitions'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
            ),
            const SizedBox(height: 4),
            Text(
              i18n.t(
                kind == 'team'
                    ? 'teams:emptyTemplatesHint'
                    : 'teams:emptyAgentsHint',
              ),
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: FontSizes.xs,
                color: t.n600,
                height: 1.6,
              ),
            ),
          ],
        ),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final definition in rows)
          _DefinitionRow(
            definition: definition,
            onRun: () => onRun(definition),
            onOpenSource: onOpenSource,
          ),
      ],
    );
  }
}

class _DefinitionRow extends ConsumerWidget {
  const _DefinitionRow({
    required this.definition,
    required this.onRun,
    required this.onOpenSource,
  });

  final TeamDefinition definition;
  final VoidCallback onRun;
  final ValueChanged<String> onOpenSource;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final team = definition.isTeam;
    final aliases = definition.memberAliases;
    final source = definition.source;
    final sourceLabel = () {
      if (source.isEmpty || source == 'user') return null;
      final label = i18n.t('teams:source.$source');
      return label == 'teams:source.$source' ? null : label;
    }();
    final summary = team
        ? (aliases.isEmpty ? i18n.t('teams:autoTeam') : aliases.join(' · '))
        : i18n.t(
            'teams:agentSummary',
            vars: {
              'model':
                  asString(definition.capability['model']) ??
                  i18n.t('teams:followDefault'),
              'skills': asList(definition.spec['skill_refs']).length,
              'tools': asList(definition.spec['tool_allowlist']).length,
            },
          );
    final footnote = [
      if (team && definition.allowSupplement) i18n.t('teams:supplementBadge'),
      if (definition.runCount != null)
        i18n.t('teams:runCount', count: definition.runCount!),
    ].join(' · ');

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          team
              ? _MemberStack(previews: definition.memberPreviews)
              : TeamAvatar(display: asMap(definition.spec['display'])),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Wrap(
                  spacing: 8,
                  runSpacing: 4,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Text(
                      definition.name,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        fontWeight: FontWeight.w500,
                        color: t.ink,
                      ),
                    ),
                    TeamStatePill(
                      label: i18n.t('teams:state.${definition.status}'),
                    ),
                    if (sourceLabel != null)
                      Text(
                        sourceLabel,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                    if (definition.containsPaidTools)
                      Text(
                        i18n.t('teams:containsPaidTools'),
                        style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                      ),
                  ],
                ),
                if (definition.description.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      definition.description,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        color: t.n600,
                        height: 1.6,
                      ),
                    ),
                  ),
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    summary,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                  ),
                ),
                if (footnote.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      footnote,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                  ),
                if (definition.sourceSessionId != null)
                  Align(
                    alignment: AlignmentDirectional.centerStart,
                    child: TeamActionLink(
                      label: i18n.t('teams:sourceConversation'),
                      onTap: () => onOpenSource(definition.sourceSessionId!),
                    ),
                  ),
              ],
            ),
          ),
          if (team && definition.status == 'active')
            IconButton(
              onPressed: onRun,
              tooltip: i18n.t('teams:run'),
              icon: Icon(Icons.play_arrow_outlined, size: 20, color: t.n700),
            ),
        ],
      ),
    );
  }
}

/// A team's roster at a glance: the first few member icons, overlapped.
class _MemberStack extends StatelessWidget {
  const _MemberStack({required this.previews});

  final List<Map<String, dynamic>> previews;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final shown = previews.take(3).toList();
    if (shown.isEmpty) {
      return Container(
        width: 34,
        height: 34,
        decoration: BoxDecoration(
          color: t.card,
          borderRadius: BorderRadius.circular(Radii.md),
          border: Border.all(color: t.hair),
        ),
        child: Icon(Icons.groups_outlined, size: 17, color: t.n700),
      );
    }
    // Each disc carries a ring in the row's own colour, so overlapping
    // members stay countable instead of merging into one shape.
    const size = 30.0;
    const step = 20.0;
    return SizedBox(
      width: size + 3 + (shown.length - 1) * step,
      height: size + 3,
      child: Stack(
        children: [
          for (final (index, member) in shown.indexed)
            PositionedDirectional(
              start: index * step,
              child: Container(
                padding: const EdgeInsets.all(1.5),
                decoration: BoxDecoration(
                  color: t.hairSoft,
                  shape: BoxShape.circle,
                ),
                child: TeamAvatar(
                  display: asMap(member['display']),
                  size: size,
                  circular: true,
                ),
              ),
            ),
        ],
      ),
    );
  }
}
