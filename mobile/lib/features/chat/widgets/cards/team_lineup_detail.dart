import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/models/json.dart';

/// The lineup on the team proposal card (web `TeamLineupDetail`): who is on
/// the team, then the few key-value lines about limits and scope. Explanatory
/// content only — the QuestionDock around it owns answers, rejection, drafts,
/// revision conflicts and multi-device synchronization.
class TeamLineupDetail extends ConsumerWidget {
  const TeamLineupDetail({super.key, required this.item});

  final QuestionItem item;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = item.detail ?? const <String, dynamic>{};
    if (detail['kind'] != 'team_lineup') return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final spec = asMap(detail['spec']);
    final policy = asMap(spec['policy']);
    final grant = asMap(detail['grant']);
    final paid = asMap(grant['paid_tools']);
    final members = asList(detail['members']).map(asMap).toList();

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: t.n100,
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if ((asString(spec['name']) ?? '').isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                asString(spec['name'])!,
                style: TextStyle(
                  fontSize: FontSizes.base,
                  fontWeight: FontWeight.w600,
                  color: t.ink,
                ),
              ),
            ),
          _LineupRow(
            name: i18n.t('teams:coordinator'),
            meta: asString(asMap(detail['coordinator'])['model']) ?? '',
          ),
          for (final member in members)
            _LineupRow(
              name: asString(member['name']) ?? asString(member['alias']) ?? '',
              meta: [
                () {
                  final source = asString(member['source']);
                  if (source == null) return '';
                  final label = i18n.t('teams:source.$source');
                  return label == 'teams:source.$source' ? '' : label;
                }(),
                asString(member['model']) ?? '',
              ].where((part) => part.isNotEmpty).join(' · '),
              responsibility:
                  asString(member['responsibility']) ??
                  asString(member['description']) ??
                  '',
              skills: asList(member['skills'])
                  .map((skill) => asString(asMap(skill)['name']) ?? '')
                  .where((name) => name.isNotEmpty)
                  .toList(),
              tools: asList(member['tool_ids']).map((tool) => '$tool').toList(),
              mcp: [
                for (final mcp in asList(member['mcp_refs']).map(asMap))
                  '${mcp['server'] ?? mcp['name'] ?? ''}: '
                      '${asList(mcp['tools']).join(', ')}',
              ],
            ),
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Divider(color: t.hair, height: 1),
          ),
          _Fact(
            text: i18n.t(
              'teams:concurrencyValue',
              count: asInt(policy['max_concurrent_members']) ?? 0,
            ),
          ),
          _Fact(
            text: i18n.t(
              'teams:durationValue',
              count: ((asInt(policy['max_wall_time_seconds']) ?? 0) / 60)
                  .ceil(),
            ),
          ),
          _Fact(
            text: i18n.t(
              policy['member_selection'] == 'explicit_only'
                  ? 'teams:fixedTeam'
                  : 'teams:supplementEnabled',
            ),
          ),
          for (final rule in asList(grant['permission_rules']).map(asMap))
            _Fact(
              text: '${rule['permission'] ?? ''} · ${rule['pattern'] ?? ''}',
            ),
          for (final mcp in asList(grant['mcp_refs']).map(asMap))
            _Fact(
              text:
                  'MCP ${mcp['server'] ?? mcp['name'] ?? ''} · '
                  '${asList(mcp['tools']).join(', ')}',
            ),
          if (paid.isNotEmpty)
            _Fact(
              // Paid tools are the one scope worth calling out before the run
              // starts: they spend account credits.
              text:
                  '${i18n.t('teams:paidAuthorization')} · ${paid.keys.join(', ')}',
              highlight: true,
            ),
        ],
      ),
    );
  }
}

class _LineupRow extends StatelessWidget {
  const _LineupRow({
    required this.name,
    required this.meta,
    this.responsibility = '',
    this.skills = const [],
    this.tools = const [],
    this.mcp = const [],
  });

  final String name, meta, responsibility;
  final List<String> skills, tools, mcp;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final detail = [
      if (skills.isNotEmpty) skills.join(' · '),
      if (tools.isNotEmpty) tools.join(', '),
      ...mcp.map((entry) => 'MCP $entry'),
    ].join(' · ');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              // The name is capped rather than flexible so a short one leaves
              // its slack to the model, instead of both halving the row and
              // truncating a model id that would have fit.
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 160),
                child: Text(
                  name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
              ),
              if (meta.isNotEmpty) ...[
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    meta,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ),
              ],
            ],
          ),
          if (responsibility.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                responsibility,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.n700,
                  height: 1.6,
                ),
              ),
            ),
          if (detail.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                detail,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.n600,
                  height: 1.6,
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _Fact extends StatelessWidget {
  const _Fact({required this.text, this.highlight = false});

  final String text;
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Text(
        text,
        style: TextStyle(
          fontSize: FontSizes.xs,
          color: highlight ? t.a700 : t.n600,
          height: 1.6,
        ),
      ),
    );
  }
}
