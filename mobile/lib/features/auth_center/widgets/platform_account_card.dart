import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import 'auth_widgets.dart';

class PlatformAccountCard extends ConsumerWidget {
  const PlatformAccountCard({
    super.key,
    required this.account,
    required this.canManage,
    required this.configured,
    required this.busy,
    required this.onProbe,
    required this.onBind,
    required this.onUnbind,
  });
  final PlatformAccount account;
  final bool canManage;
  final bool configured;
  final bool busy;
  final VoidCallback onProbe;
  final VoidCallback onBind;
  final VoidCallback onUnbind;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final now = DateTime.now();
    final status = account.statusAt(now);
    final avatar = Uri.tryParse(account.avatarUrl ?? '');
    return Padding(
      padding: const EdgeInsets.only(top: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Divider(color: t.hair),
          Row(
            children: [
              SizedBox(
                width: 38,
                height: 38,
                child: ClipOval(
                  child: avatar?.scheme == 'https'
                      ? Image.network(
                          avatar.toString(),
                          fit: BoxFit.cover,
                          errorBuilder: (_, _, _) =>
                              Icon(Icons.person_outline, color: t.n600),
                        )
                      : Icon(Icons.person_outline, color: t.n600),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  account.nickname ?? i18n.t('auth-center:account.unnamed'),
                  style: TextStyle(fontWeight: FontWeight.w600, color: t.ink),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              AuthStatusPill(status),
              Text(
                i18n.t(
                  'auth-center:account.openId',
                  vars: {
                    'id': account.externalId.substring(
                      0,
                      min(8, account.externalId.length),
                    ),
                  },
                ),
                style: TextStyle(fontSize: 12, color: t.n600),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (account.status == 'bound') ...[
            Text(
              i18n.t(
                'auth-center:account.estimatedExpiry',
                vars: {
                  'date': platformDate(account.expectedExpiry, i18n),
                  'days': max(
                    0,
                    account.expectedExpiry?.difference(now).inDays ?? 0,
                  ),
                },
              ),
              style: TextStyle(color: t.ink),
            ),
            Text(
              i18n.t(
                'auth-center:account.validUntil',
                vars: {'date': platformDate(account.refreshExpiresAt, i18n)},
              ),
              style: TextStyle(fontSize: 12, color: t.n600),
            ),
            Text(
              i18n.t(
                'auth-center:account.renewalsLeft',
                count: account.renewalsLeft,
              ),
              style: TextStyle(fontSize: 12, color: t.n600),
            ),
          ] else
            Text(i18n.t('auth-center:account.needsReauth')),
          if (account.scopes.isNotEmpty)
            Text(
              account.scopes.join(' · '),
              style: TextStyle(fontSize: 12, color: t.n600),
            ),
          Text(
            i18n.t(
              'auth-center:account.lastProbe',
              vars: {'date': platformDate(account.lastProbeAt, i18n)},
            ),
            style: TextStyle(fontSize: 12, color: t.n600),
          ),
          Wrap(
            spacing: 8,
            children: [
              if (account.status == 'bound')
                TextButton.icon(
                  onPressed: busy || !configured ? null : onProbe,
                  icon: const Icon(Icons.refresh, size: 16),
                  label: Text(i18n.t('auth-center:actions.probe')),
                ),
              if (canManage && status != 'bound')
                TextButton(
                  onPressed: busy || !configured ? null : onBind,
                  child: Text(i18n.t('auth-center:actions.reauthorize')),
                ),
              if (canManage)
                TextButton(
                  onPressed: busy ? null : onUnbind,
                  child: Text(
                    i18n.t('auth-center:actions.unbind'),
                    style: TextStyle(color: t.danger),
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
