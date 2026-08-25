import 'package:flutter/material.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// bossip brand lockup (web `shared/ui/BrandMark.tsx`): rounded "b" tile on
/// ink + wordmark text. The animated wordmark shine is decorative; here the
/// wordmark renders solid ink (mobile keeps chrome minimal).
class BrandMark extends StatelessWidget {
  const BrandMark({super.key, this.dot = false, this.size = 28});

  final bool dot;
  final double size;

  @override
  Widget build(BuildContext context) {
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
      ],
    );
  }
}
