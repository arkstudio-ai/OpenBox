import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../api/ui_mode_store.dart';
import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../i18n/i18n.dart';
import '../models/ui_mode.dart';
import '../router/paths.dart';
import 'toast.dart';

/// 界面模式 — Light or 专业 (docs `LIGHT_MODE_PLAN.md` §4.7 #9). Two cards;
/// choosing one saves it and opens that mode's home, so the switch is seen
/// to take effect rather than waiting for the next launch.
///
/// Shared by Settings → 外观 and the Light 「我的」 tab, which is why it lives
/// here rather than in either feature.
class UiModeSwitch extends ConsumerStatefulWidget {
  const UiModeSwitch({super.key});

  @override
  ConsumerState<UiModeSwitch> createState() => _UiModeSwitchState();
}

class _UiModeSwitchState extends ConsumerState<UiModeSwitch> {
  bool _busy = false;

  Future<void> _pick(UiMode mode) async {
    if (_busy) return;
    final current = ref.read(uiModeProvider).mode;
    setState(() => _busy = true);
    try {
      await ref.read(uiModeProvider.notifier).setMode(mode);
      if (!mounted) return;
      if (mode != current) {
        context.go(mode == UiMode.light ? Paths.home : Paths.app);
      }
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('light:uiMode.failed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final mode = ref.watch(uiModeProvider).mode;
    // Both cards as tall as the longer body (Vietnamese runs long).
    return IntrinsicHeight(
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (index, option) in UiMode.values.indexed) ...[
            if (index > 0) const SizedBox(width: 10),
            Expanded(
              child: _ModeCard(
                key: Key('ui-mode-${option.wire}'),
                selected: mode == option,
                title: i18n.t('light:uiMode.${option.wire}'),
                body: i18n.t('light:uiMode.${option.wire}Body'),
                icon: option == UiMode.light
                    ? Icons.grid_view_rounded
                    : Icons.dashboard_customize_outlined,
                onTap: _busy ? null : () => _pick(option),
                tokens: t,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _ModeCard extends StatelessWidget {
  const _ModeCard({
    super.key,
    required this.selected,
    required this.title,
    required this.body,
    required this.icon,
    required this.onTap,
    required this.tokens,
  });

  final bool selected;
  final String title;
  final String body;
  final IconData icon;
  final VoidCallback? onTap;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    final t = tokens;
    return Material(
      color: selected ? t.hairSoft : t.card,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.lg),
            border: Border.all(
              color: selected ? t.ink : t.hair,
              width: selected ? 1.4 : 1,
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(icon, size: 18, color: selected ? t.ink : t.n700),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      title,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                  ),
                  if (selected) Icon(Icons.check, size: 16, color: t.a800),
                ],
              ),
              const SizedBox(height: 6),
              Text(
                body,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  height: 1.4,
                  color: t.n600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
