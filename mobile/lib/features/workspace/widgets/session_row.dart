import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/session.dart';
import '../../../shared/widgets/spinner.dart';

/// One session in the drawer list (web `SessionRow.tsx`): active highlight,
/// spinner while running; long-press opens the actions sheet.
class SessionRow extends ConsumerWidget {
  const SessionRow({
    super.key,
    required this.session,
    required this.active,
    required this.onOpen,
    required this.onDelete,
    required this.onRename,
    this.selecting = false,
    this.checked = false,
    this.onToggle,
  });

  final Session session;
  final bool active;
  final VoidCallback onOpen;
  final VoidCallback onDelete;
  final VoidCallback onRename;
  final bool selecting, checked;
  final VoidCallback? onToggle;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final currentUserId = ref.watch(authProvider).user?.id;
    final owned = session.userId == null || session.userId == currentUserId;
    final title = session.title.isEmpty
        ? i18n.t('workspace:untitledChat')
        : session.title;
    final label = !owned && session.ownerUsername?.isNotEmpty == true
        ? '$title · ${session.ownerUsername}'
        : title;
    final showActions = owned && !selecting;
    return Material(
      color: active || checked ? t.n200 : Colors.transparent,
      borderRadius: BorderRadius.circular(Radii.md),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: selecting ? onToggle : onOpen,
        onLongPress: showActions ? () => _showActions(context, i18n, t) : null,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
          child: Row(
            children: [
              if (selecting && owned && !session.isCron)
                Checkbox(
                  value: checked,
                  onChanged: onToggle == null ? null : (_) => onToggle!(),
                  visualDensity: VisualDensity.compact,
                ),
              if (session.isCron) ...[
                Icon(Icons.schedule, size: 13, color: t.n600),
                const SizedBox(width: 6),
              ],
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    color: t.ink,
                    fontWeight: active ? FontWeight.w500 : FontWeight.w400,
                  ),
                ),
              ),
              if (session.isLive) ...[
                const SizedBox(width: 8),
                const Spinner(size: 12),
              ],
              if (showActions)
                IconButton(
                  tooltip: i18n.t('workspace:sessionActions'),
                  visualDensity: VisualDensity.compact,
                  onPressed: () => _showActions(context, i18n, t),
                  icon: const Icon(Icons.more_horiz, size: 19),
                ),
            ],
          ),
        ),
      ),
    );
  }

  void _showActions(BuildContext context, I18nState i18n, BossipTokens t) {
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(Icons.edit_outlined, color: t.n700, size: 20),
              title: Text(
                i18n.t('workspace:renameChat'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              onTap: () {
                Navigator.pop(sheetContext);
                onRename();
              },
            ),
            ListTile(
              leading: Icon(Icons.delete_outline, color: t.danger, size: 20),
              title: Text(
                i18n.t('common:action.delete'),
                style: TextStyle(fontSize: FontSizes.base, color: t.danger),
              ),
              onTap: () {
                Navigator.pop(sheetContext);
                onDelete();
              },
            ),
          ],
        ),
      ),
    );
  }
}
