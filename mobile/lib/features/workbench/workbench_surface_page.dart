import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/containers_api.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../cron/widgets/cron_panel_tab.dart';
import 'state/workbench_providers.dart';
import 'widgets/browser_tab.dart';
import 'widgets/desktop_tab.dart';
import 'widgets/files_tab.dart';
import 'widgets/review_tab.dart';
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
  });

  final String sessionId;

  /// One of [workbenchKinds].
  final String kind;

  @override
  ConsumerState<WorkbenchSurfacePage> createState() =>
      _WorkbenchSurfacePageState();
}

class _WorkbenchSurfacePageState extends ConsumerState<WorkbenchSurfacePage> {
  bool _creatingSandbox = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
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
        'terminal' => _withContainer(
          (containerId) => TerminalTab(
            containerId: containerId,
            sessionId: widget.sessionId,
            projectId: ref
                .watch(sessionProjectIdProvider(widget.sessionId))
                .valueOrNull,
          ),
        ),
        'browser' => _withContainer((_) => const BrowserTab()),
        'files' => _withContainer(
          (containerId) =>
              FilesTab(sessionId: widget.sessionId, containerId: containerId),
        ),
        'desktop' => const DesktopTab(),
        'cron' => CronPanelTab(
          projectId: ref
              .watch(sessionProjectIdProvider(widget.sessionId))
              .valueOrNull,
        ),
        _ => ReviewTab(sessionId: widget.sessionId),
      },
    );
  }

  /// Terminal/browser/files need a running sandbox (web `TerminalTab` empty
  /// state): say so, and offer to start one.
  Widget _withContainer(Widget Function(String containerId) builder) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final container = ref.watch(runningContainerProvider);
    return container.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(
          i18n.t('workbench:sandbox.none'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      ),
      data: (info) {
        if (info == null) {
          return Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  i18n.t('workbench:sandbox.none'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.n700),
                ),
                const SizedBox(height: 12),
                FilledButton(
                  onPressed: _creatingSandbox ? null : _createSandbox,
                  style: FilledButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(
                    i18n.t('workbench:sandbox.create'),
                    style: const TextStyle(fontSize: FontSizes.sm),
                  ),
                ),
              ],
            ),
          );
        }
        return builder(info.id);
      },
    );
  }

  Future<void> _createSandbox() async {
    setState(() => _creatingSandbox = true);
    try {
      await ref.read(containersApiProvider).create();
      ref.invalidate(runningContainerProvider);
    } finally {
      if (mounted) setState(() => _creatingSandbox = false);
    }
  }
}
