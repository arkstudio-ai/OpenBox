import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../api/logto.dart';
import '../state/auth_flow.dart';

/// Hands the sign-in and sign-up screens over to Logto (web `SsoEntry`).
///
/// Where the web app redirects the page on arrival, a phone asks first: the
/// sheet opens with a system prompt about sharing an identity with the site,
/// and having that appear unbidden as a screen loads reads as a misfire rather
/// than as something the person set off.
///
/// [child] — the account/password form — stays underneath as the fallback for
/// a deployment with no native Logto application, and is revealed when the
/// sheet fails: a spinner with no way in would be worse than a password box.
class SsoGate extends ConsumerStatefulWidget {
  const SsoGate({super.key, required this.register, required this.child});

  /// Which Logto screen opens first — its sign-up page for a "get started"
  /// entry, its sign-in page otherwise.
  final bool register;
  final Widget child;

  @override
  ConsumerState<SsoGate> createState() => _SsoGateState();
}

class _SsoGateState extends ConsumerState<SsoGate> {
  bool _busy = false;
  bool _failed = false;

  Future<void> _start(LogtoSso sso) async {
    setState(() => _busy = true);
    try {
      await ref
          .read(authFlowProvider)
          .loginWithLogto(sso, register: widget.register);
      if (mounted) context.go(Paths.app);
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
      loading: () => const Padding(
        padding: EdgeInsets.symmetric(vertical: 32),
        child: Center(child: CircularProgressIndicator(strokeWidth: 2)),
      ),
      error: (_, _) => widget.child,
      data: (config) => config == null ? widget.child : _panel(context, config),
    );
  }

  Widget _panel(BuildContext context, LogtoSso config) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          i18n.t(widget.register ? 'auth:registerTitle' : 'auth:loginTitle'),
          style: Theme.of(context).textTheme.titleLarge,
        ),
        const SizedBox(height: 8),
        Text(
          i18n.t(widget.register ? 'auth:registerBody' : 'auth:loginBody'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600, height: 1.55),
        ),
        const SizedBox(height: 24),
        SizedBox(
          width: double.infinity,
          height: 44,
          child: FilledButton(
            onPressed: _busy ? null : () => _start(config),
            style: FilledButton.styleFrom(
              backgroundColor: t.ink,
              foregroundColor: t.bg,
              disabledBackgroundColor: t.ink.withValues(alpha: 0.6),
              disabledForegroundColor: t.bg,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.lg),
              ),
            ),
            child: Text(
              _busy ? i18n.t('auth:ssoRedirecting') : i18n.t('auth:sso'),
              style: const TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ),
        if (_failed) ...[
          const SizedBox(height: 12),
          Text(
            i18n.t('auth:errors.ssoFailed'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
          ),
          const SizedBox(height: 20),
          Row(
            children: [
              Expanded(child: Divider(color: t.hair, height: 1)),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Text(
                  i18n.t('auth:or'),
                  style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
                ),
              ),
              Expanded(child: Divider(color: t.hair, height: 1)),
            ],
          ),
          const SizedBox(height: 20),
          widget.child,
        ] else ...[
          const SizedBox(height: 16),
          Text(
            i18n.t('auth:legal'),
            style: TextStyle(fontSize: FontSizes.xs2, color: t.n500, height: 1.5),
          ),
        ],
      ],
    );
  }
}
