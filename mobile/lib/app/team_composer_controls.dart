import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/chat/state/config_providers.dart';
import '../features/teams/widgets/team_picker.dart';
import '../shared/models/session.dart';
import '../shared/models/team.dart';

/// Cross-feature composition stays in app: the chat composer cannot import
/// the teams feature, so the team picker is handed to it as one more pill on
/// its toolbar row (web: TeamPicker beside the chat model). The chat input
/// keeps ownership of the mode picker and of send semantics.
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
    final mode =
        ref.watch(pickedAgentProvider(sessionKey)) ??
        session?.agent ??
        config?.defaultAgent;
    if (!enabled || mode != 'team') return const SizedBox.shrink();
    final request =
        ref.watch(pickedTeamProvider(sessionKey)) ?? const TeamRequest();
    return Padding(
      padding: const EdgeInsetsDirectional.only(end: 6),
      child: TeamPicker(
        scope: scope,
        value: request,
        disabled: busy,
        onChanged: (value) =>
            ref.read(pickedTeamProvider(sessionKey).notifier).state = value,
      ),
    );
  }
}
