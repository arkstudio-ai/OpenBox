import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import 'widgets/private_browser_tab.dart';
import 'widgets/workbench_menu.dart';
import 'widgets/workbench_runtime_gate.dart';
import 'workbench_surface_page.dart';

/// The web right-panel (`WorkbenchPanel`) re-flowed as a route.
///
/// Web opens the panel on a **menu tab** and turns it into whichever surface
/// you pick; the tab strip is how you come back. A phone has no room for a
/// strip, so the same menu is this route and a pick *pushes* the surface —
/// which means the stock back arrow and the iOS edge-swipe both return here,
/// and one more back leaves the panel. Same six surfaces, same live hints.
class WorkbenchScreen extends StatelessWidget {
  const WorkbenchScreen({
    super.key,
    required this.sessionId,
    this.initialTab = menuTab,
    this.initialControl = false,
    this.privateOnly = false,
  });

  /// `initialTab` value meaning "stay on the menu".
  static const menuTab = 'menu';

  final String sessionId;

  /// A surface to open straight away — the cron pill and chat's "审阅 →" both
  /// deep-link one. It opens *on top of* the menu, so back still lands here.
  final String initialTab;

  /// With `initialTab == 'desktop'`: take input control as soon as the stream
  /// is up (a takeover card in the chat asked for it).
  final bool initialControl;
  final bool privateOnly;

  @override
  Widget build(BuildContext context) => WorkbenchRuntimeGate(
    sessionId: sessionId,
    privateOnly: privateOnly,
    privateBuilder: (scope) => PrivateBrowserPage(scope: scope),
    ordinaryBuilder: (_) => _OrdinaryWorkbenchScreen(
      sessionId: sessionId,
      initialTab: initialTab,
      initialControl: initialControl,
    ),
  );
}

class _OrdinaryWorkbenchScreen extends ConsumerStatefulWidget {
  const _OrdinaryWorkbenchScreen({
    required this.sessionId,
    required this.initialTab,
    required this.initialControl,
  });
  final String sessionId, initialTab;
  final bool initialControl;
  @override
  ConsumerState<_OrdinaryWorkbenchScreen> createState() =>
      _WorkbenchScreenState();
}

class _WorkbenchScreenState extends ConsumerState<_OrdinaryWorkbenchScreen> {
  @override
  void initState() {
    super.initState();
    if (widget.initialTab != WorkbenchScreen.menuTab) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _open(widget.initialTab, control: widget.initialControl);
      });
    }
  }

  void _open(String kind, {bool control = false}) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => WorkbenchSurfacePage(
          sessionId: widget.sessionId,
          kind: kind,
          control: control,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(
          i18n.t('workbench:menu.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
      ),
      body: WorkbenchMenu(sessionId: widget.sessionId, onOpen: _open),
    );
  }
}
