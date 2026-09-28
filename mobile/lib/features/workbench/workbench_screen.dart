import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/appearance_store.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import 'widgets/workbench_menu.dart';
import 'workbench_surface_page.dart';

/// The web right-panel (`WorkbenchPanel`) re-flowed as a route.
///
/// Web opens the panel on a **menu tab** and turns it into whichever surface
/// you pick; the tab strip is how you come back. A phone has no room for a
/// strip, so the same menu is this route and a pick *pushes* the surface —
/// which means the stock back arrow and the iOS edge-swipe both return here,
/// and one more back leaves the panel. Same six surfaces, same live hints.
class WorkbenchScreen extends ConsumerStatefulWidget {
  const WorkbenchScreen({
    super.key,
    required this.sessionId,
    this.initialTab = menuTab,
    this.initialControl = false,
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

  @override
  ConsumerState<WorkbenchScreen> createState() => _WorkbenchScreenState();
}

class _WorkbenchScreenState extends ConsumerState<WorkbenchScreen> {
  @override
  void initState() {
    super.initState();
    // Without developer mode the desktop is this screen's body, so a deep
    // link to it has nothing to push; any other surface still opens on top.
    final desktopOnly = !ref.read(appearanceProvider).developerMode;
    final skip =
        widget.initialTab == WorkbenchScreen.menuTab ||
        (desktopOnly && widget.initialTab == 'desktop');
    if (!skip) {
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
    final developerMode = ref.watch(
      appearanceProvider.select((s) => s.developerMode),
    );
    // Most people only ever use the cloud desktop: without developer mode the
    // panel opens straight onto it rather than a menu of one row.
    if (!developerMode) {
      return WorkbenchSurfacePage(
        sessionId: widget.sessionId,
        kind: 'desktop',
        control: widget.initialControl && widget.initialTab == 'desktop',
      );
    }
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
      body: WorkbenchMenu(
        sessionId: widget.sessionId,
        onOpen: _open,
        developerMode: developerMode,
      ),
    );
  }
}
