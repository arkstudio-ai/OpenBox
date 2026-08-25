import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/widgets/brand_mark.dart';
import 'lang_pill.dart';

/// Auth page scaffold (web `features/auth/components/AuthShell.tsx`),
/// mobile-optimized: header row (brand + lang), centered card.
class AuthShell extends StatelessWidget {
  const AuthShell({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Scaffold(
      backgroundColor: t.bg,
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 0),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: const [BrandMark(), LangPill()],
              ),
            ),
            Expanded(
              child: Center(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.all(20),
                  child: Container(
                    constraints: const BoxConstraints(maxWidth: 392),
                    padding: const EdgeInsets.all(24),
                    decoration: BoxDecoration(
                      color: t.card.withValues(alpha: 0.85),
                      borderRadius: BorderRadius.circular(Radii.xl2),
                      border: Border.all(color: t.hair),
                      boxShadow: [
                        BoxShadow(
                          color: const Color(0xFF3E3929).withValues(alpha: 0.03),
                          offset: const Offset(0, 1),
                          blurRadius: 2,
                        ),
                        BoxShadow(
                          color: const Color(0xFF3E3929).withValues(alpha: 0.26),
                          offset: const Offset(0, 30),
                          blurRadius: 70,
                          spreadRadius: -42,
                        ),
                      ],
                    ),
                    child: child,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
