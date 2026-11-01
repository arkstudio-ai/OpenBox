import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/api/auth_store.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/config/env.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/format.dart';
import '../../shared/widgets/ui_mode_switch.dart';
import '../billing/state/billing_providers.dart';

/// The 「我的」 tab (docs `LIGHT_MODE_PLAN.md` §4.2, impl §4.2 T7): the
/// account with its credits, the doors to the pages Light does not redraw —
/// plans, the authorization centre, the cloud desktop, resources, Settings —
/// and the interface mode switch, so the way back to 专业 is one tap away.
/// Every door pushes the existing page onto the root navigator.
class LightMeScreen extends ConsumerWidget {
  const LightMeScreen({super.key});

  Future<void> _signOut(BuildContext context, WidgetRef ref) async {
    final i18n = ref.read(i18nProvider);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: Text(i18n.t('light:me.signOutTitle')),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialog, false),
            child: Text(i18n.t('light:me.cancel')),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialog, true),
            child: Text(
              i18n.t('common:action.signOut'),
              style: TextStyle(color: dialog.tokens.danger),
            ),
          ),
        ],
      ),
    );
    if (confirmed != true || !context.mounted) return;
    final router = GoRouter.of(context);
    unawaited(ref.read(authProvider.notifier).signOut());
    router.go(Paths.landing);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final user = ref.watch(authProvider).user;
    final credits = ref.watch(billingBalanceProvider);
    final doors = <(String, IconData, String, Key)>[
      (
        'light:me.billing',
        Icons.card_membership_outlined,
        Paths.billing(),
        const Key('light-me-billing'),
      ),
      (
        'light:me.authCenter',
        Icons.verified_user_outlined,
        Paths.authCenter(),
        const Key('light-me-auth-center'),
      ),
      (
        'light:me.desktop',
        Icons.desktop_windows_outlined,
        Paths.desktop,
        const Key('light-me-desktop'),
      ),
      (
        'light:me.resources',
        Icons.folder_open_outlined,
        Paths.resources(),
        const Key('light-me-resources'),
      ),
      (
        'light:me.settings',
        Icons.settings_outlined,
        Paths.settings(),
        const Key('light-me-settings'),
      ),
      if (user?.role == 'admin')
        (
          'light:me.admin',
          Icons.admin_panel_settings_outlined,
          Paths.admin,
          const Key('light-me-admin'),
        ),
    ];
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        titleSpacing: 16,
        title: Text(
          i18n.t('light:me.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
      ),
      body: ListView(
        key: const Key('light-me'),
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
        children: [
          if (user != null)
            Material(
              color: t.card,
              borderRadius: BorderRadius.circular(Radii.xl),
              child: InkWell(
                key: const Key('light-me-credits'),
                borderRadius: BorderRadius.circular(Radii.xl),
                onTap: () => context.push(Paths.billing('usage')),
                child: Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(Radii.xl),
                    border: Border.all(color: t.hair),
                  ),
                  child: Row(
                    children: [
                      CircleAvatar(
                        radius: 22,
                        backgroundColor: t.a200,
                        child: Text(
                          user.username.isEmpty
                              ? '?'
                              : user.username[0].toUpperCase(),
                          style: TextStyle(
                            color: t.ink,
                            fontSize: FontSizes.lg,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              user.username,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                fontSize: FontSizes.xl,
                                fontWeight: FontWeight.w600,
                                color: t.ink,
                              ),
                            ),
                            const SizedBox(height: 2),
                            Text(
                              credits.hasError
                                  ? i18n.t('workspace:creditsUnavailable')
                                  : i18n.t(
                                      'workspace:creditBalance',
                                      vars: {
                                        'value': formatCredits(
                                          credits.valueOrNull?.balance,
                                        ),
                                      },
                                    ),
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                color: t.n600,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Icon(Icons.chevron_right, size: 20, color: t.n500),
                    ],
                  ),
                ),
              ),
            ),
          const SizedBox(height: 16),
          Container(
            decoration: BoxDecoration(
              color: t.card,
              borderRadius: BorderRadius.circular(Radii.xl),
              border: Border.all(color: t.hair),
            ),
            child: Column(
              children: [
                for (final (index, (key, icon, path, widgetKey))
                    in doors.indexed) ...[
                  if (index > 0)
                    Divider(height: 1, indent: 52, color: t.hairSoft),
                  ListTile(
                    key: widgetKey,
                    leading: Icon(icon, size: 22, color: t.n700),
                    title: Text(
                      i18n.t(key),
                      style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                    ),
                    trailing: Icon(Icons.chevron_right, size: 18, color: t.n500),
                    onTap: () => context.push(path),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: 24),
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Text(
              i18n.t('light:uiMode.title'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n700,
              ),
            ),
          ),
          const UiModeSwitch(),
          const SizedBox(height: 24),
          TextButton(
            key: const Key('light-me-sign-out'),
            onPressed: () => _signOut(context, ref),
            style: TextButton.styleFrom(
              foregroundColor: t.danger,
              minimumSize: const Size.fromHeight(44),
            ),
            child: Text(
              i18n.t('light:me.signOut'),
              style: const TextStyle(fontSize: FontSizes.base),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child: Text(
              Env.isProduction
                  ? 'bossip'
                  : 'bossip · ${i18n.t('light:testBuild')} · ${Env.apiHost}',
              key: const Key('light-me-build'),
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
          ),
        ],
      ),
    );
  }
}
