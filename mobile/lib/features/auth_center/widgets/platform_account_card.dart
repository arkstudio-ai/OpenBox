import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import 'auth_widgets.dart';

/// One bound account (web `AccountRow`): name, state, when it lapses. The
/// bookkeeping (openid, grant window, renewals left, scopes, last probe) sits
/// behind a details toggle — listed under every account it read as a ledger.
class PlatformAccountCard extends ConsumerStatefulWidget {
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
  ConsumerState<PlatformAccountCard> createState() =>
      _PlatformAccountCardState();
}

class _PlatformAccountCardState extends ConsumerState<PlatformAccountCard> {
  bool _details = false;

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final account = widget.account;
    final now = DateTime.now();
    final status = account.statusAt(now);
    final bound = account.status == 'bound';
    final avatar = Uri.tryParse(account.avatarUrl ?? '');
    final small = TextStyle(fontSize: 12, color: t.n600);
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
              AuthStatusPill(status),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: Text(
                  bound
                      ? i18n.t(
                          'auth-center:account.estimatedExpiry',
                          vars: {
                            'date': platformDate(account.expectedExpiry, i18n),
                            'days': max(
                              0,
                              account.expectedExpiry?.difference(now).inDays ??
                                  0,
                            ),
                          },
                        )
                      : i18n.t('auth-center:account.needsReauth'),
                  style: TextStyle(color: bound ? t.n600 : t.ink, fontSize: 13),
                ),
              ),
              TextButton.icon(
                key: ValueKey('account-details-${account.id}'),
                onPressed: () => setState(() => _details = !_details),
                style: TextButton.styleFrom(
                  foregroundColor: t.n600,
                  minimumSize: const Size(0, 28),
                  padding: const EdgeInsets.symmetric(horizontal: 6),
                  visualDensity: VisualDensity.compact,
                ),
                icon: Icon(
                  _details ? Icons.expand_less : Icons.expand_more,
                  size: 16,
                ),
                label: Text(
                  i18n.t(
                    _details
                        ? 'auth-center:account.hideDetails'
                        : 'auth-center:account.details',
                  ),
                  style: const TextStyle(fontSize: 12),
                ),
              ),
            ],
          ),
          if (_details) ...[
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
              style: small,
            ),
            if (bound) ...[
              Text(
                i18n.t(
                  'auth-center:account.validUntil',
                  vars: {'date': platformDate(account.refreshExpiresAt, i18n)},
                ),
                style: small,
              ),
              Text(
                i18n.t(
                  'auth-center:account.renewalsLeft',
                  count: account.renewalsLeft,
                ),
                style: small,
              ),
            ],
            if (account.scopes.isNotEmpty)
              Text(account.scopes.join(' · '), style: small),
            Text(
              i18n.t(
                'auth-center:account.lastProbe',
                vars: {'date': platformDate(account.lastProbeAt, i18n)},
              ),
              style: small,
            ),
          ],
          Wrap(
            spacing: 8,
            children: [
              if (bound)
                TextButton.icon(
                  onPressed: widget.busy || !widget.configured
                      ? null
                      : widget.onProbe,
                  icon: const Icon(Icons.refresh, size: 16),
                  label: Text(i18n.t('auth-center:actions.probe')),
                ),
              if (widget.canManage && status != 'bound')
                TextButton(
                  onPressed: widget.busy || !widget.configured
                      ? null
                      : widget.onBind,
                  child: Text(i18n.t('auth-center:actions.reauthorize')),
                ),
              if (widget.canManage)
                TextButton(
                  onPressed: widget.busy ? null : widget.onUnbind,
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
