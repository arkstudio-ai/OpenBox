import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../config/env.dart';
import '../i18n/i18n.dart';

/// bossip brand lockup (web `shared/ui/BrandMark.tsx`): rounded "b" tile on
/// ink + wordmark text. The animated wordmark shine is decorative; here the
/// wordmark renders solid ink (mobile keeps chrome minimal).
///
/// A build pointed at anything but production carries a 「测试版」 tag so a
/// test package is never mistaken for the real app.
class BrandMark extends ConsumerWidget {
  const BrandMark({super.key, this.dot = false, this.size = 28});

  final bool dot;
  final double size;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Stack(
          clipBehavior: Clip.none,
          children: [
            Container(
              width: size,
              height: size,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: t.ink,
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Text(
                'b',
                style: TextStyle(
                  color: t.bg,
                  fontSize: size * 0.62,
                  fontWeight: FontWeight.w600,
                  height: 1,
                ),
              ),
            ),
            if (dot)
              Positioned(
                top: -2,
                right: -2,
                child: Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    color: t.sage,
                    shape: BoxShape.circle,
                    border: Border.all(color: t.bg, width: 1.5),
                  ),
                ),
              ),
          ],
        ),
        const SizedBox(width: 10),
        Text(
          'bossip',
          style: TextStyle(
            color: t.ink,
            fontSize: size * 0.6,
            fontWeight: FontWeight.w600,
            letterSpacing: -0.3,
          ),
        ),
        if (!Env.isProduction) ...[
          const SizedBox(width: 8),
          Container(
            key: const Key('brand-test-build'),
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
              color: t.dangerSoft,
              borderRadius: BorderRadius.circular(Radii.sm),
              border: Border.all(color: t.dangerInk.withValues(alpha: 0.4)),
            ),
            child: Text(
              ref.watch(i18nProvider).t('light:testBuild'),
              style: TextStyle(
                color: t.dangerInk,
                fontSize: FontSizes.xs2,
                fontWeight: FontWeight.w700,
                height: 1.2,
              ),
            ),
          ),
        ],
      ],
    );
  }
}
