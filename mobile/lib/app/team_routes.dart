import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/widgets/attachment_gallery.dart';
import '../features/chat/widgets/audio_preview.dart';
import '../features/chat/widgets/markdown_view.dart';
import '../features/teams/state/team_providers.dart';
import '../features/teams/team_run_screen.dart';
import '../features/teams/widgets/team_progress_card.dart';
import '../features/workbench/workbench_screen.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/json.dart';
import '../shared/models/message_part.dart';
import '../shared/models/team.dart';
import '../shared/router/paths.dart';

TeamScope? currentTeamScope(WidgetRef ref) {
  final userId = ref.watch(authProvider).user?.id;
  final workspaceId = ref.watch(activeWorkspaceProvider).valueOrNull?.currentId;
  return userId == null || workspaceId == null
      ? null
      : (userId: userId, workspaceId: workspaceId);
}

class TeamTurnTools extends StatelessWidget {
  const TeamTurnTools({super.key, required this.scope, required this.parts});
  final TeamScope scope;
  final List<MessagePart> parts;
  @override
  Widget build(BuildContext context) {
    final runIds = <String>{};
    for (final part in parts.whereType<ToolPart>()) {
      if (part.tool == 'team_propose' &&
          part.status == ToolStatus.completed &&
          part.error == null &&
          part.metadata['error'] != true &&
          asMap(part.input)['mode'] != 'amend') {
        final id = asString(part.metadata['team_run_id']);
        if (id != null) runIds.add(id);
      }
    }
    return Column(
      children: [
        for (final id in runIds)
          TeamProgressCard(
            key: ValueKey(id),
            scope: scope,
            runId: id,
            onDetails: () => context.push(Paths.teamRun(id)),
          ),
      ],
    );
  }
}

class TeamRunRoute extends ConsumerWidget {
  const TeamRunRoute({super.key, required this.runId});
  final String runId;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = currentTeamScope(ref);
    if (scope == null) return const SizedBox.shrink();
    return TeamRunScreen(
      key: ValueKey((scope, runId)),
      scope: scope,
      runId: runId,
      onOpenChat: (id) => context.push(Paths.chat(id)),
      renderText: (text) => MarkdownView(text),
      renderArtifact: (artifact) => _TeamArtifact(artifact: artifact),
    );
  }
}

class _TeamArtifact extends ConsumerWidget {
  const _TeamArtifact({required this.artifact});
  final Map<String, dynamic> artifact;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final asset = asMap(artifact['asset']);
    final id = asString(asset['id']);
    if (id == null) {
      return ListTile(
        title: Text(asString(artifact['name']) ?? ''),
        subtitle: Text(ref.watch(i18nProvider).t('teams:artifactUnavailable')),
      );
    }
    final part = FilePart(
      id: asString(artifact['id']) ?? id,
      path: asString(asset['name']) ?? id,
      assetId: id,
      mimeType: asString(asset['mime']),
    );
    if (isGalleryMedia(part)) return AttachmentGallery(parts: [part]);
    if (isAudioPart(part)) return AudioPreview(part: part);
    return FileChipRow(name: part.path, assetId: part.assetId);
  }
}

class TeamWorkbenchRoute extends ConsumerWidget {
  const TeamWorkbenchRoute({
    super.key,
    required this.sessionId,
    this.initialTab = WorkbenchScreen.menuTab,
    this.initialControl = false,
  });
  final String sessionId, initialTab;
  final bool initialControl;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = currentTeamScope(ref);
    return WorkbenchScreen(
      sessionId: sessionId,
      initialTab: initialTab,
      initialControl: initialControl,
      extraMenu: scope == null
          ? null
          : _TeamRunEntry(scope: scope, sessionId: sessionId),
    );
  }
}

class _TeamRunEntry extends ConsumerWidget {
  const _TeamRunEntry({required this.scope, required this.sessionId});
  final TeamScope scope;
  final String sessionId;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final runs = ref.watch(
      sessionTeamRunsProvider((scope: scope, sessionId: sessionId)),
    );
    return Column(
      children: [
        for (final run in runs.valueOrNull ?? const <TeamRun>[])
          ListTile(
            leading: const Icon(Icons.groups_outlined),
            title: Text(run.title),
            subtitle: Text(
              ref.watch(i18nProvider).t('teams:state.${run.state}'),
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => context.push(Paths.teamRun(run.id)),
          ),
      ],
    );
  }
}
