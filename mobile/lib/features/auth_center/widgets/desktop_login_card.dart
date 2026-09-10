import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import 'auth_widgets.dart';

/// Desktop cookie sessions are separate from OAuth grants, including expiry.
class DesktopLoginCard extends ConsumerWidget {
  const DesktopLoginCard({
    super.key,
    required this.site,
    required this.account,
    required this.canManage,
    required this.busy,
    required this.awaiting,
    required this.onLogin,
    required this.onProbe,
    required this.onLogout,
    required this.onViewDesktop,
  });
  final PlatformInfo site;
  final PlatformAccount? account;
  final bool canManage;
  final bool busy;
  final bool awaiting;
  final VoidCallback onLogin;
  final VoidCallback onProbe;
  final VoidCallback onLogout;
  final VoidCallback? onViewDesktop;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final row = account;
    final rawStatus = row?.status ?? 'none';
    final status =
        const {
          'bound',
          'expired',
          'unknown',
          'desktop_offline',
          'revoked',
          'none',
        }.contains(rawStatus)
        ? rawStatus
        : 'unknown';
    final bound = status == 'bound';
    final predicted = row?.predictedExpiresAt;
    final shop = row?.probeDisplay['account_name'];
    final role = row?.probeDisplay['role'];
    return Container(
      key: ValueKey('desktop-site-${site.key}'),
      width: double.infinity,
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.symmetric(vertical: 12),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            site.display,
            style: TextStyle(
              color: t.ink,
              fontWeight: FontWeight.w500,
              fontSize: FontSizes.lg,
            ),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 10,
                  vertical: 4,
                ),
                decoration: BoxDecoration(
                  color: bound
                      ? t.a200
                      : status == 'expired'
                      ? t.dangerSoft
                      : t.n200,
                  borderRadius: BorderRadius.circular(Radii.sm),
                ),
                child: Text(
                  i18n.t(
                    'auth-center:desktop.status.${awaiting ? 'awaiting' : status}',
                  ),
                  style: TextStyle(
                    color: bound ? t.a700 : t.n700,
                    fontSize: FontSizes.xs,
                  ),
                ),
              ),
              if (row?.nickname?.isNotEmpty ?? false) Text(row!.nickname!),
            ],
          ),
          if (site.reconPending)
            Text(
              i18n.t('auth-center:desktop.reconPending'),
              style: TextStyle(color: t.n600),
            ),
          if (bound && predicted != null)
            Text(
              i18n.t(
                'auth-center:desktop.predicted',
                vars: {
                  'date': platformDate(predicted, i18n),
                  'days': predicted
                      .difference(DateTime.now())
                      .inDays
                      .clamp(0, 99999),
                },
              ),
            ),
          if (shop is String || shop is num)
            Text(
              i18n.t(
                'auth-center:desktop.display.account',
                vars: {'name': shop},
              ),
            ),
          if (role is String && role.isNotEmpty) Text(role),
          if (row != null)
            Text(
              i18n.t(
                'auth-center:account.lastProbe',
                vars: {'date': platformDateTime(row.lastProbeAt, i18n)},
              ),
              style: TextStyle(color: t.n600, fontSize: 12),
            ),
          if (awaiting)
            Text(
              i18n.t('auth-center:desktop.awaitingHint'),
              style: TextStyle(color: t.n600),
            ),
          if (!bound && (row?.lastError?.isNotEmpty ?? false))
            Text(
              row!.lastError!,
              style: TextStyle(color: t.danger, fontSize: 12),
            ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              if (bound)
                OutlinedButton(
                  onPressed: busy ? null : onProbe,
                  child: Text(i18n.t('auth-center:actions.probe')),
                )
              else
                FilledButton(
                  onPressed:
                      busy ||
                          awaiting ||
                          site.reconPending ||
                          onViewDesktop == null
                      ? null
                      : onLogin,
                  child: Text(
                    i18n.t(
                      'auth-center:desktop.actions.${status == 'expired' ? 'relogin' : 'login'}',
                    ),
                  ),
                ),
              if (awaiting)
                TextButton(
                  onPressed: onViewDesktop,
                  child: Text(
                    i18n.t('auth-center:desktop.actions.viewDesktop'),
                  ),
                ),
              if (canManage && bound)
                TextButton(
                  onPressed: busy ? null : onLogout,
                  child: Text(i18n.t('auth-center:desktop.actions.logout')),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
