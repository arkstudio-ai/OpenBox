import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../state/team_providers.dart';

class TeamPicker extends ConsumerWidget {
  const TeamPicker({
    super.key,
    required this.scope,
    required this.value,
    required this.onChanged,
    this.disabled = false,
  });
  final TeamScope scope;
  final TeamRequest value;
  final ValueChanged<TeamRequest> onChanged;
  final bool disabled;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final result = ref.watch(teamTemplatesProvider(scope));
    final i18n = ref.watch(i18nProvider);
    final templates = result.valueOrNull ?? const <TeamTemplate>[];
    final selected = templates
        .where((t) => t.id == value.templateId)
        .firstOrNull;
    return OutlinedButton.icon(
      icon: const Icon(Icons.groups_outlined, size: 16),
      label: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 170),
        child: Text(
          selected?.name ?? i18n.t('teams:autoTeam'),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
      ),
      onPressed: disabled
          ? null
          : () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              builder: (context) => SafeArea(
                child: FractionallySizedBox(
                  heightFactor: 0.65,
                  child: Consumer(
                    builder: (context, ref, _) {
                      final loaded = ref.watch(teamTemplatesProvider(scope));
                      return ListView(
                        children: [
                          ListTile(title: Text(i18n.t('teams:chooseTeam'))),
                          ListTile(
                            title: Text(i18n.t('teams:autoTeam')),
                            subtitle: Text(i18n.t('teams:autoTeamHint')),
                            trailing: value.templateId == null
                                ? const Icon(Icons.check)
                                : null,
                            onTap: () {
                              onChanged(
                                const TeamRequest(allowSupplement: true),
                              );
                              Navigator.pop(context);
                            },
                          ),
                          if (loaded.isLoading) const LinearProgressIndicator(),
                          if (loaded.hasError)
                            ListTile(
                              title: Text(errorText(i18n, loaded.error!)),
                              trailing: TextButton(
                                onPressed: () => ref.invalidate(
                                  teamTemplatesProvider(scope),
                                ),
                                child: Text(i18n.t('common:action.retry')),
                              ),
                            ),
                          for (final template
                              in loaded.valueOrNull ?? const <TeamTemplate>[])
                            ListTile(
                              title: Text(template.name),
                              subtitle: Text(template.description),
                              trailing: template.id == value.templateId
                                  ? const Icon(Icons.check)
                                  : null,
                              onTap: () {
                                onChanged(TeamRequest(templateId: template.id));
                                Navigator.pop(context);
                              },
                            ),
                        ],
                      );
                    },
                  ),
                ),
              ),
            ),
    );
  }
}
