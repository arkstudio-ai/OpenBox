import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';

/// Bottom user row of the drawer (web `UserRow.tsx`): avatar, username,
/// `role · N sessions`; tap opens Settings / Sign out.
class UserRow extends ConsumerWidget {
  const UserRow({super.key, required this.sessionCount, required this.onSignOut});

  final int sessionCount;
  final VoidCallback onSignOut;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final user = ref.watch(authProvider).user;
    if (user == null) return const SizedBox.shrink();
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: () => _showMenu(context, i18n, t),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
          child: Row(
            children: [
              CircleAvatar(
                radius: 15,
                backgroundColor: t.a200,
                child: Text(
                  user.username.isEmpty
                      ? '?'
                      : user.username[0].toUpperCase(),
                  style: TextStyle(
                    color: t.ink,
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      user.username,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.md,
                        color: t.ink,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    Text(
                      i18n.t('workspace:userLine',
                          vars: {'role': user.role, 'count': sessionCount}),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ],
                ),
              ),
              Icon(Icons.more_horiz, size: 18, color: t.n600),
            ],
          ),
        ),
      ),
    );
  }

  void _showMenu(BuildContext context, I18nState i18n, BossipTokens t) {
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(Icons.settings_outlined, color: t.n700, size: 20),
              title: Text(i18n.t('workspace:settings'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              onTap: () {
                Navigator.pop(sheetContext);
                context.push(Paths.settings());
              },
            ),
            ListTile(
              leading: Icon(Icons.logout, color: t.danger, size: 20),
              title: Text(
                i18n.t('common:action.signOut'),
                style: TextStyle(fontSize: FontSizes.base, color: t.danger),
              ),
              onTap: () {
                Navigator.pop(sheetContext);
                onSignOut();
              },
            ),
          ],
        ),
      ),
    );
  }
}
