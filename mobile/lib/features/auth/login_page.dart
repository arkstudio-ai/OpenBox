import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import 'state/auth_flow.dart';
import 'widgets/auth_fields.dart';
import 'widgets/auth_shell.dart';
import 'widgets/sso_gate.dart';

/// Login screen (web `features/auth/components/LoginForm.tsx`).
class LoginPage extends ConsumerStatefulWidget {
  const LoginPage({super.key});

  @override
  ConsumerState<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends ConsumerState<LoginPage> {
  final _account = TextEditingController();
  final _password = TextEditingController();
  bool _remember = true;
  bool _submitting = false;
  String? _error;

  @override
  void dispose() {
    _account.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final i18n = ref.read(i18nProvider);
    if (_account.text.trim().isEmpty || _password.text.isEmpty) {
      setState(() => _error = i18n.t('auth:errors.required'));
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      await ref
          .read(authFlowProvider)
          .login(_account.text.trim(), _password.text);
      if (mounted) context.go(Paths.app);
    } catch (e) {
      if (mounted) {
        setState(() => _error = errorText(ref.read(i18nProvider), e));
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AuthShell(
      child: SsoGate(register: false, child: _form(context)),
    );
  }

  /// The account/password form — reached only when Logto is unavailable,
  /// see [SsoGate].
  Widget _form(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(i18n.t('auth:loginTitle'),
            style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 8),
        Text(
          i18n.t('auth:loginBody'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600, height: 1.55),
        ),
        const SizedBox(height: 20),
        AuthTextField(
          label: i18n.t('auth:accountLabel'),
          controller: _account,
          placeholder: i18n.t('auth:accountPlaceholder'),
          autofillHints: const [AutofillHints.username],
          textInputAction: TextInputAction.next,
        ),
        const SizedBox(height: 14),
        PasswordField(
          label: i18n.t('auth:pwLabel'),
          controller: _password,
          placeholder: i18n.t('auth:pwPlaceholder'),
          showLabel: i18n.t('auth:show'),
          hideLabel: i18n.t('auth:hide'),
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 12),
        GestureDetector(
          onTap: () => setState(() => _remember = !_remember),
          child: Row(
            children: [
              Icon(
                _remember ? Icons.check_box : Icons.check_box_outline_blank,
                size: 18,
                color: _remember ? t.a700 : t.n500,
              ),
              const SizedBox(width: 6),
              Text(
                i18n.t('auth:remember'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
            ],
          ),
        ),
        if (_error != null) ...[
          const SizedBox(height: 10),
          Text(
            _error!,
            style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
          ),
        ],
        const SizedBox(height: 18),
        SizedBox(
          width: double.infinity,
          height: 44,
          child: FilledButton(
            onPressed: _submitting ? null : _submit,
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
              _submitting ? i18n.t('auth:signingIn') : i18n.t('auth:signInBtn'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ),
        const SizedBox(height: 16),
        GestureDetector(
          onTap: () => context.go(Paths.register),
          child: Text(
            i18n.t('auth:noAccount'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.a700),
          ),
        ),
        const SizedBox(height: 12),
        Text(
          i18n.t('auth:legal'),
          style: TextStyle(fontSize: FontSizes.xs2, color: t.n500, height: 1.5),
        ),
      ],
    );
  }
}
