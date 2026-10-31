import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/first_video_entry.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/legal/legal_links.dart';
import '../../../shared/router/paths.dart';
import '../api/logto.dart';
import '../state/auth_flow.dart';

/// Hands the sign-in and sign-up screens over to Logto (web `SsoEntry`).
///
/// Both clients require an explicit policy choice before opening hosted SSO.
/// The two screens differ in title, copy, button and the Logto page that opens
/// first, and each links to the other, so 登录 and 注册 never look like one
/// page.
///
/// [child] — the account/password form — is only for a deployment whose
/// server reports SSO off (no native Logto application). With SSO on, an
/// unreachable config or a failed attempt offers a retry instead: accounts
/// created through SSO have no password to type into that form.
class SsoGate extends ConsumerStatefulWidget {
  const SsoGate({super.key, required this.register, required this.child});

  /// Which Logto screen opens first — its sign-up page for 注册, its sign-in
  /// page otherwise.
  final bool register;
  final Widget child;

  @override
  ConsumerState<SsoGate> createState() => _SsoGateState();
}

class _SsoGateState extends ConsumerState<SsoGate> {
  bool _busy = false;
  bool _accepted = false;
  bool _failed = false;

  Future<void> _start(LogtoSso sso) async {
    if (_busy || !_accepted) return;
    setState(() {
      _busy = true;
      _failed = false;
    });
    try {
      await ref
          .read(authFlowProvider)
          .loginWithLogto(
            sso,
            register: widget.register,
            acceptedLegal: _accepted,
          );
      if (mounted) {
        context.go(
          Paths.postAuthDestination(
            GoRouterState.of(context).uri,
            entry: ref.read(firstVideoEntryProvider),
          ),
        );
      }
    } on PlatformException catch (e) {
      // Dismissing the sheet is a decision, not a failure: leave the screen as
      // it was so a second tap starts over.
      if (e.code != 'CANCELED' && mounted) setState(() => _failed = true);
    } catch (_) {
      if (mounted) setState(() => _failed = true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final sso = ref.watch(logtoSsoProvider);
    return sso.when(
      // A retry shows that it is working, not the error it is replacing.
      skipLoadingOnRefresh: false,
      loading: () => const Padding(
        padding: EdgeInsets.symmetric(vertical: 32),
        child: Center(child: CircularProgressIndicator(strokeWidth: 2)),
      ),
      error: (_, _) => _unavailable(context),
      data: (config) => config == null ? widget.child : _panel(context, config),
    );
  }

  /// Title and lede: which of the two screens this is.
  List<Widget> _heading(BuildContext context, I18nState i18n) => [
    Text(
      i18n.t(widget.register ? 'auth:registerTitle' : 'auth:loginTitle'),
      style: Theme.of(context).textTheme.titleLarge,
    ),
    const SizedBox(height: 8),
    Text(
      i18n.t(widget.register ? 'auth:registerBody' : 'auth:loginBody'),
      style: TextStyle(
        fontSize: FontSizes.sm,
        color: context.tokens.n600,
        height: 1.55,
      ),
    ),
  ];

  ButtonStyle _primaryStyle(BossipTokens t) => FilledButton.styleFrom(
    backgroundColor: t.ink,
    foregroundColor: t.bg,
    disabledBackgroundColor: t.ink.withValues(alpha: 0.6),
    disabledForegroundColor: t.bg,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(Radii.lg),
    ),
  );

  /// The config request failed, so whether SSO is on is unknown: retry it.
  Widget _unavailable(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        ..._heading(context, i18n),
        const SizedBox(height: 16),
        Text(
          i18n.t('auth:ssoUnavailable'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700, height: 1.55),
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: double.infinity,
          height: 44,
          child: FilledButton(
            key: const Key('sso-retry'),
            onPressed: () => ref.invalidate(logtoSsoProvider),
            style: _primaryStyle(t),
            child: Text(
              i18n.t('common:action.retry'),
              style: const TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _panel(BuildContext context, LogtoSso config) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final register = widget.register;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        ..._heading(context, i18n),
        const SizedBox(height: 16),
        LegalConsent(
          accepted: _accepted,
          onChanged: (value) => setState(() => _accepted = value),
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: double.infinity,
          height: 44,
          child: FilledButton(
            key: const Key('sso-start'),
            onPressed: _busy || !_accepted ? null : () => _start(config),
            style: _primaryStyle(t),
            child: Text(
              i18n.t(
                _busy
                    ? 'auth:ssoRedirecting'
                    : register
                    ? 'auth:ssoRegister'
                    : 'auth:ssoSignIn',
              ),
              style: const TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ),
        // The button above is the retry; the password form stays hidden while
        // SSO is on (see [SsoGate]).
        if (_failed) ...[
          const SizedBox(height: 12),
          Text(
            i18n.t('auth:errors.ssoFailed'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
          ),
        ],
        const SizedBox(height: 16),
        GestureDetector(
          key: const Key('sso-switch'),
          onTap: () => context.go(
            Paths.authPeer(
              register ? Paths.login : Paths.register,
              GoRouterState.of(context).uri,
            ),
          ),
          child: Text(
            i18n.t(register ? 'auth:haveAccount' : 'auth:noAccount'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.a700),
          ),
        ),
      ],
    );
  }
}
