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
    final muted = TextStyle(fontSize: FontSizes.xs, color: t.n600);
    final statusColor = bound
        ? t.a700
        : status == 'expired'
        ? t.danger
        : t.n600;
    // Compact like a settings row: the site and its state on the left, the
    // one action it needs on the right, anything else quietly underneath.
    final compact = ButtonStyle(
      minimumSize: const WidgetStatePropertyAll(Size(0, 34)),
      padding: const WidgetStatePropertyAll(
        EdgeInsets.symmetric(horizontal: 14),
      ),
      textStyle: const WidgetStatePropertyAll(
        TextStyle(fontSize: FontSizes.sm),
      ),
    );
    final probe = row != null && status != 'revoked';
    final Widget? primary = !bound
        ? FilledButton(
            // A quiet tonal pill: a list of sites must not read as a wall of
            // dark buttons.
            style: compact.copyWith(
              backgroundColor: WidgetStateProperty.resolveWith(
                (states) =>
                    states.contains(WidgetState.disabled) ? t.n100 : t.n200,
              ),
              foregroundColor: WidgetStateProperty.resolveWith(
                (states) =>
                    states.contains(WidgetState.disabled) ? t.n500 : t.ink,
              ),
            ),
            onPressed:
                busy || awaiting || site.reconPending || onViewDesktop == null
                ? null
                : onLogin,
            child: Text(
              i18n.t(
                'auth-center:desktop.actions.${status == 'expired' ? 'relogin' : 'login'}',
              ),
            ),
          )
        // Any registered row can be re-checked; that is how an expired one
        // comes back to bound after logging in again on the desktop.
        : probe
        ? OutlinedButton(
            style: compact,
            onPressed: busy || awaiting ? null : onProbe,
            child: Text(i18n.t('auth-center:actions.probe')),
          )
        : null;
    final secondary = <Widget>[
      if (!bound && probe)
        TextButton(
          onPressed: busy || awaiting ? null : onProbe,
          child: Text(i18n.t('auth-center:actions.probe')),
        ),
      if (awaiting)
        TextButton(
          onPressed: onViewDesktop,
          child: Text(i18n.t('auth-center:desktop.actions.viewDesktop')),
        ),
      if (canManage && bound)
        TextButton(
          onPressed: busy ? null : onLogout,
          child: Text(i18n.t('auth-center:desktop.actions.logout')),
        ),
    ];
    final notes = <Widget>[
      if (site.reconPending)
        Text(i18n.t('auth-center:desktop.reconPending'), style: muted),
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
          style: muted,
        ),
      if (row != null)
        Text(
          i18n.t(
            'auth-center:account.lastProbe',
            vars: {'date': platformDateTime(row.lastProbeAt, i18n)},
          ),
          style: muted,
        ),
      if (awaiting)
        Text(i18n.t('auth-center:desktop.awaitingHint'), style: muted),
      if (!bound && (row?.lastError?.isNotEmpty ?? false))
        Text(
          row!.lastError!,
          style: TextStyle(color: t.danger, fontSize: FontSizes.xs),
        ),
    ];
    return Container(
      key: ValueKey('desktop-site-${site.key}'),
      width: double.infinity,
      margin: const EdgeInsets.only(top: 4),
      padding: const EdgeInsets.symmetric(vertical: 12),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      site.display,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: t.ink,
                        fontWeight: FontWeight.w500,
                        fontSize: FontSizes.base,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Wrap(
                      spacing: 6,
                      runSpacing: 2,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Container(
                          width: 6,
                          height: 6,
                          decoration: BoxDecoration(
                            color: bound ? t.a700 : statusColor,
                            shape: BoxShape.circle,
                          ),
                        ),
                        Text(
                          i18n.t(
                            'auth-center:desktop.status.${awaiting ? 'awaiting' : status}',
                          ),
                          style: TextStyle(
                            color: statusColor,
                            fontSize: FontSizes.xs,
                          ),
                        ),
                        if (row?.nickname?.isNotEmpty ?? false)
                          Text(row!.nickname!, style: muted),
                        if (shop is String || shop is num)
                          Text(
                            i18n.t(
                              'auth-center:desktop.display.account',
                              vars: {'name': shop},
                            ),
                            style: muted,
                          ),
                        if (role is String && role.isNotEmpty)
                          Text(role, style: muted),
                      ],
                    ),
                  ],
                ),
              ),
              if (primary != null) ...[const SizedBox(width: 12), primary],
            ],
          ),
          if (notes.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                spacing: 2,
                children: notes,
              ),
            ),
          if (secondary.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Wrap(spacing: 4, children: secondary),
            ),
        ],
      ),
    );
  }
}
