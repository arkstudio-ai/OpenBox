import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';

/// Local terminal-status tone: chat renders receipts from part data alone and
/// features must not import each other (README 分层); the jobs feature keeps
/// its own richer map for live cards.
(Color, String) _receiptTone(BossipTokens t, String status) => switch (status) {
      'succeeded' => (t.sage, 'jobs:status.succeeded'),
      'failed' => (t.danger, 'jobs:status.failed'),
      _ => (t.n400, 'jobs:status.cancelled'),
    };

/// Durable transcript record of finished background jobs (web
/// `SkillJobReceipts`): the dock shows live cards, this is what remains.
class SkillJobReceipts extends ConsumerWidget {
  const SkillJobReceipts({super.key, required this.parts});

  final List<MessagePart> parts;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final receipts = parts.whereType<SkillJobPart>().toList();
    if (receipts.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final part in receipts)
          Container(
            margin: const EdgeInsets.only(top: 8),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: t.n100.withValues(alpha: 0.5),
              border: Border.all(color: t.hair),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Row(children: [
              Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: _receiptTone(t, part.status).$1,
                  shape: BoxShape.circle,
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '${part.skillKey.replaceFirst(RegExp(r'^(builtin|user):'), '')} · ${part.operation}',
                style: TextStyle(
                    color: t.n800,
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500),
              ),
              const SizedBox(width: 8),
              Text(i18n.t(_receiptTone(t, part.status).$2),
                  style: TextStyle(color: t.n500, fontSize: FontSizes.xs)),
              if (part.summary.isNotEmpty) ...[
                const SizedBox(width: 8),
                Expanded(
                  child: Text(part.summary,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: t.n600, fontSize: FontSizes.xs)),
                ),
              ],
            ]),
          ),
      ],
    );
  }
}
