import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../api/chat_api.dart';
import '../markdown_view.dart';

/// Plan review card (web `chat:plan.review`): plan content + accept/reject
/// when the plan is ready. The plan list itself is read-only (Appendix D).
class PlanCard extends ConsumerWidget {
  const PlanCard({super.key, required this.plan, required this.sessionId});

  final PlanPart plan;
  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final statusLabel = switch (plan.status) {
      'ready' => i18n.t('chat:plan.review.title'),
      'accepted' => i18n.t('chat:plan.review.accepted'),
      'rejected' => i18n.t('chat:plan.review.rejected'),
      _ => i18n.t('chat:plan.title'),
    };
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            statusLabel,
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.n800,
            ),
          ),
          const SizedBox(height: 8),
          MarkdownView(plan.content, variant: MarkdownVariant.user),
          if (plan.status == 'ready') ...[
            const SizedBox(height: 12),
            Row(
              children: [
                FilledButton(
                  onPressed: () =>
                      ref.read(chatApiProvider).acceptPlan(sessionId),
                  style: FilledButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 6),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(i18n.t('chat:plan.review.accept'),
                      style: const TextStyle(fontSize: FontSizes.sm)),
                ),
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: () =>
                      ref.read(chatApiProvider).rejectPlan(sessionId),
                  style: OutlinedButton.styleFrom(
                    side: BorderSide(color: t.hair),
                    foregroundColor: t.ink,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 6),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(i18n.t('chat:plan.review.reject'),
                      style: const TextStyle(fontSize: FontSizes.sm)),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
