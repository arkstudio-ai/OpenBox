import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';

/// One-line change row (web `PatchChip` / D.4.6):
/// `⊞ path +N −M  审阅 →` — tap opens the review screen.
class PatchChip extends ConsumerWidget {
  const PatchChip({super.key, required this.patch, required this.onReview});

  final PatchPart patch;
  final VoidCallback onReview;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final additions = patch.files.fold(0, (sum, f) => sum + f.additions);
    final deletions = patch.files.fold(0, (sum, f) => sum + f.deletions);
    final label = patch.files.length == 1
        ? patch.files.first.path
        : i18n.t('chat:changedFiles', count: patch.files.length);
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: onReview,
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          decoration: BoxDecoration(
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.md),
            color: t.card,
          ),
          child: Row(
            children: [
              Text('⊞',
                  style: TextStyle(fontSize: FontSizes.base, color: t.n600)),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: t.ink,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const ['monospace'],
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Text('+$additions',
                  style: TextStyle(fontSize: FontSizes.xs, color: t.s700)),
              const SizedBox(width: 4),
              Text('−$deletions',
                  style: TextStyle(fontSize: FontSizes.xs, color: t.dangerInk)),
              const SizedBox(width: 10),
              Text(
                i18n.t('chat:reviewGo'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
