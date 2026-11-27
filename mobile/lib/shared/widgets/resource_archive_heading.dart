import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../i18n/i18n.dart';

class ResourceArchiveHeading extends ConsumerWidget {
  const ResourceArchiveHeading({super.key, required this.title});

  final String? title;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final name = title?.trim();
    final label = name == null
        ? i18n.t('resources:archive.unfiled')
        : name.isEmpty
        ? i18n.t('workspace:untitledChat')
        : name;
    return Semantics(
      header: true,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 10),
        child: Row(
          children: [
            Icon(Icons.folder_outlined, size: 17, color: t.n700),
            const SizedBox(width: 6),
            Expanded(
              child: Text(
                label,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: t.ink,
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
