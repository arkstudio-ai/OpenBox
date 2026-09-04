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

/// Registration screen (web `features/auth/components/RegisterForm.tsx`).
class RegisterPage extends ConsumerStatefulWidget {
  const RegisterPage({super.key});

  @override
  ConsumerState<RegisterPage> createState() => _RegisterPageState();
}

class _RegisterPageState extends ConsumerState<RegisterPage> {
  final _account = TextEditingController();
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _confirm = TextEditingController();
  bool _submitting = false;
  String? _error;

  @override
  void dispose() {
    _account.dispose();
    _email.dispose();
    _password.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final i18n = ref.read(i18nProvider);
    if (_account.text.trim().isEmpty || _password.text.isEmpty) {
      setState(() => _error = i18n.t('auth:errors.required'));
      return;
    }
    if (_password.text != _confirm.text) {
      setState(() => _error = i18n.t('auth:errors.pwMismatch'));
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      await ref.read(authFlowProvider).register(
            _account.text.trim(),
            _password.text,
            email: _email.text.trim(),
          );
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
      child: SsoGate(register: true, child: _form(context)),
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
        Text(i18n.t('auth:registerTitle'),
            style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 8),
        Text(
          i18n.t('auth:registerBody'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600, height: 1.55),
        ),
        const SizedBox(height: 20),
        AuthTextField(
          label: i18n.t('auth:accountLabel'),
          controller: _account,
          placeholder: i18n.t('auth:accountPlaceholder'),
          textInputAction: TextInputAction.next,
        ),
        const SizedBox(height: 14),
        AuthTextField(
          label: i18n.t('auth:emailLabel'),
          controller: _email,
          placeholder: i18n.t('auth:emailPlaceholder'),
          keyboardType: TextInputType.emailAddress,
          textInputAction: TextInputAction.next,
        ),
        const SizedBox(height: 14),
        PasswordField(
          label: i18n.t('auth:pwLabel'),
          controller: _password,
          placeholder: i18n.t('auth:pwPlaceholder'),
          showLabel: i18n.t('auth:show'),
          hideLabel: i18n.t('auth:hide'),
        ),
        const SizedBox(height: 14),
        PasswordField(
          label: i18n.t('auth:pwConfirmLabel'),
          controller: _confirm,
          placeholder: i18n.t('auth:pwConfirmPlaceholder'),
          showLabel: i18n.t('auth:show'),
          hideLabel: i18n.t('auth:hide'),
          onSubmitted: (_) => _submit(),
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
              i18n.t('auth:registerBtn'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ),
        const SizedBox(height: 16),
        GestureDetector(
          onTap: () => context.go(Paths.login),
          child: Text(
            i18n.t('auth:haveAccount'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.a700),
          ),
        ),
      ],
    );
  }
}
