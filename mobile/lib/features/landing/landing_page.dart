import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/api/auth_store.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/widgets/brand_mark.dart';
import '../auth/widgets/lang_pill.dart';

/// Landing page (web `LandingRoute`), mobile-condensed: topbar + hero +
/// feature list. CTAs run web `useStart`: authed → /app, else → /login.
class LandingPage extends ConsumerWidget {
  const LandingPage({super.key});

  void _start(BuildContext context, WidgetRef ref, {bool register = false}) {
    final authed = ref.read(authProvider).isAuthenticated;
    if (authed) {
      context.go(Paths.app);
      return;
    }
    context.go(register ? Paths.register : Paths.login);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final features = i18n.tList('landing:features');
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 40),
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const BrandMark(dot: true),
                Row(
                  children: [
                    const LangPill(),
                    const SizedBox(width: 10),
                    TextButton(
                      onPressed: () => _start(context, ref),
                      child: Text(
                        i18n.t('landing:signIn'),
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.ink),
                      ),
                    ),
                  ],
                ),
              ],
            ),
            const SizedBox(height: 56),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.full),
                color: t.card,
              ),
              child: Text(
                i18n.t('landing:badge'),
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
              ),
            ),
            const SizedBox(height: 20),
            Text(
              i18n.t('landing:heroTitle'),
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: FontSizes.hero,
                height: 1.18,
                fontWeight: FontWeight.w500,
                letterSpacing: -0.8,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 14),
            Text(
              i18n.t('landing:heroBody'),
              textAlign: TextAlign.center,
              style:
                  TextStyle(fontSize: FontSizes.base, height: 1.7, color: t.n700),
            ),
            const SizedBox(height: 24),
            SizedBox(
              height: 46,
              child: FilledButton(
                onPressed: () => _start(context, ref, register: true),
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t('landing:ctaPrimary'),
                  style: const TextStyle(
                      fontSize: FontSizes.base, fontWeight: FontWeight.w500),
                ),
              ),
            ),
            const SizedBox(height: 10),
            Center(
              child: Text(
                i18n.t('landing:ctaNote'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
              ),
            ),
            const SizedBox(height: 48),
            for (final feature in features)
              if (feature is Map<String, dynamic>)
                Container(
                  margin: const EdgeInsets.only(bottom: 10),
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: t.card,
                    borderRadius: BorderRadius.circular(Radii.xl),
                    border: Border.all(color: t.hair),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        feature['title'] as String? ?? '',
                        style: TextStyle(
                          fontSize: FontSizes.lg,
                          fontWeight: FontWeight.w500,
                          color: t.ink,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        feature['body'] as String? ?? '',
                        style: TextStyle(
                            fontSize: FontSizes.sm,
                            height: 1.6,
                            color: t.n600),
                      ),
                    ],
                  ),
                ),
          ],
        ),
      ),
    );
  }
}
