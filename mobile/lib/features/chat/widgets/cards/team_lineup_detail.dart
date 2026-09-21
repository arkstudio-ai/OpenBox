import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/models/json.dart';

/// Explanatory content only; the original QuestionDock owns answers,
/// rejection, drafts, revision conflicts and multi-device synchronization.
class TeamLineupDetail extends ConsumerWidget {
  const TeamLineupDetail({super.key, required this.item});
  final QuestionItem item;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = item.detail ?? const <String, dynamic>{};
    if (detail['kind'] != 'team_lineup') return const SizedBox.shrink();
    final i18n = ref.watch(i18nProvider);
    final spec = asMap(detail['spec']);
    final policy = asMap(spec['policy']);
    final grant = asMap(detail['grant']);
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: context.tokens.hair),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            asString(spec['name']) ?? '',
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
          Text(
            '${i18n.t('teams:coordinator')} · ${asMap(detail['coordinator'])['model'] ?? ''}',
          ),
          for (final member in asList(detail['members']).map(asMap))
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    '${member['name'] ?? member['alias'] ?? ''} · ${member['model'] ?? ''}',
                  ),
                  Text(
                    asString(member['responsibility']) ??
                        asString(member['description']) ??
                        '',
                  ),
                  Text(
                    '${i18n.t('teams:skills')}: ${asList(member['skills']).map((s) => asMap(s)['name']).join(', ')}',
                  ),
                  Text(
                    '${i18n.t('teams:tools')}: ${asList(member['tool_ids']).join(', ')}',
                  ),
                  for (final mcp in asList(member['mcp_refs']).map(asMap))
                    Text(
                      'MCP ${mcp['server'] ?? mcp['name'] ?? ''}: ${asList(mcp['tools']).join(', ')}',
                    ),
                ],
              ),
            ),
          Text(
            i18n.t(
              'teams:concurrencyValue',
              count: asInt(policy['max_concurrent_members']) ?? 0,
            ),
          ),
          Text(
            i18n.t(
              'teams:durationValue',
              count: ((asInt(policy['max_wall_time_seconds']) ?? 0) / 60)
                  .ceil(),
            ),
          ),
          Text(
            i18n.t(
              policy['member_selection'] == 'explicit_only'
                  ? 'teams:fixedTeam'
                  : 'teams:supplementEnabled',
            ),
          ),
          for (final rule in asList(grant['permission_rules']).map(asMap))
            Text('${rule['permission'] ?? ''}: ${rule['pattern'] ?? ''}'),
          for (final mcp in asList(grant['mcp_refs']).map(asMap))
            Text(
              'MCP ${mcp['server'] ?? mcp['name'] ?? ''}: ${asList(mcp['tools']).join(', ')}',
            ),
          if (asMap(grant['paid_tools']).isNotEmpty) ...[
            Text(i18n.t('teams:paidAuthorization')),
            for (final name in asMap(grant['paid_tools']).keys) Text(name),
          ],
        ],
      ),
    );
  }
}
