import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/inbox.dart';
import '../../../shared/utils/format.dart';

/// One inbox row: category icon, title, one-line body, relative time, unread
/// dot, and a workspace chip when the row belongs to another workspace.
class InboxItemTile extends ConsumerWidget {
  const InboxItemTile({
    super.key,
    required this.item,
    required this.onTap,
    this.workspaceName,
  });

  final InboxItem item;
  final VoidCallback onTap;

  /// Set only when the row's workspace differs from the current one.
  final String? workspaceName;

  static IconData iconFor(InboxItem item) => switch (item.kind) {
    'task_completed' || 'cron_completed' => Icons.check_circle_outline,
    'task_failed' || 'cron_failed' || 'publish_failed' => Icons.error_outline,
    'input_required' || 'approval_required' => Icons.help_outline,
    'publish_done' => Icons.send_outlined,
    'platform_auth_expired' ||
    'desktop_login_expired' ||
    'desktop_login_reset' => Icons.key_off_outlined,
    'announcement' => Icons.campaign_outlined,
    _ => switch (item.category) {
      'session' => Icons.chat_bubble_outline,
      'notice' => Icons.campaign_outlined,
      _ => Icons.notifications_none,
    },
  };

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final unread = item.unread;
    final failed = item.kind.endsWith('_failed');
    return InkWell(
      key: ValueKey('inbox-${item.id}'),
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 34,
              height: 34,
              decoration: BoxDecoration(
                color: unread ? t.a100 : t.n200,
                shape: BoxShape.circle,
              ),
              child: Icon(
                iconFor(item),
                size: 17,
                color: failed ? t.danger : (unread ? t.a800 : t.n700),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          item.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            fontWeight: unread
                                ? FontWeight.w600
                                : FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        formatRelative(item.createdAt, i18n.language),
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                      ),
                      if (unread) ...[
                        const SizedBox(width: 6),
                        Container(
                          key: ValueKey('inbox-unread-${item.id}'),
                          width: 7,
                          height: 7,
                          decoration: BoxDecoration(
                            color: t.accent,
                            shape: BoxShape.circle,
                          ),
                        ),
                      ],
                    ],
                  ),
                  if (item.body.isNotEmpty) ...[
                    const SizedBox(height: 3),
                    Text(
                      item.body,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        height: 1.4,
                        color: t.n700,
                      ),
                    ),
                  ],
                  if (workspaceName != null || item.resolvedAt != null) ...[
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      children: [
                        if (workspaceName != null)
                          _Chip(
                            i18n.t(
                              'inbox:workspace',
                              vars: {'name': workspaceName!},
                            ),
                          ),
                        if (item.resolvedAt != null)
                          _Chip(i18n.t('inbox:resolved')),
                      ],
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(this.label);

  final String label;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: t.n200,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: FontSizes.xs2, color: t.n700),
      ),
    );
  }
}
