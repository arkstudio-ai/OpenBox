import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/project.dart';
import '../../../shared/models/session.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/widgets/brand_mark.dart';
import '../state/workspace_store.dart';
import 'session_row.dart';
import 'user_row.dart';

/// Left drawer: the mobile re-flow of the web sidebar (`Sidebar.tsx` +
/// `ProjectTree.tsx`) — brand, new chat, search, project-grouped sessions,
/// user row.
class SessionDrawer extends ConsumerStatefulWidget {
  const SessionDrawer({super.key, this.activeSessionId});

  final String? activeSessionId;

  @override
  ConsumerState<SessionDrawer> createState() => _SessionDrawerState();
}

class _SessionDrawerState extends ConsumerState<SessionDrawer> {
  final _search = TextEditingController();
  final Set<String> _collapsed = {};

  /// Per-project sidebar filter: plain conversations (default) or cron runs
  /// (web `useWorkspaceUi.sessionFilter`).
  final Map<String, String> _sessionFilter = {};

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final workspace = ref.watch(workspaceProvider);
    final data = workspace.valueOrNull;
    final query = _search.text.trim().toLowerCase();

    return Drawer(
      backgroundColor: t.rail,
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 14, 10, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const BrandMark(),
                  IconButton(
                    icon: Icon(Icons.create_new_folder_outlined,
                        size: 19, color: t.n700),
                    tooltip: i18n.t('workspace:newProject'),
                    onPressed: () => _promptProjectName(i18n),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              // DEEIX-style nav rows (web Sidebar): left-aligned, icon
              // column; the primary action wears a round tinted icon chip
              // instead of a filled pill.
              InkWell(
                borderRadius: BorderRadius.circular(Radii.full),
                onTap: () {
                  Navigator.pop(context);
                  context.go(Paths.app);
                },
                child: SizedBox(
                  height: 40,
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 6),
                    child: Row(
                      children: [
                        Container(
                          width: 28,
                          height: 28,
                          decoration: BoxDecoration(
                              color: t.n200, shape: BoxShape.circle),
                          child: Icon(Icons.add, size: 15, color: t.ink),
                        ),
                        const SizedBox(width: 10),
                        Text(
                          i18n.t('workspace:newChat'),
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              // Borderless search row: only the focus tint marks it (web).
              SizedBox(
                height: 40,
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 6),
                  child: Row(
                    children: [
                      SizedBox(
                        width: 28,
                        child: Center(
                          child: Icon(Icons.search, size: 16, color: t.ink),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: TextField(
                          controller: _search,
                          onChanged: (_) => setState(() {}),
                          style: TextStyle(
                              fontSize: FontSizes.base, color: t.ink),
                          decoration: InputDecoration(
                            hintText: i18n.t('workspace:search'),
                            hintStyle: TextStyle(
                                color: t.n600, fontSize: FontSizes.base),
                            isDense: true,
                            border: InputBorder.none,
                            contentPadding: EdgeInsets.zero,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 10),
              // Resource centre, above the scheduled tasks like the web
              // sidebar; opens on the project the tree is showing.
              _NavRow(
                icon: Icons.layers_outlined,
                label: i18n.t('workspace:resourceCenter'),
                onTap: () {
                  Navigator.pop(context);
                  context.push(
                    Paths.resources(ref.read(selectedProjectProvider)),
                  );
                },
              ),
              // Scheduled-tasks entry, same spot as the web sidebar.
              _NavRow(
                icon: Icons.schedule,
                label: i18n.t('workspace:scheduledTasks'),
                onTap: () {
                  Navigator.pop(context);
                  context.push(Paths.cron);
                },
              ),
              const SizedBox(height: 4),
              Expanded(
                child: data == null
                    ? const Center(child: CircularProgressIndicator(strokeWidth: 2))
                    : _buildGroups(i18n, t, data, query),
              ),
              Divider(color: t.hair, height: 16),
              UserRow(
                sessionCount: data?.sessions.length ?? 0,
                onSignOut: () async {
                  await ref.read(authProvider.notifier).signOut();
                  if (context.mounted) context.go(Paths.landing);
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildGroups(
      I18nState i18n, BossipTokens t, WorkspaceData data, String query) {
    final sessions = query.isEmpty
        ? data.sessions
        : data.sessions
            .where((s) => s.title.toLowerCase().contains(query))
            .toList();
    final grouped = <(Project?, List<Session>)>[];
    final known = <String>{for (final p in data.projects) p.id};
    for (final project in data.projects) {
      grouped.add((
        project,
        sessions.where((s) => s.projectId == project.id).toList(),
      ));
    }
    final loose = sessions.where((s) => !known.contains(s.projectId)).toList();
    if (loose.isNotEmpty) grouped.add((null, loose));

    final searching = query.isNotEmpty;
    return ListView(
      padding: EdgeInsets.zero,
      children: [
        for (final (project, group) in grouped) ...[
          _groupHeader(i18n, t, project, searching),
          if (searching || !_collapsed.contains(project?.id ?? '__loose')) ...[
            // While searching, matches from both kinds show (web parity).
            if (!searching && group.any((s) => s.isCron))
              _filterToggle(i18n, t, project?.id ?? '__loose',
                  group.where((s) => s.isCron).length),
            _visibleSessions(project, group, searching).isEmpty
                ? Padding(
                    padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
                    child: Text(
                      i18n.t('workspace:noChats'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                  )
                : Column(
                    children: [
                      for (final session
                          in _visibleSessions(project, group, searching))
                        SessionRow(
                          session: session,
                          active: session.id == widget.activeSessionId,
                          onOpen: () {
                            ref
                                .read(selectedProjectProvider.notifier)
                                .state = project?.id;
                            Navigator.pop(context);
                            context.go(Paths.chat(session.id));
                          },
                          onDelete: () => _confirmDeleteSession(i18n, session),
                          onRename: () => _promptRenameSession(i18n, session),
                        ),
                    ],
                  ),
          ],
          const SizedBox(height: 4),
        ],
      ],
    );
  }

  List<Session> _visibleSessions(
      Project? project, List<Session> group, bool searching) {
    if (searching) return group;
    final mode = _sessionFilter[project?.id ?? '__loose'] ?? 'chats';
    return group
        .where((s) => mode == 'cron' ? s.isCron : !s.isCron)
        .toList();
  }

  /// [会话 | 定时运行 N] segmented toggle under a project header
  /// (web `FilterToggle`).
  Widget _filterToggle(
      I18nState i18n, BossipTokens t, String groupId, int cronCount) {
    final mode = _sessionFilter[groupId] ?? 'chats';
    // Icon-only segments (web FilterToggle) — the label lives in semantics;
    // the cron segment carries its count.
    Widget segment(String value, String label, IconData icon) {
      final active = mode == value;
      return Semantics(
        label: label,
        button: true,
        child: InkWell(
          borderRadius: BorderRadius.circular(Radii.full),
          onTap: () => setState(() => _sessionFilter[groupId] = value),
          child: Container(
            height: 24,
            padding: const EdgeInsets.symmetric(horizontal: 8),
            decoration: BoxDecoration(
              color: active ? t.n200 : Colors.transparent,
              borderRadius: BorderRadius.circular(Radii.full),
            ),
            child: Row(
              children: [
                Icon(icon, size: 12, color: active ? t.ink : t.n600),
                if (value == 'cron' && cronCount > 0) ...[
                  const SizedBox(width: 4),
                  Text(
                    '$cronCount',
                    style: TextStyle(
                      fontSize: FontSizes.xs2,
                      color: active ? t.ink : t.n600,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      );
    }

    return Padding(
      padding: const EdgeInsets.only(left: 28, bottom: 2),
      child: Row(
        children: [
          segment('chats', i18n.t('workspace:filter.chats'),
              Icons.chat_bubble_outline),
          const SizedBox(width: 2),
          segment(
              'cron', i18n.t('workspace:filter.cron'), Icons.schedule),
        ],
      ),
    );
  }

  Widget _groupHeader(
      I18nState i18n, BossipTokens t, Project? project, bool searching) {
    final id = project?.id ?? '__loose';
    final name = project?.name ?? i18n.t('workspace:unsorted');
    final collapsed = _collapsed.contains(id);
    final selected = project?.id != null &&
        ref.watch(selectedProjectProvider) == project?.id;
    return InkWell(
      onTap: searching
          ? null
          : () => setState(() {
                if (!_collapsed.remove(id)) _collapsed.add(id);
              }),
      onLongPress:
          project == null ? null : () => _showProjectActions(i18n, project),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 7),
        child: Row(
          children: [
            AnimatedRotation(
              turns: collapsed && !searching ? 0 : 0.25,
              duration: const Duration(milliseconds: 150),
              child: Icon(Icons.chevron_right, size: 14, color: t.n600),
            ),
            const SizedBox(width: 6),
            Flexible(
              child: Text(
                name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.md,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
            ),
            if (selected) ...[
              const SizedBox(width: 6),
              Container(
                width: 6,
                height: 6,
                decoration:
                    BoxDecoration(color: t.accent, shape: BoxShape.circle),
              ),
            ],
            const Spacer(),
          ],
        ),
      ),
    );
  }

  void _showProjectActions(I18nState i18n, Project project) {
    final t = context.tokens;
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(Icons.check_circle_outline, size: 20, color: t.n700),
              title: Text(i18n.t('workspace:newChatIn'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              onTap: () {
                Navigator.pop(sheetContext);
                ref.read(selectedProjectProvider.notifier).state = project.id;
                Navigator.pop(context);
                context.go(Paths.app);
              },
            ),
            ListTile(
              leading: Icon(Icons.delete_outline, size: 20, color: t.danger),
              title: Text(i18n.t('workspace:deleteProject'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.danger)),
              onTap: () {
                Navigator.pop(sheetContext);
                _confirmDeleteProject(i18n, project);
              },
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _promptProjectName(I18nState i18n) async {
    final name = await _promptText(i18n.t('workspace:projectName'));
    if (name != null && name.isNotEmpty) {
      await ref.read(workspaceProvider.notifier).createProject(name);
    }
  }

  Future<void> _promptRenameSession(I18nState i18n, Session session) async {
    final title =
        await _promptText(i18n.t('workspace:rename'), initial: session.title);
    if (title != null && title.isNotEmpty) {
      await ref.read(workspaceProvider.notifier).renameSession(session.id, title);
    }
  }

  Future<String?> _promptText(String title, {String? initial}) {
    final controller = TextEditingController(text: initial);
    return showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title, style: const TextStyle(fontSize: FontSizes.lg)),
        content: TextField(controller: controller, autofocus: true),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(ref.read(i18nProvider).t('common:action.cancel')),
          ),
          TextButton(
            onPressed: () =>
                Navigator.pop(dialogContext, controller.text.trim()),
            child: Text(ref.read(i18nProvider).t('common:action.confirm')),
          ),
        ],
      ),
    );
  }

  Future<void> _confirmDeleteSession(I18nState i18n, Session session) async {
    final confirmed = await _confirm(
      i18n.t('workspace:delChatTitle'),
      i18n.t('workspace:delChatBody'),
    );
    if (confirmed) {
      await ref.read(workspaceProvider.notifier).deleteSession(session.id);
      if (session.id == widget.activeSessionId && mounted) {
        context.go(Paths.app);
      }
    }
  }

  Future<void> _confirmDeleteProject(I18nState i18n, Project project) async {
    final confirmed = await _confirm(
      i18n.t('workspace:delTitle', vars: {'name': project.name}),
      i18n.t('workspace:delBody'),
    );
    if (confirmed) {
      await ref.read(workspaceProvider.notifier).deleteProject(project.id);
    }
  }

  Future<bool> _confirm(String title, String body) async {
    final t = context.tokens;
    final i18n = ref.read(i18nProvider);
    final result = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title, style: const TextStyle(fontSize: FontSizes.lg)),
        content: Text(body,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n700)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(i18n.t('common:action.cancel')),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(
              i18n.t('common:action.delete'),
              style: TextStyle(color: t.danger),
            ),
          ),
        ],
      ),
    );
    return result ?? false;
  }
}

/// A drawer nav row: icon column + label, the shape the web sidebar uses for
/// everything above the project tree.
class _NavRow extends StatelessWidget {
  const _NavRow({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return InkWell(
      borderRadius: BorderRadius.circular(Radii.full),
      onTap: onTap,
      child: SizedBox(
        height: 40,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          child: Row(
            children: [
              SizedBox(
                width: 28,
                child: Center(child: Icon(icon, size: 16, color: t.ink)),
              ),
              const SizedBox(width: 10),
              Text(
                label,
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
