import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../cron/widgets/cron_panel_tab.dart';
import 'state/workbench_providers.dart';
import 'widgets/browser_tab.dart';
import 'widgets/desktop_tab.dart';
import 'widgets/files_tab.dart';
import 'widgets/review_tab.dart';
import 'widgets/subscription_sandbox_surface.dart';
import 'widgets/terminal_tab.dart';

/// One workbench surface, full screen (web: one tab of `WorkbenchPanel`).
///
/// A real pushed route rather than a state flag on the menu — that is what
/// makes the iOS edge-swipe return to the menu. A `PopScope` guard would only
/// *disable* the gesture: with `canPop: false` iOS never starts the
/// interactive pop, so the callback that would have gone back never fires.
class WorkbenchSurfacePage extends ConsumerStatefulWidget {
  const WorkbenchSurfacePage({
    super.key,
    required this.sessionId,
    required this.kind,
    this.control = false,
  });

  final String sessionId;

  /// One of [workbenchKinds].
  final String kind;

  /// Desktop only: switch input control on once connected.
  final bool control;

  @override
  ConsumerState<WorkbenchSurfacePage> createState() =>
      _WorkbenchSurfacePageState();
}

class _WorkbenchSurfacePageState extends ConsumerState<WorkbenchSurfacePage> {
  /// The cloud desktop asks for the whole screen in landscape. Dropping the
  /// app bar here — rather than pushing another route — keeps the WebView in
  /// the same place in the tree, so the stream survives the transition.
  bool _immersive = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: _immersive ? Colors.black : t.bg,
      appBar: _immersive
          ? null
          : AppBar(
              title: Text(
                i18n.t('workbench:tabs.${widget.kind}'),
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
            ),
      body: switch (widget.kind) {
        'terminal' => SubscriptionSandboxSurface(
          builder: (containerId) => TerminalTab(containerId: containerId),
        ),
        'browser' => SubscriptionSandboxSurface(
          builder: (_) => const BrowserTab(),
        ),
        'files' => SubscriptionSandboxSurface(
          builder: (containerId) =>
              FilesTab(sessionId: widget.sessionId, containerId: containerId),
        ),
        'desktop' => DesktopTab(
          onImmersive: (on) => setState(() => _immersive = on),
          autoControl: widget.control,
        ),
        'cron' => CronPanelTab(
          projectId: ref
              .watch(sessionProjectIdProvider(widget.sessionId))
              .valueOrNull,
        ),
        _ => ReviewTab(sessionId: widget.sessionId),
      },
    );
  }
}
