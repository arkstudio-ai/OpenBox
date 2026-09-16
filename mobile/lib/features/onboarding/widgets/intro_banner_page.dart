import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../state/onboarding_store.dart';

/// L1: three-screen intro shown once per install, before sign-in.
class IntroBannerPage extends ConsumerStatefulWidget {
  const IntroBannerPage({super.key});

  @override
  ConsumerState<IntroBannerPage> createState() => _IntroBannerPageState();
}

class _IntroBannerPageState extends ConsumerState<IntroBannerPage> {
  final _pages = PageController();
  int _index = 0;

  @override
  void dispose() {
    _pages.dispose();
    super.dispose();
  }

  Future<void> _finish(String destination) async {
    await ref.read(prefsProvider).setBool(introSeenKey, true);
    ref.read(introSeenProvider.notifier).state = true;
    if (mounted) context.go(destination);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    const screens = ['s1', 's2', 's3'];
    final last = _index == screens.length - 1;
    return Scaffold(
      backgroundColor: t.bg,
      body: Stack(
        children: [
          PageView.builder(
            controller: _pages,
            itemCount: screens.length,
            onPageChanged: (i) => setState(() => _index = i),
            itemBuilder: (context, i) => _IntroScreen(
              image: 'assets/onboarding/intro-${i + 1}.jpg',
              title: i18n.t('onboarding:intro.${screens[i]}.title'),
              lede: i18n.t('onboarding:intro.${screens[i]}.lede'),
            ),
          ),
          if (!last)
            Positioned(
              top: MediaQuery.paddingOf(context).top + 4,
              right: 12,
              child: TextButton(
                key: const Key('intro-skip'),
                onPressed: () => _finish(Paths.landing),
                child: Text(
                  i18n.t('onboarding:intro.skip'),
                  style: TextStyle(fontSize: FontSizes.base, color: t.n700),
                ),
              ),
            ),
          Positioned(
            left: 24,
            right: 24,
            bottom: MediaQuery.paddingOf(context).bottom + 16,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                _Dots(count: screens.length, active: _index, tokens: t),
                const SizedBox(height: 18),
                if (last) ...[
                  _Primary(
                    key: const Key('intro-sign-in'),
                    label: i18n.t('onboarding:intro.signIn'),
                    onTap: () => _finish(Paths.login),
                    tokens: t,
                  ),
                  const SizedBox(height: 10),
                  _Secondary(
                    label: i18n.t('onboarding:intro.register'),
                    onTap: () => _finish(Paths.register),
                    tokens: t,
                  ),
                ] else
                  _Primary(
                    key: const Key('intro-next'),
                    label: i18n.t('onboarding:intro.next'),
                    onTap: () => _pages.nextPage(
                      duration: const Duration(milliseconds: 260),
                      curve: Curves.easeOutCubic,
                    ),
                    tokens: t,
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _IntroScreen extends StatelessWidget {
  const _IntroScreen({
    required this.image,
    required this.title,
    required this.lede,
  });

  final String image;
  final String title;
  final String lede;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final height = MediaQuery.sizeOf(context).height;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          height: height * 0.52,
          child: Image.asset(
            image,
            fit: BoxFit.cover,
            alignment: Alignment.topCenter,
            errorBuilder: (_, _, _) => ColoredBox(color: t.surface),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(24, 28, 24, 0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: TextStyle(
                  fontSize: FontSizes.xl3,
                  fontWeight: FontWeight.w700,
                  height: 1.25,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 10),
              Text(
                lede,
                style: TextStyle(
                  fontSize: FontSizes.base,
                  height: 1.6,
                  color: t.n700,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _Dots extends StatelessWidget {
  const _Dots({required this.count, required this.active, required this.tokens});

  final int count;
  final int active;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisAlignment: MainAxisAlignment.center,
    children: [
      for (var i = 0; i < count; i++)
        AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          margin: const EdgeInsets.symmetric(horizontal: 3),
          width: i == active ? 22 : 6,
          height: 6,
          decoration: BoxDecoration(
            color: i == active ? tokens.ink : tokens.n400,
            borderRadius: BorderRadius.circular(Radii.full),
          ),
        ),
    ],
  );
}

class _Primary extends StatelessWidget {
  const _Primary({
    super.key,
    required this.label,
    required this.onTap,
    required this.tokens,
  });

  final String label;
  final VoidCallback onTap;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) => FilledButton(
    onPressed: onTap,
    style: FilledButton.styleFrom(
      backgroundColor: tokens.a700,
      foregroundColor: tokens.bg,
      minimumSize: const Size.fromHeight(52),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(Radii.full),
      ),
    ),
    child: Text(
      label,
      style: const TextStyle(
        fontSize: FontSizes.lg,
        fontWeight: FontWeight.w600,
      ),
    ),
  );
}

class _Secondary extends StatelessWidget {
  const _Secondary({
    required this.label,
    required this.onTap,
    required this.tokens,
  });

  final String label;
  final VoidCallback onTap;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) => OutlinedButton(
    onPressed: onTap,
    style: OutlinedButton.styleFrom(
      foregroundColor: tokens.ink,
      side: BorderSide(color: tokens.n400),
      minimumSize: const Size.fromHeight(52),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(Radii.full),
      ),
    ),
    child: Text(
      label,
      style: const TextStyle(
        fontSize: FontSizes.lg,
        fontWeight: FontWeight.w600,
      ),
    ),
  );
}
