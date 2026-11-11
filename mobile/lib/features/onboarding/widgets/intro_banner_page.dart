import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../state/onboarding_store.dart';
import 'intro_sample.dart';

/// L1, one screen (Light 模式 M1): a line on what bossip does, a sample
/// finished video playing on its own, and one button that leads — through
/// sign-up — into making the person's own. Shown once per install, before
/// sign-in; the small link below is for accounts that already exist.
class IntroBannerPage extends ConsumerWidget {
  const IntroBannerPage({super.key});

  Future<void> _finish(
    BuildContext context,
    WidgetRef ref,
    String destination,
  ) async {
    await ref.read(prefsProvider).setBool(introSeenKey, true);
    ref.read(introSeenProvider.notifier).state = true;
    if (context.mounted) context.go(destination);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final height = MediaQuery.sizeOf(context).height;
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 28, 24, 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                i18n.t('onboarding:intro.brand'),
                style: TextStyle(
                  fontSize: FontSizes.md,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.2,
                  color: t.a800,
                ),
              ),
              const SizedBox(height: 10),
              Text(
                i18n.t('onboarding:intro.title'),
                style: TextStyle(
                  fontSize: FontSizes.xl4,
                  fontWeight: FontWeight.w800,
                  height: 1.25,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                i18n.t('onboarding:intro.lede'),
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  height: 1.45,
                  color: t.n700,
                ),
              ),
              Expanded(
                child: Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      ConstrainedBox(
                        constraints: BoxConstraints(
                          maxHeight: (height * 0.44).clamp(220.0, 400.0),
                        ),
                        child: IntroSample(
                          tag: i18n.t('onboarding:intro.sampleTag'),
                          shopName: i18n.t('onboarding:intro.sampleShop'),
                          line: i18n.t('onboarding:intro.sampleLine'),
                        ),
                      ),
                      const SizedBox(height: 10),
                      Text(
                        i18n.t('onboarding:intro.sampleCaption'),
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                      ),
                    ],
                  ),
                ),
              ),
              FilledButton(
                key: const Key('intro-register'),
                onPressed: () => _finish(context, ref, Paths.register),
                style: FilledButton.styleFrom(
                  backgroundColor: t.a700,
                  foregroundColor: t.bg,
                  minimumSize: const Size.fromHeight(54),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t('onboarding:intro.cta'),
                  style: const TextStyle(
                    fontSize: FontSizes.xl,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              const SizedBox(height: 10),
              Text(
                i18n.t('onboarding:intro.ctaHint'),
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
              ),
              TextButton(
                key: const Key('intro-sign-in'),
                onPressed: () => _finish(context, ref, Paths.login),
                style: TextButton.styleFrom(
                  foregroundColor: t.n700,
                  minimumSize: const Size.fromHeight(44),
                ),
                child: Text(
                  i18n.t('onboarding:intro.signIn'),
                  style: const TextStyle(fontSize: FontSizes.base),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
