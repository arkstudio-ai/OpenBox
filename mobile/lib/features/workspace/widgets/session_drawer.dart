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
              const SizedBox(height: 10),
              SizedBox(
                height: 40,
                child: FilledButton.icon(
                  onPressed: () {
                    Navigator.pop(context);
                    context.go(Paths.app);
                  },
                  style: FilledButton.styleFrom(
                    backgroundColor: t.a200,
                    foregroundColor: t.ink,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  icon: const Icon(Icons.add, size: 18),
                  label: Text(
                    i18n.t('workspace:newChat'),
                    style: TextStyle(
                      fontSize: FontSizes.base,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _search,
                onChanged: (_) => setState(() {}),
                style: TextStyle(fontSize: FontSizes.md, color: t.ink),
                decoration: InputDecoration(
                  hintText: i18n.t('workspace:search'),
                  hintStyle: TextStyle(color: t.n500, fontSize: FontSizes.md),
                  prefixIcon: Icon(Icons.search, size: 18, color: t.n500),
                  isDense: true,
                  contentPadding: const EdgeInsets.symmetric(vertical: 8),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                    borderSide: BorderSide(color: t.hair),
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                    borderSide: BorderSide(color: t.n400),
                  ),
                ),
              ),
              const SizedBox(height: 8),
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
          if (searching || !_collapsed.contains(project?.id ?? '__loose'))
            group.isEmpty
                ? Padding(
                    padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
                    child: Text(
                      i18n.t('workspace:noChats'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                  )
                : Column(
                    children: [
                      for (final session in group)
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
          const SizedBox(height: 4),
        ],
      ],
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
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 7),
        child: Row(
          children: [
            AnimatedRotation(
              turns: collapsed && !searching ? -0.25 : 0,
              duration: const Duration(milliseconds: 150),
              child: Icon(Icons.expand_more, size: 15, color: t.n500),
            ),
            const SizedBox(width: 4),
            Expanded(
              child: Text(
                name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w600,
                  color: t.n600,
                  letterSpacing: 0.2,
                ),
              ),
            ),
            if (selected)
              Container(
                width: 6,
                height: 6,
                decoration:
                    BoxDecoration(color: t.a700, shape: BoxShape.circle),
              ),
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
