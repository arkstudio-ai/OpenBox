import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/empty_chat_screen.dart';
import '../features/chat/state/config_providers.dart';
import '../features/chat/widgets/attachment_gallery.dart';
import '../features/chat/widgets/audio_preview.dart';
import '../features/chat/widgets/markdown_view.dart';
import '../features/teams/agent_teams_screen.dart';
import '../features/teams/api/teams_api.dart';
import '../features/teams/state/team_providers.dart';
import '../features/teams/team_run_screen.dart';
import '../features/teams/widgets/team_progress_card.dart';
import '../features/workbench/widgets/workbench_menu.dart';
import '../features/workbench/workbench_screen.dart';
import '../features/workspace/state/active_workspace_store.dart';
import '../features/workspace/state/workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/appearance/tokens.dart';
import '../shared/appearance/type_scale.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/json.dart';
import '../shared/models/message_part.dart';
import '../shared/models/project.dart';
import '../shared/models/team.dart';
import '../shared/router/paths.dart';

TeamScope? currentTeamScope(WidgetRef ref) {
  final userId = ref.watch(authProvider).user?.id;
  final workspaceId = ref.watch(activeWorkspaceProvider).valueOrNull?.currentId;
  return userId == null || workspaceId == null
      ? null
      : (userId: userId, workspaceId: workspaceId);
}

/// The drawer's "Agent 团队" destination. Starting a saved team from here is
/// the same act as picking it in the composer: choose a project, land on the
/// empty conversation with team mode and the template already selected
/// (§13.4). A rerun adds the earlier goal as the draft text (§13.8).
class AgentTeamsRoute extends ConsumerWidget {
  const AgentTeamsRoute({super.key, this.initialTab = 'agents'});

  final String initialTab;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = currentTeamScope(ref);
    if (scope == null) return const SizedBox.shrink();
    final config = ref.watch(appConfigProvider).valueOrNull;
    final workspace = ref.watch(workspaceProvider).valueOrNull;

    void start({String? projectId, String? templateId, String? goal}) {
      if (projectId != null) {
        ref.read(selectedProjectProvider.notifier).state = projectId;
      }
      ref.read(pickedAgentProvider(draftSessionKey).notifier).state = 'team';
      ref.read(pickedTeamProvider(draftSessionKey).notifier).state =
          TeamRequest(templateId: templateId);
      ref.read(draftPromptProvider.notifier).state = goal;
      context.go(Paths.app);
    }

    return AgentTeamsScreen(
      scope: scope,
      initialTab: initialTab,
      enabled: config?.teamUiEnabled == true,
      projects: [
        for (final project in workspace?.projects ?? const <Project>[])
          (id: project.id, name: project.name),
      ],
      onOpenChat: (sessionId) => context.push(Paths.chat(sessionId)),
      onRunTemplate: (definition) => _pickProject(
        context,
        ref,
        onPick: (projectId) =>
            start(projectId: projectId, templateId: definition.id),
      ),
      // The history list does not carry the goal, so read it from the run
      // itself (web does the same). A failed read still starts the team —
      // the user simply types the goal again.
      onRunAgain: (run) async {
        String? goal;
        try {
          final snapshot = await ref
              .read(teamsApiProvider)
              .snapshot(scope, run.id);
          goal = snapshot.run.goal.isEmpty ? null : snapshot.run.goal;
        } catch (_) {
          goal = null;
        }
        start(projectId: run.projectId, templateId: run.templateId, goal: goal);
      },
    );
  }
}

/// Which project the new conversation belongs to — the same choice the
/// scheduled-tasks "create via chat" dialog asks for.
Future<void> _pickProject(
  BuildContext context,
  WidgetRef ref, {
  required ValueChanged<String?> onPick,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final projects =
      ref.read(workspaceProvider).valueOrNull?.projects ?? const <Project>[];
  return showModalBottomSheet<void>(
    context: context,
    backgroundColor: t.card,
    isScrollControlled: true,
    builder: (sheetContext) => SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * 0.6,
        ),
        child: ListView(
          shrinkWrap: true,
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
              child: Text(
                i18n.t('teams:project'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w600,
                  color: t.n600,
                ),
              ),
            ),
            for (final project in projects)
              ListTile(
                dense: true,
                title: Text(
                  project.name,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                ),
                onTap: () {
                  Navigator.pop(sheetContext);
                  onPick(project.id);
                },
              ),
          ],
        ),
      ),
    ),
  );
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
    final t = context.tokens;
    final asset = asMap(artifact['asset']);
    final id = asString(asset['id']);
    if (id == null) {
      // The row stays: a registered result whose file is gone is information,
      // and dropping it would read as "nothing was produced".
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            asString(artifact['name']) ?? asString(artifact['title']) ?? '',
            style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
          ),
          const SizedBox(height: 2),
          Text(
            ref.watch(i18nProvider).t('teams:artifactUnavailable'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
        ],
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
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final runs = ref.watch(
      sessionTeamRunsProvider((scope: scope, sessionId: sessionId)),
    );
    return Column(
      children: [
        for (final run in runs.valueOrNull ?? const <TeamRun>[])
          WorkbenchMenuRow(
            glyph: workbenchGlyphs['team'] ?? '',
            label: i18n.t('workbench:menu.team'),
            hint: i18n.t('teams:state.${run.state}'),
            tokens: t,
            onTap: () => context.push(Paths.teamRun(run.id)),
          ),
      ],
    );
  }
}
