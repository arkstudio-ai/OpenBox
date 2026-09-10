import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

String platformErrorText(I18nState i18n, Object error) {
  final code = apiErrorOf(error)?.code ?? 'PLATFORM_ERROR';
  final key = 'auth-center:errors.$code';
  final text = i18n.t(key);
  return text == key ? i18n.t('auth-center:errors.PLATFORM_ERROR') : text;
}

String platformDate(DateTime? date, I18nState i18n) => date == null
    ? '—'
    : DateFormat.yMd(i18n.language.replaceAll('-', '_')).format(date.toLocal());

String platformDateTime(DateTime? date, I18nState i18n) => date == null
    ? '—'
    : DateFormat.yMd(
        i18n.language.replaceAll('-', '_'),
      ).add_Hm().format(date.toLocal());

class AuthCard extends StatelessWidget {
  const AuthCard({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    margin: const EdgeInsets.only(bottom: 12),
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: context.tokens.card,
      border: Border.all(color: context.tokens.hair),
      borderRadius: BorderRadius.circular(Radii.lg),
    ),
    child: Material(type: MaterialType.transparency, child: child),
  );
}

class AuthError extends ConsumerWidget {
  const AuthError({super.key, required this.error, required this.retry});
  final Object error;
  final VoidCallback retry;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return AuthCard(
      child: Column(
        children: [
          Text(
            platformErrorText(i18n, error),
            style: TextStyle(color: context.tokens.danger),
          ),
          TextButton(
            onPressed: retry,
            child: Text(i18n.t('common:action.retry')),
          ),
        ],
      ),
    );
  }
}

class AuthStatusPill extends ConsumerWidget {
  const AuthStatusPill(this.status, {super.key, this.job = false});
  final String status;
  final bool job;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final good = status == 'bound' || status == 'published';
    final key = 'auth-center:${job ? 'mobile.jobStatus' : 'status'}.$status';
    final value = i18n.t(key);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: good ? t.a200 : t.n200,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        value == key ? i18n.t('auth-center:mobile.unknown') : value,
        style: TextStyle(fontSize: 12, color: good ? t.a700 : t.n700),
      ),
    );
  }
}
