import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/containers_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/workbench_providers.dart';

/// The panel's home page (web `MenuTab`): one row per surface, each carrying
/// the same live hint the web menu does — how much is waiting for review,
/// whether a sandbox is up, which directory the files tab would open on.
///
/// Web converts the menu tab into the chosen kind in place, because it has a
/// tab strip to come back through. A phone has no strip, so a row opens that
/// surface and the app bar's back arrow returns here — same map, one column.

/// Surfaces, in the web menu's order.
const workbenchKinds = ['review', 'terminal', 'browser', 'files', 'desktop', 'cron'];

/// Same glyphs as web `TAB_GLYPH` — data, not icons, so the two stay identical.
const _glyphs = <String, String>{
  'review': '±',
  'terminal': '›_',
  'browser': '⊕',
  'files': '▤',
  'desktop': '▣',
  'cron': '◷',
};

class WorkbenchMenu extends ConsumerWidget {
  const WorkbenchMenu({super.key, required this.sessionId, required this.onOpen});

  final String sessionId;
  final ValueChanged<String> onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    // Hints stay human: a sandbox's machine id means nothing here, so files
    // shows the project directory and terminal only whether one is up (web).
    final pending = ref.watch(sessionDiffProvider(sessionId)).valueOrNull?.length ?? 0;
    final running = ref.watch(runningContainerProvider).valueOrNull;
    final workdir = ref.watch(sessionWorkdirProvider(sessionId)).valueOrNull;

    String hintFor(String kind) => switch (kind) {
          'review' => pending > 0
              ? i18n.t('workbench:menu.pending', count: pending)
              : i18n.t('workbench:menu.clean'),
          'terminal' => running != null ? i18n.t('workbench:menu.online') : '',
          'files' => _baseName(workdir),
          _ => '',
        };

    return ListView(
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 24),
      children: [
        for (final kind in workbenchKinds)
          _MenuRow(
            glyph: _glyphs[kind] ?? '',
            label: i18n.t('workbench:menu.$kind'),
            hint: hintFor(kind),
            onTap: () => onOpen(kind),
            tokens: t,
          ),
      ],
    );
  }
}

/// Trailing segment of the workdir, or empty when it is not known yet.
String _baseName(String? path) {
  if (path == null || path.isEmpty) return '';
  final parts = path.split('/').where((s) => s.isNotEmpty);
  return parts.isEmpty ? '' : parts.last;
}

class _MenuRow extends StatelessWidget {
  const _MenuRow({
    required this.glyph,
    required this.label,
    required this.hint,
    required this.onTap,
    required this.tokens,
  });

  final String glyph;
  final String label;
  final String hint;
  final VoidCallback onTap;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(Radii.full),
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 10),
        child: Row(
          children: [
            Container(
              width: 30,
              height: 30,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                border: Border.all(color: tokens.hair),
                shape: BoxShape.circle,
              ),
              child: Text(
                glyph,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: tokens.n700,
                  fontFamily: 'Menlo',
                  fontFamilyFallback: const ['monospace'],
                ),
              ),
            ),
            const SizedBox(width: 12),
            Text(label, style: TextStyle(fontSize: FontSizes.base, color: tokens.ink)),
            // The hint takes the slack and right-aligns inside it, so the
            // chevron sits on the same edge whether or not a row has one.
            Expanded(
              child: Padding(
                padding: const EdgeInsets.only(left: 12),
                child: Text(
                  hint,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.end,
                  style: TextStyle(fontSize: FontSizes.xs, color: tokens.n600),
                ),
              ),
            ),
            const SizedBox(width: 6),
            Icon(Icons.chevron_right, size: 18, color: tokens.n500),
          ],
        ),
      ),
    );
  }
}
