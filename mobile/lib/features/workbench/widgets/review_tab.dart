import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/diff.dart';
import '../state/workbench_providers.dart';

/// Review tab (web `ReviewTab`): expandable file cards with a unified diff —
/// add/del backgrounds + mono text, no syntax highlight, gaps collapsed as
/// "N 行未修改".
class ReviewTab extends ConsumerStatefulWidget {
  const ReviewTab({super.key, required this.sessionId});

  final String sessionId;

  @override
  ConsumerState<ReviewTab> createState() => _ReviewTabState();
}

class _ReviewTabState extends ConsumerState<ReviewTab> {
  String? _expandedPath;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final diff = ref.watch(sessionDiffProvider(widget.sessionId));

    return diff.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => _empty(t, i18n),
      data: (entries) {
        if (entries.isEmpty) return _empty(t, i18n);
        final additions = entries.fold(0, (sum, e) => sum + e.additions);
        final deletions = entries.fold(0, (sum, e) => sum + e.deletions);
        final expanded = _expandedPath ?? entries.first.path;
        return ListView(
          padding: const EdgeInsets.all(14),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    i18n.t('workbench:review.lastChanges'),
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      fontWeight: FontWeight.w600,
                      color: t.n800,
                    ),
                  ),
                ),
                Text('+$additions',
                    style: TextStyle(fontSize: FontSizes.xs, color: t.s700)),
                const SizedBox(width: 6),
                Text('−$deletions',
                    style:
                        TextStyle(fontSize: FontSizes.xs, color: t.dangerInk)),
              ],
            ),
            const SizedBox(height: 10),
            for (final entry in entries)
              _ReviewCard(
                entry: entry,
                expanded: entry.path == expanded,
                onTap: () => setState(() => _expandedPath = entry.path),
              ),
          ],
        );
      },
    );
  }

  Widget _empty(BossipTokens t, I18nState i18n) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              i18n.t('workbench:review.empty'),
              style: TextStyle(fontSize: FontSizes.base, color: t.n700),
            ),
            const SizedBox(height: 6),
            Text(
              i18n.t('workbench:review.emptyHint'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
            ),
          ],
        ),
      );
}

class _ReviewCard extends ConsumerWidget {
  const _ReviewCard({
    required this.entry,
    required this.expanded,
    required this.onTap,
  });

  final DiffEntry entry;
  final bool expanded;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final segments = entry.path.split('/');
    final base = segments.removeLast();
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.lg),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            borderRadius: BorderRadius.circular(Radii.lg),
            onTap: onTap,
            child: Padding(
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              child: Row(
                children: [
                  _badge(t),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text.rich(
                      TextSpan(
                        children: [
                          if (segments.isNotEmpty)
                            TextSpan(
                              text: '${segments.join('/')}/',
                              style: TextStyle(color: t.n500),
                            ),
                          TextSpan(
                            text: base,
                            style: TextStyle(
                              color: t.ink,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                      ),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: FontSizes.sm,
                        fontFamily: 'Menlo',
                        fontFamilyFallback: ['monospace'],
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text('+${entry.additions}',
                      style: TextStyle(fontSize: FontSizes.xs, color: t.s700)),
                  const SizedBox(width: 5),
                  Text('−${entry.deletions}',
                      style: TextStyle(
                          fontSize: FontSizes.xs, color: t.dangerInk)),
                ],
              ),
            ),
          ),
          if (expanded) _DiffBody(entry: entry, i18n: i18n),
        ],
      ),
    );
  }

  Widget _badge(BossipTokens t) {
    final (label, color) = switch (entry.status) {
      'added' => ('A', t.s700),
      'deleted' => ('D', t.danger),
      _ => ('M', t.a700),
    };
    return Container(
      width: 18,
      height: 18,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: FontSizes.xs2,
          fontWeight: FontWeight.w700,
          color: color,
        ),
      ),
    );
  }
}

class _DiffBody extends StatelessWidget {
  const _DiffBody({required this.entry, required this.i18n});

  final DiffEntry entry;
  final I18nState i18n;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final hunks = entry.hunks ?? const <DiffHunk>[];
    if (hunks.isEmpty) {
      final label = entry.status == 'added'
          ? i18n.t('workbench:review.skippedNew')
          : entry.status == 'deleted'
              ? i18n.t('workbench:review.skippedDeleted')
              : i18n.t('workbench:review.empty');
      return Padding(
        padding: const EdgeInsets.all(12),
        child: Text(label,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
      );
    }
    final rows = <Widget>[];
    int? lastNew;
    for (final hunk in hunks) {
      if (lastNew != null && hunk.newStart > lastNew + 1) {
        rows.add(_gapRow(t, hunk.newStart - lastNew - 1));
      }
      for (final line in hunk.lines) {
        rows.add(_lineRow(t, line));
      }
      lastNew = hunk.newStart + hunk.newCount - 1;
    }
    return Container(
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: ConstrainedBox(
          constraints:
              BoxConstraints(minWidth: MediaQuery.sizeOf(context).width - 58),
          child:
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: rows),
        ),
      ),
    );
  }

  Widget _gapRow(BossipTokens t, int count) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        child: Text(
          i18n.t('workbench:review.unchanged', count: count),
          style: TextStyle(fontSize: FontSizes.xs2, color: t.n500),
        ),
      );

  Widget _lineRow(BossipTokens t, DiffLine line) {
    final (bg, fg, sign) = switch (line.type) {
      'add' => (t.diffAdd, t.s700, '+'),
      'del' => (t.diffDel, t.dangerInk, '−'),
      _ => (Colors.transparent, t.n700, ' '),
    };
    final number = line.newLine ?? line.oldLine;
    return Container(
      color: bg,
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 34,
            child: Text(
              number?.toString() ?? '',
              textAlign: TextAlign.right,
              style: TextStyle(
                fontSize: FontSizes.xs2,
                color: t.n500,
                fontFamily: 'Menlo',
                fontFamilyFallback: const ['monospace'],
                height: 1.6,
              ),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            '$sign ${line.content}',
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: fg,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
              height: 1.6,
            ),
          ),
        ],
      ),
    );
  }
}
