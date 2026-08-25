import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/containers_api.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import 'widgets/browser_tab.dart';
import 'widgets/desktop_tab.dart';
import 'widgets/files_tab.dart';
import 'widgets/review_tab.dart';
import 'widgets/terminal_tab.dart';

/// The web right-panel (`WorkbenchPanel`) re-flowed as a full-screen route
/// with a segmented tab bar, same tab set as web:
/// 审阅 / 终端 / 浏览器 / 文件 / 云桌面.
class WorkbenchScreen extends ConsumerStatefulWidget {
  const WorkbenchScreen({
    super.key,
    required this.sessionId,
    this.initialTab = 'review',
  });

  final String sessionId;
  final String initialTab;

  @override
  ConsumerState<WorkbenchScreen> createState() => _WorkbenchScreenState();
}

class _WorkbenchScreenState extends ConsumerState<WorkbenchScreen> {
  late String _tab = widget.initialTab;
  bool _creatingSandbox = false;

  static const _tabs = ['review', 'terminal', 'browser', 'files', 'desktop'];

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(
          i18n.t('workbench:tabs.$_tab'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(46),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 10),
            child: Row(
              children: [
                for (final tab in _tabs)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(
                        i18n.t('workbench:tabs.$tab'),
                        style: const TextStyle(fontSize: FontSizes.sm),
                      ),
                      selected: _tab == tab,
                      showCheckmark: false,
                      selectedColor: t.a200,
                      backgroundColor: t.bg,
                      labelStyle: TextStyle(color: t.ink),
                      side: BorderSide(color: _tab == tab ? t.a700 : t.hair),
                      onSelected: (_) => setState(() => _tab = tab),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
      body: switch (_tab) {
        'terminal' => _withContainer(
            (containerId) => TerminalTab(containerId: containerId)),
        'browser' => _withContainer((_) => const BrowserTab()),
        'files' => _withContainer(
            (containerId) =>
                FilesTab(sessionId: widget.sessionId, containerId: containerId),
          ),
        'desktop' => const DesktopTab(),
        _ => ReviewTab(sessionId: widget.sessionId),
      },
    );
  }

  /// Terminal/files need a running sandbox (web `TerminalTab` empty state).
  Widget _withContainer(Widget Function(String containerId) builder) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final container = ref.watch(runningContainerProvider);
    return container.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(i18n.t('workbench:sandbox.none'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
      ),
      data: (info) {
        if (info == null) {
          return Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(i18n.t('workbench:sandbox.none'),
                    style: TextStyle(fontSize: FontSizes.base, color: t.n700)),
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
