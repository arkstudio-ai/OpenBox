import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/project.dart';
import '../../../shared/models/session.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/widgets/brand_mark.dart';
import '../../inbox/api/inbox_api.dart';
import '../../onboarding/widgets/coach_mark.dart';
import '../state/workspace_store.dart';
import 'session_row.dart';
import 'user_row.dart';
import 'workspace_switcher.dart';

/// Left drawer: the mobile re-flow of the web sidebar (`Sidebar.tsx` +
/// `ProjectTree.tsx`) — brand, new chat, search, project-grouped sessions,
/// user row.
class SessionDrawer extends ConsumerStatefulWidget {
  const SessionDrawer({
    super.key,
    this.activeSessionId,
    this.assistantUnread = 0,
  });

  final String? activeSessionId;
  final int assistantUnread;

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

  /// The page under the drawer, to show where the person is; "" outside a
  /// router (a widget test).
  static String _currentPath(BuildContext context) {
    try {
      return GoRouter.maybeOf(
            context,
          )?.routerDelegate.currentConfiguration.uri.path ??
          '';
    } catch (_) {
      return '';
    }
  }

  void _open(String path) {
    Navigator.pop(context);
    context.push(path);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final workspace = ref.watch(workspaceProvider);
    final data = workspace.valueOrNull;
    final query = _search.text.trim().toLowerCase();
    final here = _currentPath(context);
    // A short phone (an SE) keeps the tiles to one row of icons, as the web
    // does under 760 px, so the projects still get the height.
    final compact = MediaQuery.sizeOf(context).height < 700;

    return Drawer(
      backgroundColor: t.rail,
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 14, 10, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Row(children: [BrandMark()]),
              const WorkspaceSwitcher(),
              const SizedBox(height: 8),
              // Where to go (web Sidebar): the assistant's own card, then the
              // centre pages as tiles, so the projects keep the height.
              _AssistantCard(
                unread: widget.assistantUnread,
                active: here == Paths.assistant,
                onTap: () {
                  Navigator.pop(context);
                  context.go(Paths.assistant);
                },
              ),
              const SizedBox(height: 6),
              _CentreTiles(
                compact: compact,
                tiles: [
                  // The cloud desktop leads: for most people it is the one
                  // work surface they use.
                  _Tile(
                    anchor: 'drawer.desktop',
                    icon: Icons.desktop_windows_outlined,
                    label: i18n.t('workspace:desktop'),
                    active: here.startsWith(Paths.desktop),
                    onTap: () => _open(Paths.desktop),
                  ),
                  // The badge is the cross-workspace unread total.
                  _Tile(
                    anchor: 'drawer.inbox',
                    key: const ValueKey('nav-inbox'),
                    icon: Icons.notifications_none,
                    label: i18n.t('workspace:inbox'),
                    badge:
                        ref.watch(inboxUnreadProvider).valueOrNull?.total ?? 0,
                    active: here.startsWith(Paths.inbox),
                    onTap: () => _open(Paths.inbox),
                  ),
                  // Memories, topics and files are one place.
                  _Tile(
                    key: const ValueKey('nav-knowledge'),
                    icon: Icons.menu_book_outlined,
                    label: i18n.t('workspace:wiki'),
                    active:
                        here.startsWith('/app/wiki') ||
                        here.startsWith(Paths.memory),
                    onTap: () => _open(Paths.wiki()),
                  ),
                  _Tile(
                    anchor: 'drawer.cron',
                    icon: Icons.schedule,
                    label: i18n.t('workspace:scheduledTasks'),
                    active: here.startsWith(Paths.cron),
                    onTap: () => _open(Paths.cron),
                  ),
                  // Opens on the project the tree is showing.
                  _Tile(
                    anchor: 'drawer.resources',
                    icon: Icons.layers_outlined,
                    label: i18n.t('workspace:resourceCenter'),
                    active: here.startsWith('/app/resources'),
                    onTap: () => _open(
                      Paths.resources(ref.read(selectedProjectProvider)),
                    ),
                  ),
                  _Tile(
                    anchor: 'drawer.skills',
                    icon: Icons.extension_outlined,
                    label: i18n.t('workspace:skillCenter'),
                    active: here.startsWith(Paths.skills),
                    onTap: () => _open(Paths.skills),
                  ),
                  _Tile(
                    anchor: 'drawer.authCenter',
                    icon: Icons.key_outlined,
                    label: i18n.t('workspace:authCenter'),
                    active: here.startsWith('/app/auth-center'),
                    onTap: () => _open(Paths.authCenter()),
                  ),
                  _Tile(
                    anchor: 'drawer.billing',
                    icon: Icons.toll_outlined,
                    label: i18n.t('workspace:billing'),
                    active: here.startsWith('/app/billing'),
                    onTap: () => _open(Paths.billing()),
                  ),
                ],
              ),
              Divider(color: t.hair, height: compact ? 13 : 17),
              // The work: start a conversation (the frequent action, in the
              // project the tree is on), make a project, find one.
              _ActionRow(
                key: const ValueKey('drawer-new-chat'),
                icon: Icons.add,
                label: i18n.t('workspace:newChat'),
                primary: true,
                compact: compact,
                onTap: () {
                  Navigator.pop(context);
                  context.go(Paths.app);
                },
              ),
              _ActionRow(
                key: const ValueKey('drawer-new-project'),
                icon: Icons.create_new_folder_outlined,
                label: i18n.t('workspace:newProject'),
                compact: compact,
                onTap: () => _promptProjectName(i18n),
              ),
              // Borderless search row: only the focus tint marks it (web).
              SizedBox(
                height: compact ? 36 : 40,
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
                            fontSize: FontSizes.base,
                            color: t.ink,
                          ),
                          decoration: InputDecoration(
                            hintText: i18n.t('workspace:search'),
                            hintStyle: TextStyle(
                              color: t.n600,
                              fontSize: FontSizes.base,
                            ),
                            isDense: true,
                            border: InputBorder.none,
                            contentPadding: EdgeInsets.zero,
                          ),
                        ),
                      ),
                      if (_search.text.isNotEmpty)
                        IconButton(
                          tooltip: i18n.t('workspace:searchClear'),
                          onPressed: () => setState(_search.clear),
                          visualDensity: VisualDensity.compact,
                          constraints: const BoxConstraints.tightFor(
                            width: 28,
                            height: 28,
                          ),
                          padding: EdgeInsets.zero,
                          icon: Icon(Icons.close, size: 14, color: t.n700),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 4),
              Expanded(
                child: CoachAnchor(
                  name: 'drawer.projects',
                  child: data == null
                      ? const Center(
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : _buildGroups(i18n, t, data, query),
                ),
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
    I18nState i18n,
    BossipTokens t,
    WorkspaceData data,
    String query,
  ) {
    final conversations = data.sessions
        .where((s) => s.kind != 'assistant')
        .toList();
    final sessions = query.isEmpty
        ? conversations
        : conversations
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
              _filterToggle(
                i18n,
                t,
                project?.id ?? '__loose',
                group.where((s) => s.isCron).length,
              ),
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
                      for (final session in _visibleSessions(
                        project,
                        group,
                        searching,
                      ))
                        SessionRow(
                          session: session,
                          active: session.id == widget.activeSessionId,
                          onOpen: () {
                            ref.read(selectedProjectProvider.notifier).state =
                                project?.id;
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
    Project? project,
    List<Session> group,
    bool searching,
  ) {
    if (searching) return group;
    final mode = _sessionFilter[project?.id ?? '__loose'] ?? 'chats';
    return group.where((s) => mode == 'cron' ? s.isCron : !s.isCron).toList();
  }

  /// [会话 | 定时运行 N] segmented toggle under a project header
  /// (web `FilterToggle`).
  Widget _filterToggle(
    I18nState i18n,
    BossipTokens t,
    String groupId,
    int cronCount,
  ) {
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
          segment(
            'chats',
            i18n.t('workspace:filter.chats'),
            Icons.chat_bubble_outline,
          ),
          const SizedBox(width: 2),
          segment('cron', i18n.t('workspace:filter.cron'), Icons.schedule),
        ],
      ),
    );
  }

  Widget _groupHeader(
    I18nState i18n,
    BossipTokens t,
    Project? project,
    bool searching,
  ) {
    final id = project?.id ?? '__loose';
    final name = project?.name ?? i18n.t('workspace:unsorted');
    final collapsed = _collapsed.contains(id);
    final selected =
        project?.id != null &&
        ref.watch(selectedProjectProvider) == project?.id;
    return InkWell(
      onTap: searching
          ? null
          : () => setState(() {
              if (!_collapsed.remove(id)) _collapsed.add(id);
            }),
      onLongPress: project == null
          ? null
          : () => _showProjectActions(i18n, project),
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
            Expanded(
              child: Row(
                children: [
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
                      decoration: BoxDecoration(
                        color: t.accent,
                        shape: BoxShape.circle,
                      ),
                    ),
                  ],
                ],
              ),
            ),
            IconButton(
              key: ValueKey('new-chat-$id'),
              tooltip: i18n.t('workspace:newChatIn'),
              onPressed: () {
                ref.read(selectedProjectProvider.notifier).state = project?.id;
                Navigator.pop(context);
                context.go(Paths.app);
              },
              visualDensity: VisualDensity.compact,
              constraints: const BoxConstraints.tightFor(width: 28, height: 28),
              padding: EdgeInsets.zero,
              icon: Icon(Icons.add, size: 16, color: t.n700),
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
              leading: Icon(
                Icons.check_circle_outline,
                size: 20,
                color: t.n700,
              ),
              title: Text(
                i18n.t('workspace:newChatIn'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              onTap: () {
                Navigator.pop(sheetContext);
                ref.read(selectedProjectProvider.notifier).state = project.id;
                Navigator.pop(context);
                context.go(Paths.app);
              },
            ),
            ListTile(
              leading: Icon(Icons.delete_outline, size: 20, color: t.danger),
              title: Text(
                i18n.t('workspace:deleteProject'),
                style: TextStyle(fontSize: FontSizes.base, color: t.danger),
              ),
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
    final title = await _promptText(
      i18n.t('workspace:rename'),
      initial: session.title,
    );
    if (title != null && title.isNotEmpty) {
      await ref
          .read(workspaceProvider.notifier)
          .renameSession(session.id, title);
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
        content: Text(
          body,
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
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

/// An unread count, hidden at zero, capped at 99+.
class _Badge extends StatelessWidget {
  const _Badge({super.key, required this.count});
  final int count;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: t.accent,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        count > 99 ? '99+' : '$count',
        style: TextStyle(
          fontSize: FontSizes.xs2,
          fontWeight: FontWeight.w600,
          color: t.bg,
        ),
      ),
    );
  }
}

/// The personal assistant's card at the top (web `AssistantEntry`): where work
/// is handed over and followed up, so it stands apart from the centre pages —
/// a raised card in the drawer's own neutrals, its name as the person set it.
class _AssistantCard extends ConsumerWidget {
  const _AssistantCard({
    required this.unread,
    required this.active,
    required this.onTap,
  });

  final int unread;
  final bool active;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final name = assistantLabel(ref);
    return CoachAnchor(
      name: 'drawer.assistant',
      child: Material(
        color: active ? t.n200 : t.card,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: active ? t.n300 : t.hair),
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          key: const ValueKey('nav-assistant'),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 8, 10, 8),
            child: Row(
              children: [
                Container(
                  width: 34,
                  height: 34,
                  decoration: BoxDecoration(
                    color: t.n200,
                    shape: BoxShape.circle,
                  ),
                  child: Icon(Icons.auto_awesome, size: 17, color: t.n800),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.base,
                          fontWeight: FontWeight.w500,
                          color: t.ink,
                        ),
                      ),
                      Text(
                        i18n.t('workspace:assistantTagline'),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                    ],
                  ),
                ),
                if (unread > 0)
                  _Badge(key: ValueKey('nav-badge-$name'), count: unread)
                else
                  Icon(Icons.chevron_right, size: 18, color: t.n500),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// One centre page as a tile (web `NavTile`): an icon over a short label.
class _Tile {
  const _Tile({
    this.key,
    this.anchor,
    required this.icon,
    required this.label,
    required this.onTap,
    this.badge = 0,
    this.active = false,
  });

  final Key? key;

  /// Coach-mark anchor name (onboarding sidebar walkthrough).
  final String? anchor;
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final int badge;

  /// The page under the drawer is this one.
  final bool active;
}

/// The centre pages: four to a row, everyday ones first; on a short screen
/// one row of icons, the label kept for screen readers and the tooltip.
class _CentreTiles extends StatelessWidget {
  const _CentreTiles({required this.tiles, required this.compact});

  final List<_Tile> tiles;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final columns = compact ? 8 : 4;
    return Column(
      children: [
        for (var start = 0; start < tiles.length; start += columns)
          Row(
            children: [
              for (final tile in tiles.skip(start).take(columns))
                Expanded(
                  child: _TileButton(tile: tile, compact: compact),
                ),
            ],
          ),
      ],
    );
  }
}

class _TileButton extends StatelessWidget {
  const _TileButton({required this.tile, required this.compact});

  final _Tile tile;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final color = tile.active ? t.ink : t.n800;
    final button = Padding(
      padding: const EdgeInsets.all(1),
      child: Tooltip(
        message: tile.label,
        child: Semantics(
          button: true,
          selected: tile.active,
          label: tile.label,
          excludeSemantics: true,
          child: Material(
            color: tile.active ? t.n200 : Colors.transparent,
            borderRadius: BorderRadius.circular(Radii.md),
            child: InkWell(
              key: tile.key,
              borderRadius: BorderRadius.circular(Radii.md),
              onTap: tile.onTap,
              child: SizedBox(
                height: compact ? 40 : 56,
                child: Stack(
                  children: [
                    Center(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(tile.icon, size: 19, color: color),
                          if (!compact) ...[
                            const SizedBox(height: 4),
                            Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 2,
                              ),
                              child: Text(
                                tile.label,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  fontSize: FontSizes.xs2,
                                  height: 1.2,
                                  fontWeight: tile.active
                                      ? FontWeight.w500
                                      : FontWeight.w400,
                                  color: color,
                                ),
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                    if (tile.badge > 0)
                      Positioned(
                        top: 3,
                        right: 3,
                        child: _Badge(
                          key: ValueKey('nav-badge-${tile.label}'),
                          count: tile.badge,
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
    return tile.anchor == null
        ? button
        : CoachAnchor(name: tile.anchor!, child: button);
  }
}

/// A row of the work part (web Sidebar): icon column + label; the frequent
/// action wears a round neutral chip.
class _ActionRow extends StatelessWidget {
  const _ActionRow({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
    this.primary = false,
    this.compact = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool primary;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return InkWell(
      borderRadius: BorderRadius.circular(Radii.full),
      onTap: onTap,
      child: SizedBox(
        height: compact ? 36 : 40,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          child: Row(
            children: [
              primary
                  ? Container(
                      width: 28,
                      height: 28,
                      decoration: BoxDecoration(
                        color: t.n200,
                        shape: BoxShape.circle,
                      ),
                      child: Icon(icon, size: 15, color: t.ink),
                    )
                  : SizedBox(
                      width: 28,
                      child: Center(child: Icon(icon, size: 17, color: t.ink)),
                    ),
              const SizedBox(width: 10),
              Flexible(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    fontWeight: primary ? FontWeight.w500 : FontWeight.w400,
                    color: t.ink,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
