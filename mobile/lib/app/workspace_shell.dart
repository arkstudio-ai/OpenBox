import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/cron/widgets/cron_status_pill.dart';
import '../features/workspace/state/workspace_store.dart';
import '../features/workspace/widgets/session_drawer.dart';
import '../shared/appearance/tokens.dart';
import '../shared/appearance/type_scale.dart';
import '../shared/events/bus.dart';
import '../shared/i18n/i18n.dart';
import '../shared/router/paths.dart';
import '../shared/ws/ws_client.dart';

/// The workspace shell (web `WorkspaceLayout` + `Topbar`), mobile re-flow:
/// the sidebar becomes a drawer, the right panel a routed screen. Hosts the
/// app-global WS connection while signed in.
class WorkspaceShell extends ConsumerStatefulWidget {
  const WorkspaceShell({super.key, this.sessionId, required this.child});

  final String? sessionId;
  final Widget child;

  @override
  ConsumerState<WorkspaceShell> createState() => _WorkspaceShellState();
}

class _WorkspaceShellState extends ConsumerState<WorkspaceShell> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(wsClientProvider).connect();
      // Cross-feature: chat "审阅 →" emits workbench.open (web D.6).
      ref.read(appEventBusProvider).on('workbench.open').listen((event) {
        final sessionId = event.payload['sessionId'];
        if (sessionId is String && mounted) {
          context.push(Paths.workbench(sessionId));
        }
      });
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final workspace = ref.watch(workspaceProvider).valueOrNull;
    final session = widget.sessionId == null
        ? null
        : workspace?.sessionById(widget.sessionId!);
    final project = workspace?.projectById(session?.projectId);

    final title = widget.sessionId == null
        ? 'bossip'
        : (session?.title.isNotEmpty ?? false)
            ? session!.title
            : i18n.t('workspace:untitledChat');
    final subtitle = widget.sessionId == null
        ? null
        : project?.name ?? i18n.t('workspace:unsorted');

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              title,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            if (subtitle != null)
              Text(
                subtitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
          ],
        ),
        actions: [
          // Cron reminder pill before the panel toggle (web Topbar).
          if (widget.sessionId != null)
            CronStatusPill(
              projectId: session?.projectId,
              onOpen: () => context
                  .push(Paths.workbench(widget.sessionId!, tab: 'cron')),
            ),
          if (widget.sessionId != null)
            IconButton(
              icon: Icon(Icons.space_dashboard_outlined,
                  size: 20, color: t.n700),
              tooltip: i18n.t('workspace:openPanel'),
              onPressed: () =>
                  context.push(Paths.workbench(widget.sessionId!)),
            ),
          const SizedBox(width: 4),
        ],
      ),
      drawer: SessionDrawer(activeSessionId: widget.sessionId),
      body: widget.child,
    );
  }
}
