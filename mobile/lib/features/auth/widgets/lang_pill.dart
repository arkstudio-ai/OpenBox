import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/appearance_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// 中 / EN language toggle (web `features/auth/components/LangPill.tsx`).
class LangPill extends ConsumerWidget {
  const LangPill({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final language = ref.watch(i18nProvider).language;
    return Container(
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      padding: const EdgeInsets.all(2),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          _segment(context, ref, label: '中', lang: 'zh-CN', active: language == 'zh-CN'),
          _segment(context, ref, label: 'EN', lang: 'en-US', active: language == 'en-US'),
        ],
      ),
    );
  }

  Widget _segment(
    BuildContext context,
    WidgetRef ref, {
    required String label,
    required String lang,
    required bool active,
  }) {
    final t = context.tokens;
    return GestureDetector(
      onTap: () => ref.read(appearanceProvider.notifier).setLanguage(lang),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: active ? t.ink : Colors.transparent,
          borderRadius: BorderRadius.circular(Radii.full),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: active ? t.bg : t.n700,
            fontSize: FontSizes.xs,
            fontWeight: FontWeight.w500,
          ),
        ),
      ),
    );
  }
}
