import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../store/models/store.dart';

/// One line of 「今天可以做」: what to do for this shop today.
class LightStarterCardTile extends StatelessWidget {
  const LightStarterCardTile({
    super.key,
    required this.card,
    required this.onTap,
  });

  final StarterCard card;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.hairSoft,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      card.title,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w600,
                        height: 1.4,
                        color: t.ink,
                      ),
                    ),
                    if (card.hint.isNotEmpty) ...[
                      const SizedBox(height: 2),
                      Text(
                        card.hint,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          height: 1.4,
                          color: t.n600,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Icon(Icons.chevron_right, size: 20, color: t.n500),
            ],
          ),
        ),
      ),
    );
  }
}
