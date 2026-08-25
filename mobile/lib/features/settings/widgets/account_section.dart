import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Account settings (web `AccountPage`): read-only identity rows.
class AccountSection extends ConsumerWidget {
  const AccountSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final user = ref.watch(authProvider).user;
    if (user == null) return const SizedBox.shrink();
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _rowCard(t, [
          _row(t, i18n.t('settings:account.username'), user.username),
          _row(t, i18n.t('settings:account.email'),
              user.email ?? i18n.t('settings:account.emailNone')),
          _row(t, i18n.t('settings:account.role'), user.role),
          _row(t, i18n.t('settings:account.userId'), user.id, mono: true),
        ]),
      ],
    );
  }

  Widget _rowCard(BossipTokens t, List<Widget> rows) => Container(
        decoration: BoxDecoration(
          color: t.card,
          borderRadius: BorderRadius.circular(Radii.xl),
          border: Border.all(color: t.hair),
        ),
        child: Column(children: rows),
      );

  Widget _row(BossipTokens t, String label, String value, {bool mono = false}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: t.hairSoft)),
      ),
      child: Row(
        children: [
          SizedBox(
            width: 90,
            child: Text(
              label,
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: t.ink,
                fontFamily: mono ? 'Menlo' : null,
                fontFamilyFallback: mono ? const ['monospace'] : null,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
