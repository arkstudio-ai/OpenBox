import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
  });

  final Session session;
  final bool active;
  final VoidCallback onOpen;
  final VoidCallback onDelete;
  final VoidCallback onRename;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final title = session.title.isEmpty
        ? i18n.t('workspace:untitledChat')
        : session.title;
    return Material(
      color: active ? t.n200 : Colors.transparent,
      borderRadius: BorderRadius.circular(Radii.md),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: onOpen,
        onLongPress: () => _showActions(context, i18n, t),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  title,
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
              title: Text(i18n.t('workspace:rename'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
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
