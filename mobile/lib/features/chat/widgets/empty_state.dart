import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Empty-chat greeting (web `EmptyState.tsx`): time-of-day greeting with
/// username + clickable suggestion cards.
class ChatEmptyState extends ConsumerWidget {
  const ChatEmptyState({super.key, required this.onPick, this.projectName});

  final void Function(String text) onPick;
  final String? projectName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final name = ref.watch(authProvider).user?.username ?? '';
    final hour = DateTime.now().hour;
    final slot = hour < 12
        ? 'morning'
        : hour < 18
            ? 'afternoon'
            : 'evening';
    final suggestions = i18n.tList('workspace:suggestions');

    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 40, 20, 20),
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      children: [
        Text(
          i18n.t('workspace:greeting.$slot', vars: {'name': name}),
          style: TextStyle(
            fontSize: FontSizes.xl3,
            height: 1.3,
            fontWeight: FontWeight.w500,
            letterSpacing: -0.4,
            color: t.ink,
          ),
        ),
        if (projectName != null) ...[
          const SizedBox(height: 8),
          Text(
            i18n.t('workspace:emptyHint', vars: {'project': projectName!}),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600, height: 1.6),
          ),
        ],
        const SizedBox(height: 24),
        for (final suggestion in suggestions)
          if (suggestion is Map<String, dynamic>)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Material(
                color: t.card,
                borderRadius: BorderRadius.circular(Radii.lg),
                child: InkWell(
                  borderRadius: BorderRadius.circular(Radii.lg),
                  onTap: () => onPick(suggestion['title'] as String? ?? ''),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 12),
                    decoration: BoxDecoration(
                      border: Border.all(color: t.hair),
                      borderRadius: BorderRadius.circular(Radii.lg),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          suggestion['title'] as String? ?? '',
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            color: t.ink,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          suggestion['hint'] as String? ?? '',
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.n600),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
      ],
    );
  }
}
