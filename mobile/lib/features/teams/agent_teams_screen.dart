import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/team.dart';
import '../../shared/widgets/section_tabs.dart';
import 'state/team_providers.dart';
import 'widgets/definition_list.dart';
import 'widgets/run_history.dart';

/// The "Agent 团队" page behind the drawer entry (web `AgentLibraryRoute`):
/// my Agents, team templates, run history. Editing lives on the Web (§13.6),
/// so the phone lists what exists, starts a saved team and reopens a run.
class AgentTeamsScreen extends ConsumerStatefulWidget {
  const AgentTeamsScreen({
    super.key,
    required this.scope,
    required this.enabled,
    required this.projects,
    required this.onRunTemplate,
    required this.onRunAgain,
    required this.onOpenChat,
    this.initialTab = 'agents',
  });

  final TeamScope scope;

  /// The deployment's `team_ui_enabled`; false shows why the page is empty
  /// rather than an empty list that looks broken.
  final bool enabled;
  final List<({String id, String name})> projects;

  /// Start a saved template: pick a project, then open a new conversation
  /// with team mode and this template already chosen (§13.4).
  final ValueChanged<TeamDefinition> onRunTemplate;

  /// Same, seeded from a past run's template and goal (§13.8).
  final ValueChanged<TeamRunInfo> onRunAgain;
  final ValueChanged<String> onOpenChat;
  final String initialTab;

  @override
  ConsumerState<AgentTeamsScreen> createState() => _AgentTeamsScreenState();
}

class _AgentTeamsScreenState extends ConsumerState<AgentTeamsScreen> {
  static const _tabs = ['agents', 'templates', 'runs'];
  late String _tab = _tabs.contains(widget.initialTab)
      ? widget.initialTab
      : 'agents';

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    String projectName(String id) =>
        widget.projects
            .where((project) => project.id == id)
            .firstOrNull
            ?.name ??
        i18n.t('teams:project');

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              i18n.t('teams:libraryTitle'),
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            Text(
              i18n.t('teams:libraryHint'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ],
        ),
      ),
      body: !widget.enabled
          ? Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  i18n.t('teams:featureUnavailable'),
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
            )
          : Column(
              children: [
                SectionTabs(
                  labels: {
                    for (final tab in _tabs)
                      tab: i18n.t('teams:libraryTabs.$tab'),
                  },
                  value: _tab,
                  onChanged: (tab) => setState(() => _tab = tab),
                ),
                Expanded(
                  child: RefreshIndicator(
                    color: t.accent,
                    onRefresh: () async {
                      ref.invalidate(teamCatalogProvider);
                      ref.invalidate(teamTemplatesProvider);
                      setState(() {});
                    },
                    child: ListView(
                      key: ValueKey(_tab),
                      padding: const EdgeInsets.fromLTRB(16, 14, 16, 24),
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        if (_tab == 'runs')
                          RunHistory(
                            scope: widget.scope,
                            projects: widget.projects,
                            projectName: projectName,
                            onOpen: (run) =>
                                widget.onOpenChat(run.rootSessionId),
                            onRunAgain: widget.onRunAgain,
                          )
                        else ...[
                          DefinitionList(
                            scope: widget.scope,
                            kind: _tab == 'templates' ? 'team' : 'agent',
                            onRun: widget.onRunTemplate,
                            onOpenSource: widget.onOpenChat,
                          ),
                          Padding(
                            padding: const EdgeInsets.only(top: 12),
                            child: Text(
                              i18n.t('teams:manageOnWeb'),
                              style: TextStyle(
                                fontSize: FontSizes.xs,
                                color: t.n600,
                                height: 1.6,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
              ],
            ),
    );
  }
}
