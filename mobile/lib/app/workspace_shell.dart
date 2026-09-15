import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/cron/widgets/cron_status_pill.dart';
import '../features/onboarding/state/onboarding_store.dart';
import '../features/onboarding/widgets/coach_mark.dart';
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
  StreamSubscription<AppEvent>? _workbenchSub;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(wsClientProvider).connect();
      if (widget.sessionId != null) unawaited(_panelEntryGuide());
      // Cross-feature: chat "审阅 →" emits workbench.open (web D.6).
      _workbenchSub = ref.read(appEventBusProvider).on('workbench.open').listen(
        (event) {
          final sessionId = event.payload['sessionId'];
          final kind = event.payload['kind'];
          if (sessionId is String && mounted) {
            context.push(
              Paths.workbench(
                sessionId,
                tab: kind is String && kind.isNotEmpty ? kind : 'review',
                control: kind == 'desktop' && event.payload['control'] == true,
              ),
            );
          } else if (kind == 'desktop' && mounted) {
            context.push(Paths.desktop);
          }
        },
      );
    });
  }

  @override
  void dispose() {
    unawaited(_workbenchSub?.cancel());
    super.dispose();
  }

  static const _drawerMarks = [
    ('drawer.projects', 'projects', 14.0),
    ('drawer.resources', 'resources', 999.0),
    ('drawer.inbox', 'inbox', 999.0),
    ('drawer.authCenter', 'authCenter', 999.0),
    ('drawer.skills', 'skills', 999.0),
    ('drawer.cron', 'cron', 999.0),
    ('drawer.billing', 'billing', 999.0),
    ('drawer.desktop', 'desktop', 999.0),
  ];

  /// L3 sidebar walkthrough on the account's first drawer open.
  Future<void> _drawerGuide() async {
    final onboarding = ref.read(onboardingProvider.notifier);
    await onboarding.whenLoaded();
    if (!mounted || !onboarding.shouldShow(Guides.drawer)) return;
    final i18n = ref.read(i18nProvider);
    await showCoachMarks(
      context,
      ref,
      guideKey: Guides.drawer,
      steps: [
        for (final (anchor, key, radius) in _drawerMarks)
          CoachStep(
            anchor: anchor,
            title: i18n.t('onboarding:marks.drawer.$key.title'),
            body: i18n.t('onboarding:marks.drawer.$key.body'),
            radius: radius,
          ),
      ],
    );
  }

  /// L3 panel entry bubble on the first conversation screen.
  Future<void> _panelEntryGuide() async {
    final onboarding = ref.read(onboardingProvider.notifier);
    await onboarding.whenLoaded();
    if (!mounted || !onboarding.shouldShow(Guides.panelEntry)) return;
    await ref.read(guideQueueProvider.notifier).whenIdle();
    if (!mounted) return;
    final i18n = ref.read(i18nProvider);
    await showCoachMarks(
      context,
      ref,
      guideKey: Guides.panelEntry,
      steps: [
        CoachStep(
          anchor: 'panel.entry',
          title: i18n.t('onboarding:marks.panelEntry.title'),
          body: i18n.t('onboarding:marks.panelEntry.body'),
        ),
      ],
    );
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
              onOpen: () =>
                  context.push(Paths.workbench(widget.sessionId!, tab: 'cron')),
            ),
          if (widget.sessionId != null)
            CoachAnchor(
              name: 'panel.entry',
              child: IconButton(
                icon: Icon(
                  Icons.space_dashboard_outlined,
                  size: 20,
                  color: t.n700,
                ),
                tooltip: i18n.t('workspace:openPanel'),
                onPressed: () =>
                    context.push(Paths.workbench(widget.sessionId!)),
              ),
            ),
          const SizedBox(width: 4),
        ],
      ),
      drawer: SessionDrawer(activeSessionId: widget.sessionId),
      onDrawerChanged: (isOpen) {
        // The drawer overlays the entire screen. A chat composer keyboard
        // must not cover its account/settings actions (including swipe-open).
        if (isOpen) FocusManager.instance.primaryFocus?.unfocus();
        if (isOpen) {
          WidgetsBinding.instance.addPostFrameCallback(
            (_) => unawaited(_drawerGuide()),
          );
        }
      },
      body: widget.child,
    );
  }
}
