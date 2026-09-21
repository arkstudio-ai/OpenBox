import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/chat/state/config_providers.dart';
import '../features/teams/state/team_providers.dart';
import '../features/teams/widgets/team_picker.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/session.dart';
import '../shared/models/team.dart';

/// Cross-feature composition stays in app. The chat input owns send semantics.
class TeamComposerControls extends ConsumerWidget {
  const TeamComposerControls({
    super.key,
    required this.scope,
    required this.sessionKey,
    this.session,
    this.busy = false,
  });
  final TeamScope scope;
  final String sessionKey;
  final Session? session;
  final bool busy;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final config = ref.watch(appConfigProvider).valueOrNull;
    final enabled =
        config?.teamUiEnabled == true && config?.teamAdmissionEnabled == true;
    final i18n = ref.watch(i18nProvider);
    final mode =
        ref.watch(pickedAgentProvider(sessionKey)) ?? session?.agent ?? 'build';
    final request =
        ref.watch(pickedTeamProvider(sessionKey)) ?? const TeamRequest();
    final templates = enabled && mode == 'team'
        ? ref.watch(teamTemplatesProvider(scope)).valueOrNull
        : null;
    final selected = templates
        ?.where((item) => item.id == request.templateId)
        .firstOrNull;
    return Material(
      type: MaterialType.transparency,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Wrap(
              spacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                PopupMenuButton<String>(
                  enabled: !busy,
                  tooltip: i18n.t('chat:mode.label'),
                  initialValue: mode,
                  onSelected: (value) =>
                      ref.read(pickedAgentProvider(sessionKey).notifier).state =
                          value,
                  itemBuilder: (_) => [
                    for (final value in ['build', 'plan', if (enabled) 'team'])
                      PopupMenuItem(
                        value: value,
                        child: Text(i18n.t('chat:mode.$value')),
                      ),
                  ],
                  child: Padding(
                    padding: const EdgeInsets.all(8),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(i18n.t('chat:mode.$mode')),
                        const Icon(Icons.expand_more, size: 16),
                      ],
                    ),
                  ),
                ),
                if (enabled && mode == 'team')
                  TeamPicker(
                    scope: scope,
                    value: request,
                    disabled: busy,
                    onChanged: (value) =>
                        ref
                                .read(pickedTeamProvider(sessionKey).notifier)
                                .state =
                            value,
                  ),
              ],
            ),
            if (enabled && mode == 'team')
              CheckboxListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                controlAffinity: ListTileControlAffinity.leading,
                title: Text(i18n.t('teams:allowSupplement')),
                value:
                    request.allowSupplement ??
                    selected?.allowSupplement ??
                    true,
                onChanged: busy || request.templateId == null
                    ? null
                    : (value) =>
                          ref
                              .read(pickedTeamProvider(sessionKey).notifier)
                              .state = TeamRequest(
                            templateId: request.templateId,
                            allowSupplement: value,
                          ),
              ),
          ],
        ),
      ),
    );
  }
}
